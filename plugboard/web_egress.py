"""Egress guard for the web tools of a CLI (PLU-158).

A small HTTP CONNECT proxy, bound to the loopback, that the claude CLI is
pointed at (HTTPS_PROXY) when a request asks for the web. It tunnels only to
public addresses: every destination is resolved here, each address is checked
with ``netguard.ip_is_public`` and the tunnel connects to the checked address
(no second resolution, so no DNS rebinding). Loopback, private ranges, the
tailnet's CGNAT range, link-local and the cloud metadata address are refused
with 403, and so is a tunnel to any port but 443 (4.15.0), so no plain HTTP
goes through it. Plain-HTTP proxying (absolute-URI GET) is refused too:
WebFetch upgrades http:// to https://, and nothing else is needed.

Names are resolved on threads of the egress' own (8 at most), never on the
loop's default executor, and a name that does not resolve in 5 s is refused
(PLU-170).

It runs in the gateway's event loop, started on first use. It reads no body:
it only moves bytes, and logs nothing of what goes through (host names only
at debug level when refused).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from plugboard import envcompat, netguard

log = logging.getLogger(__name__)

_MAX_HEAD = 8192
_HEAD_TIMEOUT_S = 10.0
_CONNECT_TIMEOUT_S = 10.0
_IDLE_TIMEOUT_S = 120.0
_BUF = 65536
# Name resolution (PLU-170): on threads of its own, never the loop's default
# executor (shared with asyncio.to_thread, which writes billing), at most
# _DNS_THREADS at once, and given up after _DNS_TIMEOUT_S: a name whose DNS
# hangs (an authority the asker controls) is refused, its thread left to
# finish on its own; a resolution still queued at the deadline never runs.
_DNS_THREADS = 8
_DNS_TIMEOUT_S = 5.0
_dns_pool: Optional[ThreadPoolExecutor] = None
# A tunnel's whole life (PLU-169), on top of the idle delay of each way:
# bytes trickled in both directions never keep a connection forever.
_TUNNEL_MAX_S = 600.0

_server: Optional[asyncio.AbstractServer] = None
_port: Optional[int] = None
# The only port a tunnel goes to: HTTPS.
_TLS_PORT = 443
_lock: Optional[asyncio.Lock] = None
refused_total = 0
tunnels_total = 0

# Connections one egress serves at once (each proxy: the web tools' one, and
# one per agy call, BUG-156); past it, 503 at once, nothing read.
MAX_CONNECTIONS = 64
# Refusals are logged once per host and window, then summed up (how many
# more) at the first refusal of that host after the window; past
# REFUSAL_LOG_HOSTS hosts in a window, the others share one line.
REFUSAL_LOG_WINDOW_S = 60.0
REFUSAL_LOG_HOSTS = 256
_OTHER_HOSTS = "<other hosts>"
_clock = time.monotonic


class RefusalLog:
    """The refusal lines of one egress: one per host and window, each later
    one saying how many were not logged, so a client that repeats a refused
    request cannot fill the log."""

    def __init__(self, label: str) -> None:
        self.label = label
        # host -> [window start, refusals not logged since]
        self._hosts: Dict[str, List[float]] = {}

    def refused(self, host: str, reason: str) -> None:
        now = _clock()
        key = host[:253]
        if key not in self._hosts and len(self._hosts) >= REFUSAL_LOG_HOSTS:
            self._prune(now)
            if len(self._hosts) >= REFUSAL_LOG_HOSTS:
                key = _OTHER_HOSTS
        entry = self._hosts.get(key)
        if entry is not None and now - entry[0] < REFUSAL_LOG_WINDOW_S:
            entry[1] += 1
            return
        more = int(entry[1]) if entry is not None else 0
        self._hosts[key] = [now, 0]
        self._log(key, reason if key != _OTHER_HOSTS else "", more)

    def _prune(self, now: float) -> None:
        for host, (start, more) in list(self._hosts.items()):
            if now - start >= REFUSAL_LOG_WINDOW_S:
                del self._hosts[host]
                if more:
                    self._log(host, "", int(more))

    def flush(self) -> None:
        """Log what was not logged yet (the egress is closing)."""
        for host, (_start, more) in self._hosts.items():
            if more:
                self._log(host, "", int(more))
        self._hosts.clear()

    def _log(self, host: str, reason: str, more: int) -> None:
        tail = (
            f" ({more} more refused in the last {REFUSAL_LOG_WINDOW_S:.0f} s)"
            if more
            else ""
        )
        log.info("%s refused: host=%s%s%s", self.label, host, reason, tail)


class ConnectionCap:
    """At most :data:`MAX_CONNECTIONS` connections of one egress at once;
    the first refusal of each window is logged."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.active = 0
        self._full_logged: Optional[float] = None

    def enter(self) -> bool:
        if self.active < MAX_CONNECTIONS:
            self.active += 1
            return True
        now = _clock()
        if self._full_logged is None or now - self._full_logged >= REFUSAL_LOG_WINDOW_S:
            self._full_logged = now
            log.warning(
                "%s: %d connections open, refusing more (503)",
                self.label,
                MAX_CONNECTIONS,
            )
        return False

    def leave(self) -> None:
        self.active -= 1


