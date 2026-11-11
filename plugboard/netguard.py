"""Egress guard: protected providers never connect to a non-public address.

The check runs on every connection, not once at creation: a name that later
resolves inside the network (DNS rebinding) or an IP literal is refused before
any socket is opened.
"""

from __future__ import annotations

import errno
import ipaddress
import socket

import aiohttp

EGRESS_REFUSED = "egress_refused"

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
