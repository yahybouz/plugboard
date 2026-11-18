"""Egress guard: protected providers never connect to a non-public address.

The check runs on every connection, not once at creation: a name that later
resolves inside the network (DNS rebinding) or an IP literal is refused before
any socket is opened.
"""

from __future__ import annotations

import asyncio
import contextvars
import errno
import ipaddress
import json
import logging
import socket
import time
from collections.abc import Mapping
from typing import Any

import aiohttp
import aiohttp.resolver
from aiohttp.abc import AbstractResolver, ResolveResult

from plugboard import alerts, audit

log = logging.getLogger(__name__)

EGRESS_REFUSED = "egress_refused"

# Refusal report (D-17): one audit entry and one alert per provider per window.
REPORT_WINDOW_S = 300.0
_clock = time.monotonic
_last_report: dict[str, float] = {}
_tasks: set[asyncio.Task] = set()

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_METADATA = ipaddress.ip_address("169.254.169.254")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")


def ip_is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv6 forms that carry an IPv4 address are judged by that address.
        if ip in _NAT64_LOCAL:
            return False
        embedded = (
            ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        )
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            ip = embedded
    if ip == _METADATA or (ip.version == 4 and ip in _CGNAT):
        return False
    return bool(ip.is_global) and not ip.is_multicast and not ip.is_unspecified


class EgressRefused(OSError):
    """A protected provider tried to reach a non-public address."""


def refuse_non_public(addr_info: aiohttp.AddrInfoType) -> socket.socket:
    """``socket_factory`` for aiohttp: no socket for a non-public address.

    It sees every address aiohttp is about to connect to, IP literals and
    redirect targets included.
    """
    family, type_, proto, _, sockaddr = addr_info
    if not ip_is_public(sockaddr[0]):
        raise EgressRefused(errno.EACCES, "non-public address refused")
    return socket.socket(family=family, type=type_, proto=proto)


class PublicOnlyResolver(AbstractResolver):
    """Resolver that refuses a name as soon as one of its answers is not public.

    The same rule as at creation: a name answering both public and private
    addresses is refused, never raced. aiohttp caches no failed lookup, so the
    next request resolves again.
    """

    def __init__(self, inner: AbstractResolver | None = None) -> None:
        self._inner = inner if inner is not None else aiohttp.resolver.DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        answers = await self._inner.resolve(host, port, family)
        if any(not ip_is_public(answer["host"]) for answer in answers):
            raise EgressRefused(errno.EACCES, "non-public address refused")
        return answers

    async def close(self) -> None:
        await self._inner.close()


def public_connector(
    *,
    resolver: AbstractResolver | None = None,
    inner: AbstractResolver | None = None,
    **kw,
) -> aiohttp.TCPConnector:
    """A connector that reaches public addresses only.

    The resolver refuses non-public names; ``refuse_non_public`` is the net
    for IP literals, a redirect's included, which never reach the resolver.
    """
    return aiohttp.TCPConnector(
        resolver=resolver or PublicOnlyResolver(inner),
        socket_factory=refuse_non_public,
        **kw,
    )


def client_session(public_only: bool, **kw) -> aiohttp.ClientSession:
    """A session for one provider: public-only and proxy-free when guarded.

    Unguarded, it is ``aiohttp.ClientSession(**kw)`` with nothing added.
    """
    if public_only:
        if "connector" not in kw:
            kw["connector"] = public_connector()
        kw["trust_env"] = False
    return aiohttp.ClientSession(**kw)


def is_egress_refusal(exc: BaseException) -> bool:
    """True when a connection failed because the guard refused the address."""
    return isinstance(exc, aiohttp.ClientConnectorError) and isinstance(
        exc.os_error, EgressRefused
    )


def _is_http(url: Any) -> bool:
    # aiohttp lowercases the scheme: "HTTPS://" is still a connection to guard.
    return isinstance(url, str) and url.lower().startswith(("http://", "https://"))


def guard_is_forced(provider: Mapping[str, Any]) -> bool:
    """True when the guard is imposed: catalogue providers and built-in HTTP presets.

    ``subprocess://`` presets never open an HTTP connection, so they are not
    guarded.
    """
    from plugboard import direct_providers, models_dev

    key = provider.get("preset_key")
    if not isinstance(key, str) or not key:
        return False
    if key.startswith(models_dev.PRESET_PREFIX):
        return True
    preset = direct_providers.PRESETS.get(key)
    return preset is not None and _is_http(preset.get("base_url"))


def is_protected(provider: Mapping[str, Any]) -> bool:
    """True when the provider's connections go through the egress guard.

    Imposed by ``guard_is_forced``, or chosen by the admin (``egress_guard``)
    for a provider that reaches an HTTP(S) ``base_url``.
    """
    return guard_is_forced(provider) or (
        provider.get("egress_guard") is True and _is_http(provider.get("base_url"))
    )


def report_refusal(provider_id: str, host: str, source: str) -> bool:
    """Report a refused connection: once per provider per ``REPORT_WINDOW_S``.

    The first refusal of a window is logged as an error, then audited and
    alerted by a background task; the caller never waits for it, so a burst of
    refusals does not block the event loop. Later refusals of the window are
    logged at debug level only. Nothing is disabled: the next connection is
    checked again. Returns True when this refusal was reported.
    """
    now = _clock()
    last = _last_report.get(provider_id)
    if last is not None and now - last < REPORT_WINDOW_S:
        log.debug(
            "[%s] egress refused: %s resolves to a non-public address "
            "(source: %s, already reported)",
            provider_id,
            host,
            source,
        )
        return False
    _last_report[provider_id] = now
    log.error(
        "[%s] egress refused: %s resolves to a non-public address (source: %s)",
        provider_id,
        host,
        source,
    )
    # A fresh context: the report must not hold the principal of the admin
    # request that triggered it (PB-AUTH-01).
    task = asyncio.get_running_loop().create_task(
        _audit_and_alert(provider_id, host, source), context=contextvars.Context()
    )
    _tasks.add(task)  # keep a reference until it finishes
    task.add_done_callback(_tasks.discard)
    return True


async def _audit_and_alert(provider_id: str, host: str, source: str) -> None:
    try:
        await asyncio.to_thread(
            audit.log_action,
            "provider.egress_refused",
            actor="system",
            target_type="provider",
            target_id=provider_id,
            details=json.dumps({"host": host, "source": source}),
        )
    except Exception as exc:
        log.warning("Egress refusal audit failed for %s: %s", provider_id, exc)
    try:
        await alerts.send_egress_refused_alert(provider_id, host)
    except Exception as exc:
        log.warning("Egress refusal alert failed for %s: %s", provider_id, exc)
