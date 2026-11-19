"""TLS ClientHellos for the egress tests, built in memory (no network)."""

from __future__ import annotations

import ssl
from typing import Iterable, Optional, Sequence, Tuple


def client_hello(server_name: Optional[str], alpn: Sequence[str] = ()) -> bytes:
    """The first TLS record Python's client sends to ``server_name`` (None:
    no SNI); ``alpn`` protocols make it bigger."""
    ctx = ssl.create_default_context()
    if server_name is None:
        ctx.check_hostname = False
    if alpn:
        ctx.set_alpn_protocols(list(alpn))
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ctx.wrap_bio(incoming, outgoing, server_hostname=server_name)
    try:
        tls.do_handshake()
    except ssl.SSLWantReadError:
        pass
    else:  # pragma: no cover - a handshake cannot end without a server
        raise AssertionError("handshake ended without a server")
    return outgoing.read()


def _u16(n: int) -> bytes:
    return n.to_bytes(2, "big")


def sni_extension(names: Iterable[str], trailing: bytes = b"") -> Tuple[int, bytes]:
    """A server_name extension listing ``names`` (``trailing``: bytes after
    the list, inside the extension)."""
    entries = b"".join(b"\x00" + _u16(len(n.encode())) + n.encode() for n in names)
    return 0x0000, _u16(len(entries)) + entries + trailing


def build_client_hello(extensions: Iterable[Tuple[int, bytes]]) -> bytes:
    """A minimal ClientHello record carrying ``extensions`` as given."""
    exts = b"".join(_u16(kind) + _u16(len(data)) + data for kind, data in extensions)
    body = (
        b"\x03\x03"  # client_version
        + bytes(32)  # random
        + b"\x00"  # session id
        + _u16(2)
        + b"\x13\x01"  # one cipher suite
        + b"\x01\x00"  # one compression method: null
        + _u16(len(exts))
        + exts
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + _u16(len(handshake)) + handshake
