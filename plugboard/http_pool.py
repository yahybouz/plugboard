"""Shared aiohttp.ClientSession with pooled connections (PERF-2).

Problem: every outbound request used to open a fresh `ClientSession()`
which means TCP connect + TLS handshake + DNS on each call. For a
cloud provider like Anthropic or GitHub Models that's easily 50-150 ms
per request even when the server is healthy.

Solution: one singleton session shared app-wide with:
- A bounded connection pool (limit=100 total, 20 per host) so we don't
  hammer a single provider.
- Keep-alive so subsequent calls reuse the same TCP connection.
- DNS cache (ttl=300s) to skip repeated lookups.
- Sensible default timeout so a stuck upstream can't hang us forever.

Integration points are strictly the HTTP callers on hot paths
(router.forward, router.forward_stream, health_probe, monitoring,
watchdog). Callers pass `timeout` per request — we never hard-code it
at the session level to preserve per-endpoint budgets.

A second singleton, the public session (PLU-7), serves the providers
the egress guard protects (`netguard.is_protected`): it connects
directly, never through HTTPS_PROXY / HTTP_PROXY, and only to public
addresses, checked at every connection. `session_for(provider)` picks
the right one.

Lifecycle: created lazily on first use, closed by the FastAPI lifespan
on shutdown. Tests that create/destroy event loops should call
:func:`close_session` in teardown.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Optional

import aiohttp

from plugboard import envcompat, netguard

log = logging.getLogger(__name__)

# Tunables (env overridable)
_CONN_LIMIT = envcompat.get_int("PLUGBOARD_HTTP_POOL_SIZE", default=100)
_CONN_PER_HOST = envcompat.get_int("PLUGBOARD_HTTP_POOL_PER_HOST", default=20)
_DNS_TTL = envcompat.get_int("PLUGBOARD_HTTP_DNS_TTL", default=300)

_session: Optional[aiohttp.ClientSession] = None
_public_session: Optional[aiohttp.ClientSession] = None
_public_resolver: Optional[netguard.PublicOnlyResolver] = None
_lock = asyncio.Lock()


def _build_session() -> aiohttp.ClientSession:
    connector = aiohttp.TCPConnector(
        limit=_CONN_LIMIT,
        limit_per_host=_CONN_PER_HOST,
        ttl_dns_cache=_DNS_TTL,
        # `enable_cleanup_closed` removed: no-op on Python 3.12.5+ since
        # the upstream SSL transport leak was fixed (cpython#118960).
    )
    return aiohttp.ClientSession(
        connector=connector,
        # No default timeout here — callers set their own.
        raise_for_status=False,
        trust_env=True,  # honour HTTPS_PROXY / HTTP_PROXY when set
    )


async def get_session() -> aiohttp.ClientSession:
    """Return the singleton session, creating it on first call."""
    global _session
    if _session is not None and not _session.closed:
        return _session
    async with _lock:
        # Double-check under lock — racing callers on first access.
        if _session is None or _session.closed:
            _session = _build_session()
            log.info(
                "http_pool: session created (limit=%d, per_host=%d, dns_ttl=%ds)",
                _CONN_LIMIT,
                _CONN_PER_HOST,
                _DNS_TTL,
            )
    return _session


async def get_public_session() -> aiohttp.ClientSession:
    """Return the public-only session of protected providers (PLU-7).

    Direct (``trust_env=False``: a proxy would resolve the name itself,
    out of the guard's sight) and public addresses only, checked by the
    resolver and by the socket factory at every connection.
    """
    global _public_session, _public_resolver
    if _public_session is not None and not _public_session.closed:
        return _public_session
    async with _lock:
        # Double-check under lock — racing callers on first access.
        if _public_session is None or _public_session.closed:
            if _public_resolver is None:
                _public_resolver = netguard.PublicOnlyResolver()
            connector = netguard.public_connector(
                resolver=_public_resolver,
                limit=_CONN_LIMIT,
                limit_per_host=_CONN_PER_HOST,
                ttl_dns_cache=_DNS_TTL,
            )
            _public_session = aiohttp.ClientSession(
                connector=connector,
                # No default timeout here — callers set their own.
                raise_for_status=False,
                trust_env=False,  # never through a proxy (D-20)
            )
            log.info("http_pool: public-only session created")
    return _public_session


async def session_for(provider: Mapping[str, Any]) -> aiohttp.ClientSession:
    """The session to reach ``provider``'s base URL with.

    Protected providers get the public-only session, the others the
    shared one. ``get_session`` is looked up at call time, so a fake set
    on ``http_pool.get_session`` still intercepts.
    """
    if netguard.is_protected(provider):
        return await get_public_session()
    return await get_session()


async def close_session() -> None:
    """Close both singletons. Call from FastAPI lifespan on shutdown."""
    global _session, _public_session, _public_resolver
    if _session is not None and not _session.closed:
        try:
            await _session.close()
        except Exception as exc:  # pragma: no cover
            log.warning("http_pool: close error (ignored): %s", exc)
    _session = None
    if _public_session is not None and not _public_session.closed:
        try:
            await _public_session.close()
        except Exception as exc:  # pragma: no cover
            log.warning("http_pool: public close error (ignored): %s", exc)
    _public_session = None
    # The connector does not own a resolver it was given: close it here.
    if _public_resolver is not None:
        try:
            await _public_resolver.close()
        except Exception as exc:  # pragma: no cover
            log.warning("http_pool: resolver close error (ignored): %s", exc)
    _public_resolver = None


async def reset_for_tests() -> None:
    """Test helper — drop the singleton so the next call rebuilds it.

    Useful when a test sets an env var that the session already
    captured (e.g. PLUGBOARD_HTTP_POOL_SIZE).
    """
    await close_session()
