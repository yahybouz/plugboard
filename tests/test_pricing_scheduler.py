"""Tests for plugboard.pricing_scheduler.

The scheduler is fully injectable (update_fn, providers_fn, sleep_fn,
now_fn) so we never touch real network or real time. Each test exercises
one branch of the loop with deterministic fakes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest


# -- helpers ------------------------------------------------------------------


def _fake_clock(start: datetime):
    """A mutable now() function that the test can advance manually."""
    state = {"now": start}

    def now_fn() -> datetime:
        return state["now"]

    def advance(seconds: float) -> None:
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now_fn, advance


class _CountingSleep:
    """Records every sleep duration; lets the test control how long the
    scheduler actually waits between iterations."""

    def __init__(self, *, max_iterations: int = 2):
        self.calls: List[float] = []
        self._iterations = 0
        self._max = max_iterations

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self._iterations += 1
        if self._iterations >= self._max:
            # Exit the loop by raising — the scheduler treats this as a
            # cancellation and propagates.
            raise asyncio.CancelledError()
        # Yield control so other tasks run, but don't actually sleep.
        await asyncio.sleep(0)


# -- tests --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_via_env(monkeypatch):
    """PRICING_SCHEDULER_DISABLED=1 → start() is a no-op, no task created."""
    monkeypatch.setenv("PRICING_SCHEDULER_DISABLED", "1")
    from plugboard.pricing_scheduler import PricingScheduler

    sched = PricingScheduler(
        update_fn=_unused_update,
        providers_fn=lambda: [{"id": "p1"}],
    )
    await sched.start()
    assert sched.task is None


@pytest.mark.asyncio
async def test_bootstrap_runs_once_when_providers_ready(monkeypatch):
    """Happy path: providers loaded → bootstrap calls update_current_pricing."""
    monkeypatch.delenv("PRICING_SCHEDULER_DISABLED", raising=False)
    calls: List[str] = []

    async def fake_update():
        calls.append("update")
        return {"added": 1, "updated": 0, "inherited": 5, "total": 6}

    sleep = _CountingSleep(max_iterations=1)  # exit after the first daily sleep
    from plugboard.pricing_scheduler import PricingScheduler

    sched = PricingScheduler(
        update_fn=fake_update,
        providers_fn=lambda: [{"id": "ollama"}],
        sleep_fn=sleep,
    )
    await sched.start()
    # Wait for the loop to hit its first sleep call.
    await _wait_for_task_done(sched.task)
    assert calls == ["update"], "bootstrap update should fire exactly once"


@pytest.mark.asyncio
async def test_bootstrap_waits_for_providers_then_proceeds(monkeypatch):
    """Providers initially empty: scheduler retries with backoff before
    falling through to the bootstrap update."""
    monkeypatch.delenv("PRICING_SCHEDULER_DISABLED", raising=False)
    calls: Dict[str, int] = {"providers_polls": 0, "updates": 0}

    def providers_fn():
        calls["providers_polls"] += 1
        # Providers appear at the 2nd poll.
        return [{"id": "p1"}] if calls["providers_polls"] >= 2 else []

    async def fake_update():
        calls["updates"] += 1
        return {"added": 0, "updated": 0, "total": 0}

    sleep = _CountingSleep(max_iterations=2)
    from plugboard.pricing_scheduler import PricingScheduler

    sched = PricingScheduler(
        update_fn=fake_update,
        providers_fn=providers_fn,
        sleep_fn=sleep,
    )
    await sched.start()
    await _wait_for_task_done(sched.task)

    assert calls["providers_polls"] >= 2
    assert calls["updates"] == 1


@pytest.mark.asyncio
async def test_bootstrap_proceeds_without_providers_after_max_retries(monkeypatch):
    """If providers never appear, scheduler still runs the bootstrap
    update (with a warning) — better than blocking forever."""
    monkeypatch.delenv("PRICING_SCHEDULER_DISABLED", raising=False)
    monkeypatch.setenv("PRICING_PROVIDERS_READY_MAX_RETRIES", "2")
    update_calls: List[str] = []

    async def fake_update():
        update_calls.append("u")
        return {"total": 0}

    # 2 retries for providers + 1 daily-loop sleep before _CountingSleep raises.
    sleep = _CountingSleep(max_iterations=3)
    from plugboard.pricing_scheduler import PricingScheduler

    sched = PricingScheduler(
        update_fn=fake_update,
        providers_fn=lambda: [],  # never ready
        sleep_fn=sleep,
    )
    await sched.start()
    await _wait_for_task_done(sched.task)
    assert update_calls == ["u"]


@pytest.mark.asyncio
async def test_bootstrap_timeout_recorded_as_failure(monkeypatch):
    """asyncio.TimeoutError on the BOOTSTRAP update increments the CB counter.

    Scope: only the bootstrap path is exercised here (sleep_fn raises after
    the first daily-loop sleep). A separate test would be needed to cover
    timeout behaviour inside the daily loop iterations themselves.
    """
    monkeypatch.setenv("PRICING_UPDATE_TIMEOUT_S", "1")

    async def hanging_update():
        await asyncio.sleep(10)  # would exceed timeout
        return {"total": 0}

    sleep = _CountingSleep(max_iterations=1)
    from plugboard.pricing_scheduler import PricingScheduler

    sched = PricingScheduler(
        update_fn=hanging_update,
        providers_fn=lambda: [{"id": "p"}],
        sleep_fn=sleep,
    )
    await sched.start()
    await _wait_for_task_done(sched.task)
    assert sched._consecutive_failures == 1


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_three_failures(monkeypatch, tmp_path):
    """3 update failures → CB OPEN, subsequent iterations skip the call."""
    monkeypatch.delenv("PRICING_SCHEDULER_DISABLED", raising=False)
    update_calls: List[str] = []

    async def failing_update():
        update_calls.append("u")
        raise RuntimeError("boom")

    # Need to allow 4+ daily ticks; max_iterations=5 keeps the loop alive
    # long enough for all 3 failures + one CB-skip iteration.
    sleep = _CountingSleep(max_iterations=5)
    from plugboard.pricing_scheduler import PricingScheduler

    sched = PricingScheduler(
        update_fn=failing_update,
        providers_fn=lambda: [{"id": "p"}],
        sleep_fn=sleep,
        cb_state_file=tmp_path / "cb_state.json",
    )
    await sched.start()
    await _wait_for_task_done(sched.task)

    # The CB counter is shared across bootstrap + daily iterations: any
    # 3 consecutive failures open it. Here: 1 bootstrap failure + 2 daily
    # failures = 3 → CB opens, subsequent daily iterations skip the call
    # while OPEN (asserted indirectly by the unchanged length of update_calls).
    assert len(update_calls) == 3, f"got {len(update_calls)} updates"
    assert sched._cb_open_until is not None


@pytest.mark.asyncio
async def test_seconds_until_next_run_handles_rollover(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_PRICING_REFRESH_INTERVAL_H", "0")  # daily mode
    """If we're past today's PRICING_DAILY_HOUR_UTC, schedule for tomorrow."""
    from plugboard.pricing_scheduler import PricingScheduler

    now_fn, _advance = _fake_clock(datetime(2026, 4, 25, 5, 0, 0, tzinfo=timezone.utc))
    sched = PricingScheduler(
        update_fn=_unused_update,
        providers_fn=lambda: [{"id": "p"}],
        now_fn=now_fn,
        cb_state_file=tmp_path / "cb_state.json",
    )
    sched.daily_hour_utc = 3  # today's 03:00 already passed
    seconds = sched._seconds_until_next_run()
    # Tomorrow 03:00 = 22h from "now" (5:00). +0..300s jitter (ARCH-DEBT-035).
    assert 22 * 3600 <= seconds <= 22 * 3600 + 301


@pytest.mark.asyncio
async def test_seconds_until_next_run_today(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_PRICING_REFRESH_INTERVAL_H", "0")  # daily mode
    """If we're before today's PRICING_DAILY_HOUR_UTC, schedule for today."""
    from plugboard.pricing_scheduler import PricingScheduler

    now_fn, _advance = _fake_clock(datetime(2026, 4, 25, 1, 0, 0, tzinfo=timezone.utc))
    sched = PricingScheduler(
        update_fn=_unused_update,
        providers_fn=lambda: [{"id": "p"}],
        now_fn=now_fn,
        cb_state_file=tmp_path / "cb_state.json",
    )
    sched.daily_hour_utc = 3  # 2 hours from now
    seconds = sched._seconds_until_next_run()
    assert 2 * 3600 <= seconds <= 2 * 3600 + 301


@pytest.mark.asyncio
async def test_status_dict_reflects_state(monkeypatch, tmp_path):
    """status_dict surfaces CB state for /admin/pricing/scheduler/status."""
    from plugboard.pricing_scheduler import PricingScheduler

    sched = PricingScheduler(
        update_fn=_unused_update,
        providers_fn=lambda: [{"id": "p"}],
        cb_state_file=tmp_path / "cb_state.json",
    )
    s0 = sched.status_dict
    assert s0["cb_state"] == "CLOSED"
    assert s0["cb_consecutive_failures"] == 0
    assert s0["disabled"] is False
    assert s0["running"] is False  # not started

    # Simulate failures to flip the CB.
    sched._record_failure()
    assert sched.status_dict["cb_state"] == "DEGRADED"
    sched._record_failure()
    sched._record_failure()
    s_open = sched.status_dict
    assert s_open["cb_state"] == "OPEN"
    assert s_open["cb_open_until"] is not None


@pytest.mark.asyncio
async def test_wait_providers_ready_short_circuits_on_stop(monkeypatch):
    """stop() during the bootstrap providers-ready loop must exit quickly."""
    monkeypatch.setenv("PRICING_PROVIDERS_READY_MAX_RETRIES", "10")
    monkeypatch.setenv("PRICING_PROVIDERS_READY_BACKOFF_S", "60")  # would block 10min
    from plugboard.pricing_scheduler import PricingScheduler

    poll_count = {"n": 0}

    def providers_fn():
        poll_count["n"] += 1
        return []  # never ready

    async def slow_sleep(seconds: float) -> None:
        # Yield once so the test can flip the stop flag.
        await asyncio.sleep(0)

    sched = PricingScheduler(
        update_fn=_unused_update,
        providers_fn=providers_fn,
        sleep_fn=slow_sleep,
    )
    # Pre-set the stop event then call _wait_providers_ready directly.
    sched._stopping.set()
    ok = await sched._wait_providers_ready()
    assert ok is True  # early-exit returns True
    assert poll_count["n"] == 0, "no provider polls should happen once stopping is set"


@pytest.mark.asyncio
async def test_stop_cancels_loop_cleanly(monkeypatch):
    """stop() during a long sleep terminates without unhandled exceptions."""

    async def slow_sleep(seconds: float) -> None:
        await asyncio.sleep(0.1)  # let the loop yield
        # Keep waiting until cancelled.
        await asyncio.sleep(60)

    async def fake_update():
        return {"total": 0}

    from plugboard.pricing_scheduler import PricingScheduler

    sched = PricingScheduler(
        update_fn=fake_update,
        providers_fn=lambda: [{"id": "p"}],
        sleep_fn=slow_sleep,
    )
    await sched.start()
    await asyncio.sleep(0.2)  # let bootstrap fire
    await sched.stop()
    assert sched.task is None


# -- helpers ------------------------------------------------------------------


async def _unused_update() -> Dict[str, Any]:  # pragma: no cover
    raise AssertionError("update_fn should not be called in this test")


async def _wait_for_task_done(task: asyncio.Task, timeout: float = 5.0) -> None:
    """Wait until the loop task finishes (it exits when CancelledError fires).

    No asyncio.shield: if the test exceeds the timeout we cancel and
    await the task explicitly so it cannot leak into the next test.
    """
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.CancelledError:
        # The scheduler's CancelledError is the expected exit path.
        pass
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


# ── TNRs added for ARCH-DEBT-033/035/036/037 (closed 2026-05-04) ────────────


@pytest.mark.asyncio
async def test_tnr_033_cb_state_persisted_across_restarts(tmp_path):
    """ARCH-DEBT-033: CB state must survive a process restart so an OOM
    crash loop doesn't repeatedly slam a known-bad upstream."""
    from plugboard.pricing_scheduler import PricingScheduler

    state_file = tmp_path / "cb_state.json"
    sched = PricingScheduler(
        update_fn=_unused_update,
        providers_fn=lambda: [{"id": "p"}],
        cb_state_file=state_file,
    )
    sched._record_failure()
    sched._record_failure()
    sched._record_failure()  # opens CB
    assert sched._cb_open_until is not None
    assert state_file.exists(), "CB state must be persisted after CB transitions"

    # Simulate a restart: a fresh instance must load the OPEN state.
    sched2 = PricingScheduler(
        update_fn=_unused_update,
        providers_fn=lambda: [{"id": "p"}],
        cb_state_file=state_file,
    )
    assert sched2._cb_open_until is not None, "CB OPEN must be restored from disk"
    assert sched2._consecutive_failures >= 3


