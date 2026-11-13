"""Tests for the data retention pass (plugboard/retention.py)."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)

_VARIABLES = (
    "PLUGBOARD_USAGE_RETENTION_MONTHS",
    "PLUGBOARD_AUDIT_RETENTION_DAYS",
    "PLUGBOARD_WATCHDOG_RETENTION_DAYS",
    "PLUGBOARD_MONITORING_RETENTION_DAYS",
    "PLUGBOARD_RETENTION_HOUR_UTC",
    "PLUGBOARD_RETENTION_DISABLED",
)
_RETENTIONS = _VARIABLES[:4]


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """Point the five stores and the report at ``tmp_path``, for this test only."""
    from plugboard import (
        audit,
        monitoring,
        provider_watchdog,
        retention,
        session_store,
        usage,
    )

    paths = {
        "usage": tmp_path / "usage.db",
        "audit": tmp_path / "audit.db",
        "watchdog": tmp_path / "watchdog.db",
        "monitoring": tmp_path / "monitoring.db",
        "sessions": tmp_path / "sessions.db",
        "report": tmp_path / "retention.json",
    }
    monkeypatch.setattr(usage, "_DB_PATH", paths["usage"])
    monkeypatch.setattr(usage, "_schema_ready", {})
    monkeypatch.setattr(audit, "_DB_PATH", paths["audit"])
    monkeypatch.setattr(provider_watchdog, "_DB_PATH", paths["watchdog"])
    monkeypatch.setattr(monitoring, "_DB_PATH", paths["monitoring"])
    monkeypatch.setattr(session_store, "_DB_PATH", paths["sessions"])
    monkeypatch.setattr(session_store, "_INITIALISED", False)
    monkeypatch.setattr(retention, "report_path", lambda: paths["report"])
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return paths


# --- seeding helpers -------------------------------------------------------


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values())
    )


def _seed(get_conn, table: str, rows: list) -> None:
    conn = get_conn()
    try:
        for row in rows:
            _insert(conn, table, row)
        conn.commit()
    finally:
        conn.close()


def _audit_row(row_id: str, ts: str) -> dict:
    return {"id": row_id, "ts": ts, "action": "test.action"}


def _event_row(row_id: str, ts: str, details: str = "") -> dict:
    return {
        "id": row_id,
        "ts": ts,
        "provider_id": "p1",
        "event_type": "health_check",
        "details": details,
    }


def _snapshot_row(row_id: str, ts: str) -> dict:
    return {"id": row_id, "ts": ts, "provider_id": "p1"}


def _usage_row(row_id: str, month: str) -> dict:
    return {
        "id": row_id,
        "ts": "2026-11-11T00:00:00.000Z",
        "month": month,
        "client_id": "c1",
        "model": "m1",
        "provider_id": "p1",
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "pricing_month": month,
        "cost_usd": 0.0,
    }


def seed_audit(rows):
    from plugboard import audit

    _seed(audit._get_conn, "audit_log", rows)


def seed_watchdog(rows):
    from plugboard import provider_watchdog

    _seed(provider_watchdog._get_conn, "watchdog_events", rows)


def seed_monitoring(rows):
    from plugboard import monitoring

    _seed(monitoring._get_conn, "monitoring_snapshots", rows)


def seed_usage(rows):
    from plugboard import usage

    _seed(usage._get_conn, "usage_entries", rows)


def seed_session(session_id: str, updated_at: float) -> None:
    from plugboard import session_store

    session_store.save_session("alice", session_id, [{"role": "user", "content": "x"}])
    conn = session_store._get_conn()
    try:
        conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
            (updated_at, session_id),
        )
        conn.commit()
    finally:
        conn.close()


def column(path, table: str, name: str) -> list:
    conn = sqlite3.connect(str(path))
    try:
        return sorted(r[0] for r in conn.execute(f"SELECT {name} FROM {table}"))
    finally:
        conn.close()


def seed_one_old_and_one_recent_row_everywhere() -> None:
    # Cutoffs from NOW: usage 2025_10, audit 2025-11-12, watchdog 2026-10-29,
    # monitoring 2026-08-14, sessions 24 h before the real clock.
    seed_usage([_usage_row("u-old", "2025_09"), _usage_row("u-new", "2026_11")])
    seed_audit(
        [
            _audit_row("a-old", "2025-11-01T00:00:00.000Z"),
            _audit_row("a-new", "2026-11-11T00:00:00.000Z"),
        ]
    )
    seed_watchdog(
        [
            _event_row("w-old", "2026-10-01T00:00:00.000Z"),
            _event_row("w-new", "2026-11-11T00:00:00.000Z"),
        ]
    )
    seed_monitoring(
        [
            _snapshot_row("m-old", "2026-07-01T00:00:00.000Z"),
            _snapshot_row("m-new", "2026-11-11T00:00:00.000Z"),
        ]
    )
    seed_session("s-old", time.time() - 2 * 86400)
    seed_session("s-new", time.time())


# --- tests -----------------------------------------------------------------


def test_defaults(stores):
    from plugboard import retention

    assert retention.settings() == {
        "disabled": False,
        "hour_utc": 4,
        "usage_months": 13,
        "audit_days": 365,
        "watchdog_days": 14,
        "monitoring_days": 90,
        "session_ttl_s": 86400,
    }


def test_run_deletes_only_rows_older_than_each_retention(stores):
    from plugboard import retention

    seed_one_old_and_one_recent_row_everywhere()

    report = retention.run_retention(NOW)

    assert column(stores["usage"], "usage_entries", "id") == ["u-new"]
    assert column(stores["watchdog"], "watchdog_events", "id") == ["w-new"]
    assert column(stores["monitoring"], "monitoring_snapshots", "id") == ["m-new"]
    assert column(stores["sessions"], "chat_sessions", "session_id") == ["s-new"]
    # audit_log also holds the retention.run line this pass has just written.
    audit_ids = column(stores["audit"], "audit_log", "id")
    assert "a-old" not in audit_ids
    assert "a-new" in audit_ids
    assert min(column(stores["audit"], "audit_log", "ts")) >= "2025-11-12T04:00:00"
    assert report["deleted_total"] == 5
    assert report["errors"] == 0
    for name in ("usage", "audit", "watchdog", "monitoring", "sessions"):
        assert report["stores"][name]["deleted"] == 1


def test_usage_cutoff_crosses_the_year_and_keeps_the_boundary_month(stores):
    from plugboard import retention

    seed_usage(
        [
            _usage_row("nov", "2024_11"),
            _usage_row("dec", "2024_12"),
            _usage_row("jan", "2026_01"),
        ]
    )

    retention.run_retention(datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc))

    assert column(stores["usage"], "usage_entries", "month") == ["2024_12", "2026_01"]


def test_malformed_usage_months_are_never_deleted(stores):
    from plugboard import retention

    seed_usage(
        [
            _usage_row("empty", ""),
            _usage_row("dash", "2026-11"),
            _usage_row("text", "abc"),
            _usage_row("old", "2020_01"),
        ]
    )

    report = retention.run_retention(NOW)

    assert column(stores["usage"], "usage_entries", "month") == ["", "2026-11", "abc"]
    assert report["stores"]["usage"]["deleted"] == 1


def test_zero_keeps_everything(stores, monkeypatch):
    from plugboard import retention

    for name in _RETENTIONS:
        monkeypatch.setenv(name, "0")
    seed_usage([_usage_row("u-old", "2001_01")])
    seed_audit([_audit_row("a-old", "2001-01-01T00:00:00.000Z")])
    seed_watchdog([_event_row("w-old", "2001-01-01T00:00:00.000Z")])
    seed_monitoring([_snapshot_row("m-old", "2001-01-01T00:00:00.000Z")])

    report = retention.run_retention(NOW)

    assert report["deleted_total"] == 0
    assert column(stores["usage"], "usage_entries", "id") == ["u-old"]
    assert column(stores["audit"], "audit_log", "id") == ["a-old"]
    assert column(stores["watchdog"], "watchdog_events", "id") == ["w-old"]
    assert column(stores["monitoring"], "monitoring_snapshots", "id") == ["m-old"]
    for name in ("usage", "audit", "watchdog", "monitoring"):
        assert report["stores"][name]["kept"] == "all"


def test_unusable_values_never_delete_more(stores, monkeypatch):
    from plugboard import retention

    for raw in ("-5", "abc"):
        for name in _RETENTIONS:
            monkeypatch.setenv(name, raw)
        settings = retention.settings()
        assert settings["usage_months"] == retention.DEFAULT_USAGE_MONTHS
        assert settings["audit_days"] == retention.DEFAULT_AUDIT_DAYS
        assert settings["watchdog_days"] == retention.DEFAULT_WATCHDOG_DAYS
        assert settings["monitoring_days"] == retention.DEFAULT_MONITORING_DAYS

    seed_usage([_usage_row("u-old", "2000_01")])
    seed_audit([_audit_row("a-old", "2000-01-01T00:00:00.000Z")])
    seed_watchdog([_event_row("w-old", "2000-01-01T00:00:00.000Z")])
    seed_monitoring([_snapshot_row("m-old", "2000-01-01T00:00:00.000Z")])
    # 999999999 reaches before year 1; 739000 days (about 2023 years) lands
    # in year 3, a cutoff that must still compare as older than year 2000
    # ("3-01-..." without its leading zeros would compare as newer).
    for raw in ("999999999", "739000"):
        for name in _RETENTIONS:
            monkeypatch.setenv(name, raw)

        report = retention.run_retention(NOW)

        assert report["deleted_total"] == 0
        assert report["errors"] == 0
    assert column(stores["usage"], "usage_entries", "id") == ["u-old"]
    assert column(stores["audit"], "audit_log", "id") == ["a-old"]
    assert column(stores["watchdog"], "watchdog_events", "id") == ["w-old"]
    assert column(stores["monitoring"], "monitoring_snapshots", "id") == ["m-old"]


def test_one_broken_store_does_not_stop_the_others(stores):
    from plugboard import retention

    stores["watchdog"].write_bytes(b"this is not an SQLite database\n" * 200)
    seed_audit(
        [
            _audit_row("a-old", "2025-11-01T00:00:00.000Z"),
            _audit_row("a-new", "2026-11-11T00:00:00.000Z"),
        ]
    )

    report = retention.run_retention(NOW)

    assert report["stores"]["watchdog"]["error"].startswith("DatabaseError: ")
    assert len(report["stores"]["watchdog"]["error"]) <= 200
    assert report["errors"] == 1
    assert "a-old" not in column(stores["audit"], "audit_log", "id")
    assert report["stores"]["audit"]["deleted"] == 1
    assert stores["report"].is_file()


def test_missing_stores_create_no_file(stores):
    from plugboard import retention

    report = retention.run_retention(NOW)

    assert report["deleted_total"] == 0
    assert report["errors"] == 0
    for name in ("usage", "audit", "watchdog", "monitoring", "sessions"):
        assert not stores[name].exists()


def test_report_is_written_and_read_back(stores):
    from plugboard import retention

    assert retention.last_report() is None

    report = retention.run_retention(NOW)

    assert report["ran_at"] == "2026-11-12T04:00:00Z"
    assert retention.last_report() == report
    assert stat.S_IMODE(os.stat(stores["report"]).st_mode) == 0o600


def test_audit_line_when_rows_were_deleted(stores):
    from plugboard import audit, retention

    retention.run_retention(NOW)
    assert audit.get_audit_log(action="retention.run") == []

    seed_watchdog([_event_row("w-old", "2026-10-01T00:00:00.000Z")])
    retention.run_retention(NOW)

    entries = audit.get_audit_log(action="retention.run")
    assert len(entries) == 1
    assert entries[0]["actor"] == "system"
    assert entries[0]["details"] == "usage=0 audit=0 watchdog=1 monitoring=0 sessions=0"


def test_hour_is_clamped_and_disabled_is_read(stores, monkeypatch):
    from plugboard import retention

    monkeypatch.setenv("PLUGBOARD_RETENTION_HOUR_UTC", "99")
    assert retention.settings()["hour_utc"] == 23

    for raw in ("1", "true", "yes", "TRUE"):
        monkeypatch.setenv("PLUGBOARD_RETENTION_DISABLED", raw)
        assert retention.settings()["disabled"] is True
    monkeypatch.setenv("PLUGBOARD_RETENTION_DISABLED", "0")
    assert retention.settings()["disabled"] is False


def test_compaction_of_a_real_wal_store(stores):
    from plugboard import provider_watchdog, retention

    conn = provider_watchdog._get_conn()  # created by the module itself: WAL
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        for i in range(3000):
            _insert(
                conn,
                "watchdog_events",
                _event_row(f"old-{i}", "2026-10-01T00:00:00.000Z", "x" * 500),
            )
        _insert(
            conn, "watchdog_events", _event_row("recent", "2026-11-11T00:00:00.000Z")
        )
        conn.commit()
    finally:
        conn.close()
    size_before = stores["watchdog"].stat().st_size

    report = retention.run_retention(NOW)

    assert report["stores"]["watchdog"]["deleted"] == 3000
    assert report["stores"]["watchdog"]["compacted"] is True
    assert stores["watchdog"].stat().st_size < size_before
    assert column(stores["watchdog"], "watchdog_events", "id") == ["recent"]


def test_a_failed_compaction_keeps_the_deleted_count(stores, monkeypatch):
    from plugboard import audit, retention, storage

    real_compact = storage.compact_sqlite

    def locked(path, *args, **kwargs):
        if path == stores["watchdog"]:
            raise sqlite3.OperationalError("database is locked")
        return real_compact(path, *args, **kwargs)

    monkeypatch.setattr(storage, "compact_sqlite", locked)
    seed_watchdog([_event_row("w-old", "2026-10-01T00:00:00.000Z")])

    report = retention.run_retention(NOW)

    watchdog = report["stores"]["watchdog"]
    assert watchdog["deleted"] == 1
    assert watchdog["compacted"] is False
    assert watchdog["error"] == "OperationalError: database is locked"
    assert report["deleted_total"] == 1
    assert report["errors"] == 1
    assert len(audit.get_audit_log(action="retention.run")) == 1


def test_a_failure_after_committed_batches_keeps_their_count(stores, monkeypatch):
    from plugboard import audit, retention, storage

    seed_watchdog(
        [_event_row(f"w-old-{i}", "2026-10-01T00:00:00.000Z") for i in range(5003)]
        + [_event_row("w-new", "2026-11-11T00:00:00.000Z")]
    )
    real_batch = storage._delete_batch
    watchdog_batches = []

    def locked_on_the_second_batch(conn, table, *args, **kwargs):
        if table == "watchdog_events":
            watchdog_batches.append(table)
            if len(watchdog_batches) == 2:
                raise sqlite3.OperationalError("database is locked")
        return real_batch(conn, table, *args, **kwargs)

    monkeypatch.setattr(storage, "_delete_batch", locked_on_the_second_batch)

    report = retention.run_retention(NOW)

    watchdog = report["stores"]["watchdog"]
    assert watchdog["deleted"] == 5000  # the first batch, committed
    assert watchdog["compacted"] is False
    assert watchdog["error"] == "OperationalError: database is locked"
    assert report["deleted_total"] == 5000
    assert report["errors"] == 1
    assert len(column(stores["watchdog"], "watchdog_events", "id")) == 4
    entries = audit.get_audit_log(action="retention.run")
    assert len(entries) == 1
    assert "watchdog=5000" in entries[0]["details"]


# --- BUG-70: a clock running ahead -------------------------------------------


def _last_pass_at(stores, when: datetime) -> None:
    import json

    stores["report"].write_text(
        json.dumps({"ran_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "stores": {}})
    )


def test_clock_check_refuses_a_pass_long_after_the_last_one(stores):
    from plugboard import retention

    _last_pass_at(stores, NOW - timedelta(days=1))
    assert retention.clock_check(NOW) is None
    _last_pass_at(stores, NOW - timedelta(days=retention.MAX_CLOCK_STEP_DAYS + 1))
    message = retention.clock_check(NOW)
    assert message and "clock" in message and "Jobs" in message


def test_clock_check_passes_without_a_previous_pass(stores):
    from plugboard import retention

    assert retention.clock_check(NOW) is None


async def test_scheduled_pass_with_a_clock_far_ahead_deletes_nothing(stores):
    # A server clock a year ahead would have emptied every store.
    from plugboard import maintenance, retention

    seed_one_old_and_one_recent_row_everywhere()
    before = {name: column(stores[name], *spec) for name, spec in _COLUMNS.items()}
    _last_pass_at(stores, datetime.now(timezone.utc) - timedelta(days=365))
    job = maintenance.retention_job()
    await job._run_once("scheduled")
    assert "clock" in job.status_dict["last_error"]
    assert {
        name: column(stores[name], *spec) for name, spec in _COLUMNS.items()
    } == before
    assert retention.last_report()["stores"] == {}  # the last pass is unchanged


async def test_a_manual_pass_is_not_held_by_the_clock_check(stores):
    from plugboard import maintenance, retention

    _last_pass_at(stores, datetime.now(timezone.utc) - timedelta(days=365))
    job = maintenance.retention_job()
    await job.run_now("manual")
    assert job.status_dict["last_error"] is None
    assert retention.last_report()["stores"]  # a new report was written


_COLUMNS = {
    "usage": ("usage_entries", "id"),
    "audit": ("audit_log", "id"),
    "watchdog": ("watchdog_events", "id"),
    "monitoring": ("monitoring_snapshots", "id"),
}


# --- BUG-71: floors on the configured retentions -----------------------------


@pytest.mark.parametrize(
    "variable, key, value, floor",
    [
        ("PLUGBOARD_AUDIT_RETENTION_DAYS", "audit_days", "12", 90),
        ("PLUGBOARD_USAGE_RETENTION_MONTHS", "usage_months", "1", 3),
        ("PLUGBOARD_WATCHDOG_RETENTION_DAYS", "watchdog_days", "2", 7),
        ("PLUGBOARD_MONITORING_RETENTION_DAYS", "monitoring_days", "1", 7),
    ],
)
def test_a_retention_below_its_floor_is_raised_to_it(
    stores, monkeypatch, caplog, variable, key, value, floor
):
    import logging

    from plugboard import retention

    monkeypatch.setattr(retention, "_floor_warned", set())
    monkeypatch.setenv(variable, value)
    with caplog.at_level(logging.WARNING, logger="plugboard.retention"):
        assert retention.settings()[key] == floor
    assert any(variable in r.getMessage() for r in caplog.records)


def test_zero_still_keeps_everything_and_the_floor_holds_rows(stores, monkeypatch):
    from plugboard import retention

    monkeypatch.setenv("PLUGBOARD_AUDIT_RETENTION_DAYS", "0")
    assert retention.settings()["audit_days"] == 0
    monkeypatch.setenv("PLUGBOARD_AUDIT_RETENTION_DAYS", "12")
    seed_audit([_audit_row("a30", (NOW - timedelta(days=30)).isoformat())])
    retention.run_retention(NOW)
    assert column(stores["audit"], "audit_log", "id") == ["a30"]


# --- BUG-72: catching up a missed daily pass ---------------------------------


def test_a_pass_is_overdue_a_day_after_the_last_one(stores):
    from plugboard import retention

    assert retention.pass_overdue(NOW) is False  # never ran: wait for the hour
    _last_pass_at(stores, NOW - timedelta(hours=23))
    assert retention.pass_overdue(NOW) is False
    _last_pass_at(stores, NOW - timedelta(hours=26))
    assert retention.pass_overdue(NOW) is True


def test_the_retention_job_catches_up_an_overdue_pass(stores):
    from plugboard import maintenance, retention

    _last_pass_at(stores, datetime.now(timezone.utc) - timedelta(days=2))
    job = maintenance.retention_job()
    assert job.catches_up() is True
    assert retention.pass_overdue() is True


# --- BUG-76: no compaction of the stores the requests write ------------------


def test_the_stores_written_by_requests_are_never_compacted(stores, monkeypatch):
    # VACUUM locks the store while it runs: a billing write (usage) or a chat
    # session write waited up to its 5 s busy timeout on the event loop, and
    # failed after it. Their free pages are reused by later writes instead.
    from plugboard import retention, storage

    seed_one_old_and_one_recent_row_everywhere()
    compacted = []
    real_compact = storage.compact_sqlite

    def spy(path, *args, **kwargs):
        compacted.append(path)
        return real_compact(path, *args, **kwargs)

    monkeypatch.setattr(storage, "compact_sqlite", spy)
    report = retention.run_retention(NOW)

    assert stores["usage"] not in compacted
    assert stores["sessions"] not in compacted
    assert {stores["audit"], stores["watchdog"], stores["monitoring"]} <= set(compacted)
    assert report["stores"]["usage"]["deleted"] >= 1
    assert report["stores"]["usage"]["compacted"] is False
