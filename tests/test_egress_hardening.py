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
