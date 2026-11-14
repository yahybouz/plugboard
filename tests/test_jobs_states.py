"""A background job that is not working must not read as healthy (INC-2)."""

from __future__ import annotations

import asyncio

import pytest

from plugboard import jobs, provider_watchdog
from plugboard.maintenance import PeriodicJob

pytestmark = pytest.mark.anyio


class _Sched:
    running_since = None

    def __init__(self, **status):
        self._status = {"disabled": False, "refresh_interval_h": 6, **status}

    def status_dict(self):
        return dict(self._status)


class _State:
    pass


def _statuses(**by_attr):
    state = _State()
    for attr, sched in by_attr.items():
        setattr(state, attr, sched)
    return {j["id"]: j for j in jobs.get_jobs_status(state)}


def test_a_loop_that_is_no_longer_alive_reads_stopped():
    got = _statuses(
        pricing_scheduler=_Sched(
            running=False, last_success_at="2026-11-14T06:00:00+00:00"
        )
    )
    assert got["pricing_refresh"]["last_status"] == "stopped"


def test_an_open_or_degraded_circuit_reads_error_despite_an_old_success():
    for cb in ("OPEN", "DEGRADED"):
        got = _statuses(
            pricing_scheduler=_Sched(
                running=True, cb_state=cb, last_success_at="2026-11-14T06:00:00+00:00"
            )
        )
        assert got["pricing_refresh"]["last_status"] == "error", cb


def test_a_healthy_job_still_reads_success():
    got = _statuses(
        pricing_scheduler=_Sched(
            running=True, cb_state="CLOSED", last_success_at="2026-11-14T06:00:00+00:00"
        )
    )
    assert got["pricing_refresh"]["last_status"] == "success"


def test_a_scheduler_the_app_never_started_is_not_called_disabled():
    got = _statuses()
    assert got["model_refresh"]["last_status"] == "not_started"
    assert got["model_refresh"]["schedule"] == "not started"


def test_a_scheduler_off_by_configuration_reads_disabled():
    got = _statuses(pricing_scheduler=_Sched(disabled=True))
    assert got["pricing_refresh"]["last_status"] == "disabled"


def test_a_failing_watchdog_probe_does_not_read_running(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(provider_watchdog, "get_watchdog_status", boom)
    assert jobs.get_jobs_status(None)[2]["last_status"] == "error"


def test_the_watchdog_shows_its_last_check(monkeypatch):
    monkeypatch.setattr(
        provider_watchdog,
        "get_watchdog_status",
        lambda: {
            "running": True,
            "providers": {
                "a": {"last_check_ago_s": 40.0},
                "b": {"last_check_ago_s": 12.5},
            },
        },
    )
    row = jobs.get_jobs_status(None)[2]
    assert row["last_status"] == "running"
    assert row["last_run"] and row["last_run"].endswith("UTC")


async def test_a_crashed_periodic_loop_is_recorded_and_reads_error():
    async def work():
        return {}

    async def broken_sleep(_delay):
        raise RuntimeError("clock broke")

    job = PeriodicJob(
        "availability_snapshots", work, interval_s=60, sleep_fn=broken_sleep
    )
    await job.start()
    await asyncio.sleep(0.05)
    assert job.last_error and "clock broke" in job.last_error
    state = _State()
    state.availability_job = job
    row = {j["id"]: j for j in jobs.get_jobs_status(state)}["availability_snapshots"]
    assert row["last_status"] in {"error", "stopped"}
    await job.stop()


async def test_a_started_periodic_job_that_has_not_run_yet_reads_never_run():
    async def work():
        return {}

    job = PeriodicJob("data_retention", work, daily_hour_utc=4)
    await job.start()
    try:
        state = _State()
        state.retention_job = job
        row = {j["id"]: j for j in jobs.get_jobs_status(state)}["data_retention"]
        assert row["last_status"] == "never_run"
    finally:
        await job.stop()
