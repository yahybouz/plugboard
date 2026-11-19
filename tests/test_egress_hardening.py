"""Egress hardening of 4.15.2: name resolution, tunnel length, refusal log.

No CLI runs, nothing leaves the machine: every name resolution is a fake
(``web_egress._getaddrinfo`` replaced, it still runs on the egress' own
resolver threads) and every upstream is a local socket.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time

import pytest

from plugboard import web_egress


async def _send(port: int, data: bytes, wait: float = 5) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    head = await asyncio.wait_for(reader.read(200), wait)
    writer.close()
    return head


async def _connect(port: int, target: str, wait: float = 5) -> bytes:
    return await _send(port, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode(), wait)


@pytest.fixture
async def guard():
    port = await web_egress.ensure_started()
    try:
        yield port
    finally:
        await web_egress.stop()


def _refused(caplog) -> list:
    return [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]


# ── PLU-170: name resolution on the egress' own bounded pool, 5 s at most ────


def test_resolution_pool_is_bounded_and_times_out_in_5_s():
    assert web_egress._DNS_THREADS == 8
    assert web_egress._DNS_TIMEOUT_S == 5.0


async def test_slow_resolution_spares_the_default_executor_and_times_out(
    guard, monkeypatch, caplog
):
    """Names whose DNS hangs hold the egress' resolver threads, never the
    loop's default executor (billing writes go through asyncio.to_thread),
    and are refused once the resolution delay is over."""
    monkeypatch.setattr(web_egress, "_DNS_TIMEOUT_S", 0.3)
    release = threading.Event()
    threads: list = []

    def hanging(host, port):
        threads.append(threading.current_thread().name)
        release.wait(10)
        raise socket.gaierror(socket.EAI_AGAIN, "slow")

    monkeypatch.setattr(web_egress, "_getaddrinfo", hanging)
    try:
        with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
            asks = [
                asyncio.create_task(_connect(guard, f"slow{i}.example.test:443"))
                for i in range(40)
            ]
            # The default executor still answers at once.
            started = time.monotonic()
            assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), 1) == 42
            assert time.monotonic() - started < 1
            heads = await asyncio.wait_for(asyncio.gather(*asks), 5)
        assert all(h.startswith(b"HTTP/1.1 403") for h in heads), heads
        assert threads and all(t.startswith("egress-dns") for t in threads), threads
        assert len(set(threads)) <= web_egress._DNS_THREADS
        lines = _refused(caplog)
        assert any("did not resolve" in line for line in lines), lines
    finally:
        release.set()


# ── PLU-169: a tunnel lasts at most PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS ──────


@pytest.fixture
def fake_dns(monkeypatch):
    answers: dict = {}

    def getaddrinfo(host, port):
        if host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in answers[host]
        ]

    monkeypatch.setattr(web_egress, "_getaddrinfo", getaddrinfo)
    return answers


async def _echo_server():
    async def echo(reader, writer):
        try:
            while data := await reader.read(1024):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 600.0),
        ("", 600.0),
        ("abc", 600.0),
        ("0", 600.0),
        ("-5", 600.0),
        ("nan", 600.0),
        ("30", 30.0),
        ("1.5", 1.5),
    ],
)
def test_tunnel_max_seconds_setting(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS", raising=False)
    else:
        monkeypatch.setenv("PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS", value)
    assert web_egress.tunnel_max_seconds() == expected


async def test_a_busy_tunnel_is_closed_past_its_max_duration(
    guard, fake_dns, monkeypatch, caplog
):
    """Bytes keep flowing both ways (no idle delay ever runs out), yet the
    tunnel is closed once it is older than the max duration."""
    monkeypatch.setenv("PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS", "0.5")
    upstream, up_port = await _echo_server()
    fake_dns["busy.example.test"] = ["127.0.0.1"]
    monkeypatch.setattr(web_egress.netguard, "ip_is_public", lambda _ip: True)
    monkeypatch.setattr(web_egress, "_TLS_PORT", up_port)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", guard)
        writer.write(f"CONNECT busy.example.test:{up_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert head.startswith(b"HTTP/1.1 200"), head
        started = time.monotonic()
        closed = False
        with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
            while time.monotonic() - started < 5:
                try:
                    writer.write(b"ping")
                    await writer.drain()
                    data = await asyncio.wait_for(reader.read(1024), 1)
                except TimeoutError:
                    pytest.fail("the echo stopped answering, yet the tunnel is open")
                except (ConnectionError, OSError):
                    closed = True
                    break
                if not data:
                    closed = True
                    break
                await asyncio.sleep(0.05)
        elapsed = time.monotonic() - started
        writer.close()
        assert closed, "the tunnel outlived its max duration"
        assert 0.4 <= elapsed < 3, elapsed
        assert any("max duration" in r.getMessage() for r in caplog.records)
    finally:
        upstream.close()
        await upstream.wait_closed()


# ── PLU-167 (+ I3): the refusal log, per host and port, bounded, escaped ─────


async def test_a_second_port_of_a_host_is_logged(guard, monkeypatch, caplog):
    monkeypatch.setattr(web_egress, "_clock", lambda: 1000.0)
    with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
        for target in ("example.com:80", "example.com:80", "example.com:8080"):
            assert (await _connect(guard, target)).startswith(b"HTTP/1.1 403")
    lines = _refused(caplog)
    assert len(lines) == 2, lines
    assert "host=example.com port=80 " in lines[0], lines
    assert "host=example.com port=8080 " in lines[1], lines


def test_the_overflow_line_carries_a_sample_of_8_host_ports(monkeypatch, caplog):
    monkeypatch.setattr(web_egress, "_clock", lambda: 1000.0)
    refusals = web_egress.RefusalLog("test egress")
    with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
        for i in range(web_egress.REFUSAL_LOG_HOSTS):
            refusals.refused(f"h{i}.example.com", 443, " is not public")
        for i in range(20):
            refusals.refused(f"x{i}.example.com", 8000 + i, " is not 443")
        refusals.flush()
    lines = _refused(caplog)
    assert len(lines) <= web_egress.REFUSAL_LOG_HOSTS + 2, len(lines)
    other = [line for line in lines if "<other hosts>" in line]
    assert other, lines[-3:]
    # The first overflow line names the first host it stands for...
    assert "sample=x0.example.com:8000" in other[0], other
    # ...the summary of the rest at most 8 of them, as host:port.
    assert "19 more" in other[-1], other
    sample = other[-1].split("sample=", 1)[1].split()[0].split(",")
    assert len(sample) == web_egress.REFUSAL_LOG_SAMPLE == 8, sample
    assert all(s.startswith("x") and s.rsplit(":", 1)[1].isdigit() for s in sample)


async def test_web_refusal_counts_are_logged_periodically(monkeypatch, caplog):
    """A host refused many times, then never again, has its count logged
    within a window, not only when the egress stops."""
    monkeypatch.setattr(web_egress, "REFUSAL_LOG_WINDOW_S", 0.2)
    port = await web_egress.ensure_started()
    try:
        with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
            for _ in range(5):
                await _connect(port, "flood.example.com:80")
            await asyncio.sleep(0.7)
            lines = _refused(caplog)
            assert any("flood.example.com" in li and "4 more" in li for li in lines), (
                lines
            )
    finally:
        await web_egress.stop()


async def test_refused_hosts_are_logged_escaped_and_bounded(guard, caplog):
    evil = "\x1b[31mred\x07\x7f‮evil"
    with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
        await _send(
            guard, f"CONNECT {evil}:80 HTTP/1.1\r\n\r\n".encode("latin-1", "replace")
        )
        await _send(
            guard, f"CONNECT {'\x1b' * 4000}.example:80 HTTP/1.1\r\n\r\n".encode()
        )
    lines = _refused(caplog)
    assert len(lines) == 2, lines
    for line in lines:
        assert all(0x20 <= ord(c) < 0x7F for c in line), repr(line)
    assert "\\x1b[31mred\\x07\\x7f" in lines[0], lines[0]
    host = lines[1].split("host=", 1)[1].split(" port=")[0]
    assert len(host) <= 253, len(host)


def test_the_log_escapes_what_it_names():
    refusals = web_egress.RefusalLog("test egress")
    assert refusals.key("a\x1b[0m‮b", 443) == ("a\\x1b[0m\\u202eb", 443)


# ── PLU-171 (I2): agy's tunnel carries the TLS name its CONNECT asked ────────


def _client_hello(server_name) -> bytes:
    """The first TLS record a client sends to ``server_name`` (no network:
    a memory BIO)."""
    import ssl

    ctx = ssl.create_default_context()
    if server_name is None:
        ctx.check_hostname = False
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ctx.wrap_bio(incoming, outgoing, server_hostname=server_name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def test_client_hello_sni_reads_the_server_name():
    hello = _client_hello("oauth2.googleapis.com")
    assert web_egress.client_hello_sni(hello) == "oauth2.googleapis.com"
    assert web_egress.client_hello_sni(hello[:-10]) is None  # cut short
    assert web_egress.client_hello_sni(b"GET / HTTP/1.1\r\n\r\n") is None
    assert web_egress.client_hello_sni(b"\x16\x03\x01\xff") is None
    assert web_egress.client_hello_sni(b"") is None


def test_client_hello_without_a_name_has_no_sni():
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ctx.wrap_bio(incoming, outgoing)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    assert web_egress.client_hello_sni(outgoing.read()) is None


async def _recording_upstream():
    got = bytearray()

    async def keep(reader, writer):
        try:
            while data := await reader.read(65536):
                got.extend(data)
                writer.write(b"ok")
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(keep, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], got


async def _tunnel_once(open_conn, target: str, payload: bytes) -> bytes:
    """CONNECT ``target``, send ``payload`` and return what comes back
    before the tunnel closes (b"" when it is closed at once)."""
    reader, writer = await open_conn()
    writer.write(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert head.startswith(b"HTTP/1.1 200"), head
    writer.write(payload)
    await writer.drain()
    try:
        back = await asyncio.wait_for(reader.read(100), 5)
    except (ConnectionError, OSError):
        back = b""
    writer.close()
    return back


@pytest.fixture
async def agy_egress_path(tmp_path):
    from plugboard import agy_egress

    path = str(tmp_path / "e.sock")
    server = await agy_egress.start(path)
    try:
        yield path
    finally:
        await server.close()


async def test_agy_tunnel_passes_a_client_hello_naming_its_host(
    agy_egress_path, fake_dns, monkeypatch
):
    upstream, up_port, got = await _recording_upstream()
    fake_dns["oauth2.googleapis.com"] = ["127.0.0.1"]
    monkeypatch.setattr(web_egress.netguard, "ip_is_public", lambda _ip: True)
    monkeypatch.setattr(web_egress, "_TLS_PORT", up_port)
    try:
        hello = _client_hello("OAuth2.googleapis.com.")
        back = await _tunnel_once(
            lambda: asyncio.open_unix_connection(agy_egress_path),
            f"oauth2.googleapis.com:{up_port}",
            hello,
        )
        assert back == b"ok"
        assert bytes(got) == hello
    finally:
        upstream.close()
        await upstream.wait_closed()


@pytest.mark.parametrize("sent", ["storage.googleapis.com", None, "not tls"])
async def test_agy_tunnel_closes_on_another_tls_name(
    agy_egress_path, fake_dns, monkeypatch, caplog, sent
):
    """SNI fronting: CONNECT a listed name, then a ClientHello for another
    Google name (or none, or no TLS at all): nothing reaches upstream."""
    upstream, up_port, got = await _recording_upstream()
    fake_dns["www.googleapis.com"] = ["127.0.0.1"]
    monkeypatch.setattr(web_egress.netguard, "ip_is_public", lambda _ip: True)
    monkeypatch.setattr(web_egress, "_TLS_PORT", up_port)
    payload = b"\x16\x03\x01\x00\x05hello" if sent == "not tls" else _client_hello(sent)
    try:
        with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
            back = await _tunnel_once(
                lambda: asyncio.open_unix_connection(agy_egress_path),
                f"www.googleapis.com:{up_port}",
                payload,
            )
        assert back == b""
        await asyncio.sleep(0.1)
        assert bytes(got) == b""
        lines = _refused(caplog)
        assert any("www.googleapis.com" in li and "TLS name" in li for li in lines)
    finally:
        upstream.close()
        await upstream.wait_closed()


async def test_web_tunnels_never_read_the_client_hello(guard, fake_dns, monkeypatch):
    upstream, up_port, got = await _recording_upstream()
    fake_dns["docs.example.org"] = ["127.0.0.1"]
    monkeypatch.setattr(web_egress.netguard, "ip_is_public", lambda _ip: True)
    monkeypatch.setattr(web_egress, "_TLS_PORT", up_port)
    try:
        back = await _tunnel_once(
            lambda: asyncio.open_connection("127.0.0.1", guard),
            f"docs.example.org:{up_port}",
            b"plain bytes",
        )
        assert back == b"ok" and bytes(got) == b"plain bytes"
    finally:
        upstream.close()
        await upstream.wait_closed()