@pytest.mark.asyncio
async def test_tnr_033_cb_state_corrupted_file_starts_clean(tmp_path):
    """A malformed cb_state.json must NOT crash the scheduler — degrade
    to a clean CLOSED start with a warn log."""
    from plugboard.pricing_scheduler import PricingScheduler

    state_file = tmp_path / "cb_state.json"
    state_file.write_text("not json {{")
    sched = PricingScheduler(
        update_fn=_unused_update,
        providers_fn=lambda: [{"id": "p"}],
        cb_state_file=state_file,
    )
    assert sched._consecutive_failures == 0
    assert sched._cb_open_until is None


@pytest.mark.asyncio
async def test_tnr_036_failure_counter_capped(tmp_path):
    """ARCH-DEBT-036: counter must be bounded to prevent integer overflow
    on a pathological long failure run."""
    from plugboard.pricing_scheduler import (
        PricingScheduler,
        _FAILURE_COUNTER_CAP,
    )

    sched = PricingScheduler(
        update_fn=_unused_update,
        providers_fn=lambda: [{"id": "p"}],
        cb_state_file=tmp_path / "cb_state.json",
    )
    for _ in range(_FAILURE_COUNTER_CAP + 100):
        sched._record_failure()
    assert sched._consecutive_failures == _FAILURE_COUNTER_CAP
