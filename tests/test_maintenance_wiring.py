"""The maintenance jobs: their settings, their work and ``GET /admin/retention``."""

from __future__ import annotations

import asyncio
import threading

import pytest

from plugboard import maintenance, retention

ADMIN = {"Authorization": "Bearer adminkey"}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Point the five stores and the retention report at ``tmp_path``."""
    from plugboard import audit, monitoring, provider_watchdog, session_store, usage

    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    monkeypatch.setattr(audit, "_DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(provider_watchdog, "_DB_PATH", tmp_path / "watchdog.db")
    monkeypatch.setattr(monitoring, "_DB_PATH", tmp_path / "monitoring.db")
    monkeypatch.setattr(session_store, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(retention, "report_path", lambda: tmp_path / "retention.json")
    for name in (
        "PLUGBOARD_USAGE_RETENTION_MONTHS",
        "PLUGBOARD_AUDIT_RETENTION_DAYS",
        "PLUGBOARD_WATCHDOG_RETENTION_DAYS",
        "PLUGBOARD_MONITORING_RETENTION_DAYS",
        "PLUGBOARD_RETENTION_HOUR_UTC",
        "PLUGBOARD_RETENTION_DISABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_availability_job_reads_its_interval(monkeypatch):
    monkeypatch.delenv("PLUGBOARD_MONITORING_INTERVAL_S", raising=False)
    job = maintenance.availability_job()
    assert job.name == "availability_snapshots"
    assert job.interval_s == 300
    assert job.disabled is False

    monkeypatch.setenv("PLUGBOARD_MONITORING_INTERVAL_S", "0")
    job = maintenance.availability_job()
    assert job.disabled is True
    assert job.interval_s == 300

    monkeypatch.setenv("PLUGBOARD_MONITORING_INTERVAL_S", "5")
    assert maintenance.availability_job().interval_s == 60


def test_retention_job_follows_the_settings(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RETENTION_HOUR_UTC", "2")
    monkeypatch.setenv("PLUGBOARD_RETENTION_DISABLED", "1")
    job = maintenance.retention_job()
    assert job.name == "data_retention"
    assert job.daily_hour_utc == 2
    assert job.disabled is True


async def test_retention_job_runs_the_pass_off_the_event_loop(monkeypatch):
    on_main_thread: list[bool] = []
    report = {"ran_at": "2026-11-12T04:00:00+00:00", "deleted_total": 0}

    def _fake_pass():
        on_main_thread.append(threading.current_thread() is threading.main_thread())
        return report

    monkeypatch.setattr(retention, "run_retention", _fake_pass)
    job = maintenance.retention_job()
    await job.run_now()

    assert on_main_thread == [False]
    assert job.status_dict["last_result"] == report
    assert job.status_dict["last_error"] is None


async def test_retention_job_finishes_the_pass_when_stopped(monkeypatch):
    # A thread cannot be stopped: a cancelled run waits for the pass to end,
    # then the cancellation goes on.
    entered = threading.Event()
    release = threading.Event()
    finished: list[bool] = []

    def _slow_pass():
        entered.set()
        release.wait(5)
        finished.append(True)
        return {"deleted_total": 0}

    monkeypatch.setattr(retention, "run_retention", _slow_pass)
    job = maintenance.retention_job()
    run = asyncio.create_task(job.run_now())
    assert await asyncio.to_thread(entered.wait, 5)

    run.cancel()
    await asyncio.sleep(0.05)
    assert not run.done(), "the run stopped waiting while the pass went on"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert finished == [True]


async def test_admin_retention_needs_admin_and_returns_settings_and_last_run(
    admin_client, stores
):
    res = await admin_client.get("/admin/retention")
    assert res.status_code == 401

    res = await admin_client.get("/admin/retention", headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert body["settings"] == retention.settings()
    assert body["last_run"] is None

    retention.run_retention()
    res = await admin_client.get("/admin/retention", headers=ADMIN)
    assert res.status_code == 200
    assert res.json()["last_run"]["deleted_total"] == 0


async def test_a_pass_with_a_failed_store_is_an_error(stores):
    (stores / "watchdog.db").write_bytes(b"this is not an SQLite database\n" * 200)
    job = maintenance.retention_job()

    await job.run_now()

    assert job.status_dict["last_error"].startswith(
        "RuntimeError: 1 store(s) in error: watchdog"
    )
    report = retention.last_report()
    assert report is not None
    assert report["errors"] == 1
    assert "error" in report["stores"]["watchdog"]


def test_retention_job_description_names_the_stores_it_compacts():
    # Review M-6: it said "then compacts the stores", but usage and sessions
    # are never compacted (BUG-76).
    from plugboard import jobs

    text = jobs._JOBS["data_retention"]["description"]
    assert "compacts the audit, watchdog and monitoring stores" in text
