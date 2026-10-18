"""Unit tests for the shared aiohttp ClientSession (PERF-2).

Covers the three invariants we care about:
  1. Singleton reuse — same session across sequential callers.
  2. Double-check locking — N concurrent first-callers all get the
     same session (one build, not N).
  3. Idempotent close — calling close_session() twice is safe, and
     get_session() after close rebuilds a fresh session.
"""

from __future__ import annotations

import asyncio

import pytest

from plugboard import http_pool


@pytest.fixture(autouse=True)
async def _reset_pool():
    """Always start each test with a clean slate."""
    await http_pool.reset_for_tests()
    yield
    await http_pool.reset_for_tests()


@pytest.mark.asyncio
async def test_singleton_reuse_sequential():
    """Two sequential get_session() calls return the same object."""
    s1 = await http_pool.get_session()
    s2 = await http_pool.get_session()
    assert s1 is s2
    assert not s1.closed


@pytest.mark.asyncio
async def test_double_check_lock_under_concurrency():
    """When N tasks hit get_session() concurrently, only one session is built.

    The double-check pattern means the fast path skips the lock when
    _session is already set — but on the very first call, multiple
    tasks can race past the initial `is None` check. The async lock
    + re-check inside guarantees a single build.
    """
    # Spawn 20 tasks that all call get_session() before any has a chance
    # to complete — gather forces concurrent scheduling.
    tasks = [asyncio.create_task(http_pool.get_session()) for _ in range(20)]
    sessions = await asyncio.gather(*tasks)
    # All 20 refer to the exact same ClientSession instance.
    first = sessions[0]
    assert all(s is first for s in sessions)
    assert not first.closed


@pytest.mark.asyncio
async def test_close_is_idempotent():
    """close_session() twice in a row must not raise."""
    s = await http_pool.get_session()
    assert not s.closed
    await http_pool.close_session()
    # Second close on a None singleton must also be a no-op.
    await http_pool.close_session()


@pytest.mark.asyncio
async def test_get_session_after_close_rebuilds():
    """After close, a subsequent get_session() returns a fresh session.

    Without this, a FastAPI lifespan that closed the pool during a
    graceful restart would leave the router wedged with a closed
    session forever.
    """
    s1 = await http_pool.get_session()
    await http_pool.close_session()
    assert s1.closed
    s2 = await http_pool.get_session()
    assert s2 is not s1
    assert not s2.closed


@pytest.mark.asyncio
async def test_closed_session_triggers_rebuild(monkeypatch):
    """If the singleton exists but was closed outside close_session(),
    get_session() still returns a usable session rather than the dead one."""
    s1 = await http_pool.get_session()
    # Simulate an external close (e.g. aiohttp internal cleanup).
    await s1.close()
    assert s1.closed
    s2 = await http_pool.get_session()
    assert s2 is not s1
    assert not s2.closed
