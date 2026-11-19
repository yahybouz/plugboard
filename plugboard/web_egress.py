"""Egress guard for the web tools of a CLI (PLU-158).

A small HTTP CONNECT proxy, bound to the loopback, that the claude CLI is
pointed at (HTTPS_PROXY) when a request asks for the web. It tunnels only to
public addresses: every destination is resolved here, each address is checked
with ``netguard.ip_is_public`` and the tunnel connects to the checked address
(no second resolution, so no DNS rebinding). Loopback, private ranges, the
tailnet's CGNAT range, link-local and the cloud metadata address are refused
with 403. Plain-HTTP proxying (absolute-URI GET) is refused too: WebFetch
upgrades http:// to https://, and nothing else is needed.

It runs in the gateway's event loop, started on first use. It reads no body:
it only moves bytes, and logs nothing of what goes through (host names only
at debug level when refused).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Optional, Tuple

from plugboard import netguard

log = logging.getLogger(__name__)

_MAX_HEAD = 8192
_HEAD_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 10.0
_IDLE_TIMEOUT_S = 120.0
_BUF = 65536

_server: Optional[asyncio.AbstractServer] = None
_port: Optional[int] = None
_lock: Optional[asyncio.Lock] = None
refused_total = 0
tunnels_total = 0


def _parse_target(line: str) -> Optional[Tuple[str, int]]:
    """(host, port) of a ``CONNECT host:port HTTP/1.1`` line, else None."""
    parts = line.split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT":
        return None
    target = parts[1]
    if target.startswith("["):  # [v6]:port
        host, _, rest = target[1:].partition("]")
        port_s = rest.removeprefix(":")
    else:
        host, _, port_s = target.rpartition(":")
    if not host or not port_s.isdigit():
        return None
    port = int(port_s)
    if not 0 < port < 65536:
        return None
    return host, port


async def _public_addresses(host: str, port: int) -> list:
    """The public addresses ``host`` resolves to; [] when any is not public.

    One non-public answer refuses the whole name: a name that mixes public
    and internal addresses is not one to trust.
    """
    try:
        ipaddress.ip_address(host)
        infos = [(socket.AF_INET6 if ":" in host else socket.AF_INET, host)]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            res = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return []
        infos = [(fam, sa[0]) for fam, _t, _p, _c, sa in res]
    if not infos:
        return []
    for _fam, addr in infos:
        try:
            if not netguard.ip_is_public(addr):
                return []
        except ValueError:
            return []
    return infos


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await asyncio.wait_for(reader.read(_BUF), _IDLE_TIMEOUT_S)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # pragma: no cover - already closed
            pass


async def _reply(writer: asyncio.StreamWriter, status: str) -> None:
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n".encode())
    try:
        await writer.drain()
    finally:
        writer.close()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global refused_total, tunnels_total
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT_S)
    except (
        asyncio.TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        writer.close()
        return
    if len(head) > _MAX_HEAD:
        await _reply(writer, "431 Request Header Fields Too Large")
        return
    first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    target = _parse_target(first)
    if target is None:
        await _reply(writer, "405 Method Not Allowed")
        return
    host, port = target
    addrs = await _public_addresses(host, port)
    if not addrs:
        refused_total += 1
        log.info("web egress refused: destination is not public")
        log.debug("web egress refused host=%s port=%d", host, port)
        await _reply(writer, "403 Forbidden")
        return
    up_reader = up_writer = None
    for _fam, addr in addrs:
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(addr, port), _CONNECT_TIMEOUT_S
            )
            break
        except (OSError, asyncio.TimeoutError):
            continue
    if up_writer is None:
        await _reply(writer, "502 Bad Gateway")
        return
    tunnels_total += 1
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))


async def ensure_started() -> int:
    """Start the proxy once (loopback, a free port) and return its port."""
    global _server, _port, _lock
    if _server is not None and _port is not None:
        return _port
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _server is None:
            _server = await asyncio.start_server(
                _handle, host="127.0.0.1", port=0, limit=_MAX_HEAD * 2
            )
            _port = _server.sockets[0].getsockname()[1]
            log.info("web egress guard listening on 127.0.0.1:%d", _port)
    assert _port is not None
    return _port


async def stop() -> None:
    global _server, _port
    if _server is not None:
        _server.close()
        await _server.wait_closed()
    _server = None
    _port = None


def stats() -> dict:
    """Tunnels opened and refused since start (counts only)."""
    return {
        "egress_listening": _port is not None,
        "egress_tunnels": tunnels_total,
        "egress_refused": refused_total,
    }


def proxy_env(port: int) -> dict:
    """The child env entries that send every connection through the guard."""
    url = f"http://127.0.0.1:{port}"
    return {
        "HTTPS_PROXY": url,
        "HTTP_PROXY": url,
        "https_proxy": url,
        "http_proxy": url,
        "NO_PROXY": "",
        "no_proxy": "",
    }
