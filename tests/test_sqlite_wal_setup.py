"""BUG-128: every SQLite store is set up once, under a lock, before it is written.

BUG-122 (audit.py, 28b3b8f): threads that ran ``PRAGMA journal_mode=WAL`` on
a brand-new database at once got "database is locked" (SQLite does not call
the busy handler on that lock upgrade) and lost their write. The same set-up
ran unlocked, on every connection, in semantic_cache, mcp_server,
session_store, suivi, monitoring and provider_watchdog. A store deleted while
the gateway runs must also be set up again on the next write, not answer
"no such table" until a restart.

The threads are bounded in time: a thread still running after ``JOIN_S``
fails the test instead of hanging it.
"""

from __future__ import annotations

import importlib
import sqlite3
import threading
from pathlib import Path

import pytest

THREADS = 4
ROUNDS = 40
JOIN_S = 30.0

# module, attribute holding the database path (None: under PLUGBOARD_DATA),
# file name, opener, write statement (one ``?`` for a unique id), table.
STORES = {
    "semantic_cache": (
        "plugboard.semantic_cache",
        "_DB_PATH",
        "semantic_cache.db",
        "_get_conn",
        "INSERT INTO semantic_cache (cache_key, model, signature, band_keys,"
        " prompt_hash, response, created_at, ttl)"
        " VALUES (?, 'm', x'00', '', '', '', 0, 1)",
        "semantic_cache",
    ),
    "mcp_server": (
        "plugboard.mcp_server",
        "_TRACKING_DB",
        "client_updates.db",
        "_tracking_conn",
        "INSERT INTO applied_updates (client_id, prompt_id, applied_at)"
        " VALUES ('c', ?, 't')",
        "applied_updates",
    ),
    "session_store": (
        "plugboard.session_store",
        "_DB_PATH",
        "sessions.db",
        "_get_conn",
        "INSERT INTO chat_sessions VALUES ('c', ?, '[]', 0, 0)",
        "chat_sessions",
    ),
    "suivi": (
        "plugboard.suivi",
        None,
        "suivi.db",
        "_connect",
        "INSERT INTO tickets (titre, statut, priorite, type, source, auteur,"
        " cree_le, maj_le) VALUES (?, 'a_trier', 'P2', 'bug', 'manuel', 't',"
        " 't', 't')",
        "tickets",
    ),
    "monitoring": (
        "plugboard.monitoring",
        "_DB_PATH",
        "monitoring.db",
        "_get_conn",
        "INSERT INTO monitoring_snapshots (id, ts, provider_id) VALUES (?, 't', 'p')",
        "monitoring_snapshots",
    ),
    "provider_watchdog": (
        "plugboard.provider_watchdog",
        "_DB_PATH",
        "watchdog.db",
        "_get_conn",
        "INSERT INTO watchdog_events (id, ts, provider_id, event_type)"
        " VALUES (?, 't', 'p', 'e')",
        "watchdog_events",
    ),
    "audit": (
        "plugboard.audit",
        "_DB_PATH",
        "audit.db",
        "_get_conn",
        "INSERT INTO audit_log (id, ts, action) VALUES (?, 't', 'a')",
        "audit_log",
    ),
}


def _point_at(monkeypatch, store: str, directory: Path) -> Path:
    module_name, attr, filename, *_ = STORES[store]
    path = directory / filename
    if attr is None:
        monkeypatch.setenv("PLUGBOARD_DATA", str(directory))
    else:
        monkeypatch.setattr(importlib.import_module(module_name), attr, path)
    return path


def _write(store: str, uid: str) -> None:
    module_name, _attr, _file, opener, statement, _table = STORES[store]
    conn = getattr(importlib.import_module(module_name), opener)()
    try:
        conn.execute(statement, (uid,))
        conn.commit()
    finally:
        conn.close()


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # nosec B608
    finally:
        conn.close()


def _write_at_once(store: str, rnd: int) -> list:
    errors: list = []
    barrier = threading.Barrier(THREADS)

    def write(i: int) -> None:
        try:
            barrier.wait(timeout=JOIN_S)
            _write(store, f"{rnd}-{i}")
        except Exception as exc:  # noqa: BLE001 - the test records any failure
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=write, args=(i,), daemon=True) for i in range(THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=JOIN_S)
    if any(t.is_alive() for t in threads):
        errors.append("a writer was still running after %.0f s" % JOIN_S)
    return errors


@pytest.mark.parametrize("store", sorted(STORES))
def test_threads_on_a_new_database_lose_no_write(monkeypatch, tmp_path, store):
    table = STORES[store][5]
    failures = []
    for rnd in range(ROUNDS):
        path = _point_at(monkeypatch, store, tmp_path / f"r{rnd}")
        errors = _write_at_once(store, rnd)
        count = _count(path, table) if path.exists() else 0
        if errors or count != THREADS:
            failures.append((rnd, count, errors[:1]))
    assert failures == []


@pytest.mark.parametrize("store", sorted(STORES))
def test_a_deleted_database_is_set_up_again(monkeypatch, tmp_path, store):
    table = STORES[store][5]
    path = _point_at(monkeypatch, store, tmp_path)
    _write(store, "first")
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    _write(store, "second")
    assert _count(path, table) == 1
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
