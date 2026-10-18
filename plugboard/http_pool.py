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

Lifecycle: created lazily on first use, closed by the FastAPI lifespan
on shutdown. Tests that create/destroy event loops should call
:func:`close_session` in teardown.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

# Tunables (env overridable)
_CONN_LIMIT = int(os.environ.get("PLUGBOARD_HTTP_POOL_SIZE", "100"))
_CONN_PER_HOST = int(os.environ.get("PLUGBOARD_HTTP_POOL_PER_HOST", "20"))
_DNS_TTL = int(os.environ.get("PLUGBOARD_HTTP_DNS_TTL", "300"))

_session: Optional[aiohttp.ClientSession] = None
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


async def close_session() -> None:
    """Close the singleton. Call from FastAPI lifespan on shutdown."""
    global _session
    if _session is not None and not _session.closed:
        try:
            await _session.close()
        except Exception as exc:  # pragma: no cover
            log.warning("http_pool: close error (ignored): %s", exc)
    _session = None


async def reset_for_tests() -> None:
    """Test helper — drop the singleton so the next call rebuilds it.

    Useful when a test sets an env var that the session already
    captured (e.g. PLUGBOARD_HTTP_POOL_SIZE).
    """
    await close_session()
