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