async def reply_busy(writer: asyncio.StreamWriter) -> None:
    """503 at once to a connection past the cap, nothing read."""
    try:
        await _reply(writer, "503 Service Unavailable")
    except (ConnectionError, OSError):
        pass


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
    # ASCII digits only: "²" is a digit to isdigit(), not to int().
    if not host or not (port_s.isascii() and port_s.isdigit()):
        return None
    port = int(port_s)
    if not 0 < port < 65536:
        return None
    return host, port


def tunnel_max_seconds() -> float:
    """How long a tunnel may last, ``PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS``
    (default 600; a value that is not a number above 0 gives the default)."""
    return envcompat.get_float(
        "PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS", default=_TUNNEL_MAX_S, above=0
    )


def _getaddrinfo(host: str, port: int) -> list:
    """The blocking lookup, run on the resolver threads."""
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def _dns_executor() -> ThreadPoolExecutor:
    global _dns_pool
    if _dns_pool is None:
        _dns_pool = ThreadPoolExecutor(
            max_workers=_DNS_THREADS, thread_name_prefix="egress-dns"
        )
    return _dns_pool


async def _resolve(host: str, port: int) -> list:
    """``getaddrinfo`` of ``host`` on the egress' own threads; raises
    ``TimeoutError`` past :data:`_DNS_TIMEOUT_S`, ``OSError`` when the name
    does not resolve."""
    loop = asyncio.get_running_loop()
    lookup = loop.run_in_executor(_dns_executor(), _getaddrinfo, host, port)
    return await asyncio.wait_for(lookup, _DNS_TIMEOUT_S)


async def _public_addresses(host: str, port: int) -> list:
    """The public addresses ``host`` resolves to; [] when any is not public.

    One non-public answer refuses the whole name: a name that mixes public
    and internal addresses is not one to trust. Raises ``TimeoutError`` when
    the name did not resolve in time.
    """
    try:
        ipaddress.ip_address(host)
        infos = [(socket.AF_INET6 if ":" in host else socket.AF_INET, host)]
    except ValueError:
        try:
            res = await _resolve(host, port)
        except TimeoutError:
            raise
        except (OSError, UnicodeError):
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
    """Answer and close; a client already gone is not an error."""
    writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n".encode())
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


_web_cap = ConnectionCap("web egress")
_web_refusals = RefusalLog("web egress")


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    global refused_total
    cap = _web_cap
    if not cap.enter():
        await reply_busy(writer)
        return
    try:
        outcome = await serve_connect(
            reader, writer, tunnel_opened=_count_tunnel, refusals=_web_refusals
        )
    finally:
        cap.leave()
    if outcome is False:
        refused_total += 1


