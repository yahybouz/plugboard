"""Tests for plugboard.maintenance.PeriodicJob.

Nothing sleeps for real: every job gets a fake ``sleep_fn`` that records
the requested durations and a fake ``now_fn``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import pytest

from plugboard.maintenance import PeriodicJob

_NOW = datetime(2026, 11, 12, 22, 30, 0, tzinfo=timezone.utc)


def _clock(value: datetime = _NOW) -> Callable[[], datetime]:
    return lambda: value


class _FakeSleep:
    """Records each duration; raises CancelledError on call number ``stop_after``."""

    def __init__(
        self, stop_after: int, on_call: Optional[Callable[[], None]] = None
    ) -> None:
        self.calls: List[float] = []
        self._stop_after = stop_after
        self._on_call = on_call

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._on_call is not None:
            self._on_call()
        if len(self.calls) >= self._stop_after:
            raise asyncio.CancelledError()
        await asyncio.sleep(0)


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


async def _run_loop_to_the_end(job: PeriodicJob) -> None:
    await job.start()
    assert job.task is not None
    with pytest.raises(asyncio.CancelledError):
        await job.task


async def test_interval_job_waits_then_runs_every_interval():
    calls: List[int] = []

    async def work() -> Dict[str, Any]:
        calls.append(1)
        return {"ok": True}

    sleep = _FakeSleep(stop_after=3)
    job = PeriodicJob(
        "snapshots", work, interval_s=300, sleep_fn=sleep, now_fn=_clock()
    )
    await _run_loop_to_the_end(job)

    assert sleep.calls == [60.0, 300, 300]
    assert len(calls) == 2
    assert job.status_dict["last_result"] == {"ok": True}
    assert job.status_dict["last_success_at"] == _NOW.isoformat()


async def test_daily_job_sleeps_until_the_next_hour_utc():
    async def work() -> Dict[str, Any]:
        return {}

    sleep = _FakeSleep(stop_after=1)
    job = PeriodicJob(
        "retention", work, daily_hour_utc=4, sleep_fn=sleep, now_fn=_clock()
    )
    await _run_loop_to_the_end(job)
    assert sleep.calls == [19800]

    on_the_hour = datetime(2026, 11, 12, 4, 0, 0, tzinfo=timezone.utc)
    sleep = _FakeSleep(stop_after=1)
    job = PeriodicJob(
        "retention",
        work,
        daily_hour_utc=4,
        sleep_fn=sleep,
        now_fn=_clock(on_the_hour),
    )
    await _run_loop_to_the_end(job)
    assert sleep.calls == [86400]


async def test_a_failing_run_is_recorded_and_the_loop_goes_on():
    attempts: List[int] = []

    async def work() -> Dict[str, Any]:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return {"deleted": 3}

    seen_errors: List[Optional[str]] = []
    job: Optional[PeriodicJob] = None

    def snapshot() -> None:
        assert job is not None
        seen_errors.append(job.status_dict["last_error"])

    sleep = _FakeSleep(stop_after=3, on_call=snapshot)
    job = PeriodicJob(
        "retention", work, interval_s=300, sleep_fn=sleep, now_fn=_clock()
    )
    await _run_loop_to_the_end(job)

    # Snapshots are taken before each sleep: none, after run 1, after run 2.
    assert seen_errors[1] == "RuntimeError: boom"
    status = job.status_dict
    assert status["last_error"] is None
    assert status["runs"] == 2
    assert status["last_result"] == {"deleted": 3}
    assert status["last_run_at"] == _NOW.isoformat()


async def test_disabled_job_does_not_start():
    async def work() -> Dict[str, Any]:
        return {}

    job = PeriodicJob(
        "retention",
        work,
        daily_hour_utc=4,
        disabled=True,
        sleep_fn=_FakeSleep(stop_after=1),
        now_fn=_clock(),
    )
    await job.start()

    assert job.task is None
    status = job.status_dict
    assert status["disabled"] is True
    assert status["running"] is False
    assert set(status) == {
        "name",
        "disabled",
        "running",
        "interval_s",
        "daily_hour_utc",
        "refresh_interval_h",
        "running_since",
        "started_at",
        "last_run_at",
        "last_success_at",
        "last_error",
        "last_result",
        "runs",
    }
    assert status["refresh_interval_h"] == 0


async def test_run_now_refuses_a_second_run_in_flight():
    release = asyncio.Event()

    async def work() -> Dict[str, Any]:
        await release.wait()
        return {"ok": True}

    job = PeriodicJob(
        "retention", work, daily_hour_utc=4, sleep_fn=_FakeSleep(1), now_fn=_clock()
    )
    first = asyncio.create_task(job.run_now("manual"))
    await _wait_until(lambda: job.running_since is not None)
    assert job.running_since == _NOW.isoformat()

    with pytest.raises(RuntimeError, match="already running"):
        await job.run_now("manual")

    release.set()
    await first
    assert job.running_since is None
    assert job.status_dict["runs"] == 1


async def test_run_now_works_on_a_disabled_job():
    async def work() -> Dict[str, Any]:
        return {"deleted": 0}

    job = PeriodicJob(
        "retention",
        work,
        daily_hour_utc=4,
        disabled=True,
        sleep_fn=_FakeSleep(1),
        now_fn=_clock(),
    )
    await job.run_now()

    assert job.status_dict["last_result"] == {"deleted": 0}
    assert job.task is None


async def test_stop_cancels_the_loop():
    async def work() -> Dict[str, Any]:
        return {}

    async def sleep_forever(_seconds: float) -> None:
        await asyncio.Event().wait()

    job = PeriodicJob(
        "snapshots", work, interval_s=300, sleep_fn=sleep_forever, now_fn=_clock()
    )
    await job.start()
    assert job.status_dict["running"] is True
    assert job.status_dict["started_at"] == _NOW.isoformat()

    await job.stop()

    assert job.status_dict["running"] is False
    assert job.task is None


async def test_stop_during_a_run_records_no_error():
    async def work() -> Dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    job = PeriodicJob(
        "snapshots", work, interval_s=300, sleep_fn=_FakeSleep(99), now_fn=_clock()
    )
    await job.start()
    await _wait_until(lambda: job.running_since is not None)

    await job.stop()

    status = job.status_dict
    assert status["running"] is False
    assert status["running_since"] is None
    assert status["last_error"] is None
    assert status["runs"] == 0


async def test_a_scheduled_run_is_skipped_while_a_manual_run_is_in_flight():
    release = asyncio.Event()
    calls: List[int] = []

    async def work() -> Dict[str, Any]:
        calls.append(1)
        await release.wait()
        return {"ok": True}

    sleep = _FakeSleep(stop_after=2)
    job = PeriodicJob(
        "snapshots", work, interval_s=300, sleep_fn=sleep, now_fn=_clock()
    )
    manual = asyncio.create_task(job.run_now("manual"))
    await _wait_until(lambda: job.running_since is not None)

    # The loop wakes once (60 s) while the manual run holds the guard.
    await _run_loop_to_the_end(job)
    assert sleep.calls == [60.0, 300]
    assert len(calls) == 1

    release.set()
    await manual
    status = job.status_dict
    assert status["runs"] == 1
    assert status["last_error"] is None


def test_constructor_needs_exactly_one_schedule():
    async def work() -> Dict[str, Any]:
        return {}

    with pytest.raises(ValueError):
        PeriodicJob("nothing", work)
    with pytest.raises(ValueError):
        PeriodicJob("both", work, interval_s=300, daily_hour_utc=4)
    with pytest.raises(ValueError):
        PeriodicJob("bad hour", work, daily_hour_utc=24)


async def test_a_missed_daily_run_is_caught_up_after_the_first_delay():
    # BUG-72: a server down at the daily hour waited a whole day more.
    calls: List[int] = []

    async def work() -> Dict[str, Any]:
        calls.append(1)
        return {}

    sleep = _FakeSleep(stop_after=2)
    job = PeriodicJob(
        "retention",
        work,
        daily_hour_utc=4,
        sleep_fn=sleep,
        now_fn=_clock(),
        catch_up=lambda: True,
    )
    await _run_loop_to_the_end(job)
    assert sleep.calls == [60.0, 19800]
    assert calls == [1]


async def test_no_catch_up_when_the_last_run_is_recent():
    async def work() -> Dict[str, Any]:
        return {}

    sleep = _FakeSleep(stop_after=1)
    job = PeriodicJob(
        "retention",
        work,
        daily_hour_utc=4,
        sleep_fn=sleep,
        now_fn=_clock(),
        catch_up=lambda: False,
    )
    await _run_loop_to_the_end(job)
    assert sleep.calls == [19800]


async def test_a_guard_that_raises_holds_the_run_and_keeps_the_loop():
    # M-1: an exception in the guard killed the job's loop for good.
    calls: List[int] = []

    async def work() -> Dict[str, Any]:
        calls.append(1)
        return {}

    def guard() -> str:
        raise OSError("disk")

    sleep = _FakeSleep(stop_after=3)
    job = PeriodicJob(
        "retention",
        work,
        interval_s=300,
        sleep_fn=sleep,
        now_fn=_clock(),
        scheduled_guard=guard,
    )
    await _run_loop_to_the_end(job)
    assert calls == []
    assert len(sleep.calls) == 3  # the loop went on after each held run
    assert "OSError" in job.status_dict["last_error"]


async def test_a_catch_up_check_that_raises_waits_for_the_daily_hour():
    async def work() -> Dict[str, Any]:
        return {}

    def catch_up() -> bool:
        raise ValueError("bad report")

    sleep = _FakeSleep(stop_after=1)
    job = PeriodicJob(
        "retention",
        work,
        daily_hour_utc=4,
        sleep_fn=sleep,
        now_fn=_clock(),
        catch_up=catch_up,
    )
    await _run_loop_to_the_end(job)
    assert sleep.calls == [19800]
