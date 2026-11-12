"""Purge and compaction helpers for the SQLite stores."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time

import pytest

from plugboard.storage import compact_sqlite, private_sqlite_file, purge_sqlite_rows

OLD_ROWS = 200_000


def _make_db(path, rows):
    private_sqlite_file(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, ts TEXT, body TEXT)")
        conn.executemany("INSERT INTO t (ts, body) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()
    return path


def _count(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def test_purge_deletes_matching_rows_and_returns_count(tmp_path):
    db = _make_db(
        tmp_path / "store.db",
        [("2026-01-01", "a"), ("2026-06-01", "b"), ("2026-11-01", "c")],
    )

    assert purge_sqlite_rows(db, "t", "ts < ?", ("2026-06-01",)) == 1
    assert _count(db) == 2


def test_purge_missing_file_returns_zero_and_creates_nothing(tmp_path):
    db = tmp_path / "absent" / "store.db"

    assert purge_sqlite_rows(db, "t", "ts < ?", ("2026-06-01",)) == 0
    assert not db.exists()


def test_purge_missing_table_returns_zero(tmp_path):
    db = _make_db(tmp_path / "store.db", [("2026-01-01", "a")])

    assert purge_sqlite_rows(db, "other", "ts < ?", ("2026-06-01",)) == 0
    assert _count(db) == 1


def test_purge_raises_when_the_database_is_locked(tmp_path):
    db = _make_db(tmp_path / "store.db", [("2026-01-01", "a")])
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError):
            purge_sqlite_rows(db, "t", "ts < ?", ("2026-06-01",), busy_timeout_ms=50)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _count(db) == 1


def test_compact_skips_a_file_with_little_free_space(tmp_path):
    db = _make_db(tmp_path / "store.db", [("2026-01-01", "x" * 100)] * 2000)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM t WHERE id = 1")
        conn.commit()
    finally:
        conn.close()

    assert compact_sqlite(db) is False


def test_compact_shrinks_a_mostly_free_file_and_keeps_its_mode(tmp_path):
    db = _make_db(tmp_path / "store.db", [("2026-01-01", "x" * 500)] * 5000)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM t")
        conn.commit()
    finally:
        conn.close()
    size_before = db.stat().st_size

    assert compact_sqlite(db) is True
    assert db.stat().st_size < size_before
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600


def test_compact_missing_file_returns_false(tmp_path):
    db = tmp_path / "absent.db"

    assert compact_sqlite(db) is False
    assert not db.exists()


def test_purge_deletes_in_batches(tmp_path, monkeypatch):
    from plugboard import storage

    db = _make_db(
        tmp_path / "store.db",
        [("2026-01-01", "old")] * 1000 + [("2026-11-01", "new")] * 10,
    )
    batches = []
    real_batch = storage._delete_batch

    def counted(*args, **kwargs):
        batches.append(real_batch(*args, **kwargs))
        return batches[-1]

    monkeypatch.setattr(storage, "_delete_batch", counted)

    deleted = purge_sqlite_rows(
        db, "t", "ts < ?", ("2026-06-01",), batch_size=100, pause_s=0
    )

    assert deleted == 1000
    assert batches == [100] * 10 + [0]
    assert _count(db) == 10


def test_a_concurrent_writer_is_never_refused_during_a_purge(tmp_path):
    # A writer set like the gateway's own (busy timeout 1 s here, 5 s there)
    # inserts in a loop while a large purge runs: short batches leave it
    # room between two of them, so none of its inserts is refused, and many
    # land while the purge is still going (a single-transaction purge would
    # hold them all back until its end). The pause is shorter than the
    # default to keep the test quick; the default leaves writers more room.
    db = private_sqlite_file(tmp_path / "store.db")
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, ts TEXT, body TEXT)")
        conn.executemany(
            "INSERT INTO t (ts, body) VALUES (?, ?)",
            (("2026-01-01", "x" * 100) for _ in range(OLD_ROWS)),
        )
        conn.commit()
    finally:
        conn.close()

    started = threading.Event()
    purge_over = threading.Event()
    written: list = []
    refused: list = []

    def writer():
        writer_conn = sqlite3.connect(db, timeout=1.0)
        try:
            while not purge_over.is_set():
                try:
                    writer_conn.execute(
                        "INSERT INTO t (ts, body) VALUES (?, ?)", ("2026-11-01", "new")
                    )
                    writer_conn.commit()
                    written.append(time.monotonic())
                except sqlite3.Error as exc:
                    refused.append(exc)
                    writer_conn.rollback()
                started.set()
                time.sleep(0.005)
        finally:
            writer_conn.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert started.wait(5)
        began = time.monotonic()
        deleted = purge_sqlite_rows(
            db, "t", "ts < ?", ("2026-06-01",), batch_size=2000, pause_s=0.01
        )
        ended = time.monotonic()
    finally:
        purge_over.set()
        thread.join(10)

    assert refused == []
    assert deleted == OLD_ROWS
    assert sum(began <= when <= ended for when in written) >= 10, (
        "the writer did not run alongside the purge"
    )
    conn = sqlite3.connect(db)
    try:
        rows = dict(conn.execute("SELECT body, COUNT(*) FROM t GROUP BY body"))
    finally:
        conn.close()
    assert rows == {"new": len(written)}