def active_connections() -> int:
    """Connections the web tools' egress serves now."""
    return _web_cap.active


def _count_tunnel() -> None:
    global tunnels_total
    tunnels_total += 1


async def serve_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    host_allowed: Optional[Callable[[str], bool]] = None,
    label: str = "web egress",
    tunnel_opened: Optional[Callable[[], None]] = None,
    refusals: Optional[RefusalLog] = None,
) -> Optional[bool]:
    """Answer one CONNECT request: tunnel it, or refuse it.

    Port 443 only, public addresses only (resolved once, here), and, when
    ``host_allowed`` is given, only a host it accepts (agy's cage,
    BUG-156). Returns False when the destination was refused (403), True
    once a tunnel ended, None for a malformed request or an upstream that
    could not be reached. ``label`` starts every log line, which names the
    host asked, never a path; refusals go through ``refusals`` (one line
    per host and window).
    """
    if refusals is None:
        refusals = RefusalLog(label)
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT_S)
    except (
        asyncio.TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        ConnectionError,
        OSError,
    ):
        writer.close()
        return None
    if len(head) > _MAX_HEAD:
        await _reply(writer, "431 Request Header Fields Too Large")
        return None
    first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    target = _parse_target(first)
    if target is None:
        await _reply(writer, "405 Method Not Allowed")
        return None
    host, port = target
    if port != _TLS_PORT:
        # No plain HTTP inside a tunnel either: WebFetch speaks HTTPS only.
        refusals.refused(host, f" port={port} is not 443")
        await _reply(writer, "403 Forbidden")
        return False
    if host_allowed is not None and not host_allowed(host):
        refusals.refused(host, " is not on its list")
        await _reply(writer, "403 Forbidden")
        return False
    try:
        addrs = await _public_addresses(host, port)
    except TimeoutError:
        refusals.refused(host, f" port={port} did not resolve in {_DNS_TIMEOUT_S:g} s")
        await _reply(writer, "403 Forbidden")
        return False
    if not addrs:
        # The audit trail of a run: the host asked, never a path.
        refusals.refused(host, f" port={port} is not public")
        await _reply(writer, "403 Forbidden")
        return False
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
        return None
    if tunnel_opened is not None:
        tunnel_opened()
    log.info("%s tunnel: host=%s port=%d", label, host[:253], port)
    try:
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
    except (ConnectionError, OSError):
        writer.close()  # the client left before the 200
        up_writer.close()
        return True
    limit = tunnel_max_seconds()
    try:
        await asyncio.wait_for(
            asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer)),
            limit,
        )
    except TimeoutError:
        log.info(
            "%s tunnel: host=%s port=%d closed at its max duration (%g s)",
            label,
            host[:253],
            port,
            limit,
        )
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()
        up_writer.close()
    return True


async def ensure_started() -> int:
    """Start the proxy once (loopback, a free port) and return its port."""
    global _server, _port, _lock, _web_cap, _web_refusals
    if _server is not None and _port is not None:
        return _port
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _server is None:
            _web_cap = ConnectionCap("web egress")
            _web_refusals = RefusalLog("web egress")
            _server = await asyncio.start_server(
                _handle, host="127.0.0.1", port=0, limit=_MAX_HEAD * 2
            )
            _port = _server.sockets[0].getsockname()[1]
            log.info("web egress guard listening on 127.0.0.1:%d", _port)
    assert _port is not None
    return _port


async def stop() -> None:
    global _server, _port, _dns_pool
    if _server is not None:
        _server.close()
        await _server.wait_closed()
    _web_refusals.flush()
    if _dns_pool is not None:
        # A lookup still hanging ends on its own; none queued is started.
        _dns_pool.shutdown(wait=False, cancel_futures=True)
        _dns_pool = None
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
        # An inherited SOCKS or catch-all proxy never takes over (PLU-160).
        "ALL_PROXY": "",
        "all_proxy": "",
    }
