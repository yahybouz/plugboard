"""Purge and compaction helpers for the SQLite stores."""

from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from plugboard.storage import compact_sqlite, private_sqlite_file, purge_sqlite_rows


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
