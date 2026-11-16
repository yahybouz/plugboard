"""BUG-122: threads that open a new audit database at once lose no entry.

Several threads ran ``PRAGMA journal_mode=WAL`` on a brand-new database at
the same time; SQLite answered "database is locked" to the losers (the busy
handler is not called on that lock upgrade) and their audit entry was lost.
That made ``test_netguard.py::test_report_refusal_once_per_window`` flaky.

BUG-131: an entry written while the audit database is deleted and recreated
is never lost: it went into the new empty file ("no such table"), or into the
deleted file, or failed with "disk I/O error" when the files were removed
under its connection. A real error is still raised at once. Every thread is
joined with a timeout: no test can hang.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from plugboard import audit as audit_mod
from plugboard import storage

THREADS = 4
ROUNDS = 60  # one round lost an entry ~14 % of the time before the fix


def _write_at_once(n: int) -> list[str]:
    errors: list[str] = []

    def write(i: int) -> None:
        try:
            audit_mod.log_action("test.concurrent", target_id=str(i))
        except Exception as exc:  # noqa: BLE001 - the test records any failure
            errors.append(repr(exc))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_threads_on_a_new_audit_database_lose_no_entry(monkeypatch, tmp_path):
    failures = []
    for rnd in range(ROUNDS):
        monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / f"r{rnd}" / "audit.db")
        errors = _write_at_once(THREADS)
        count = audit_mod.count_audit_log(action="test.concurrent")
        if errors or count != THREADS:
            failures.append((rnd, count, errors[:1]))
    assert failures == []


def test_a_deleted_audit_database_is_set_up_again(monkeypatch, tmp_path):
    path = tmp_path / "audit.db"
    monkeypatch.setattr(audit_mod, "_DB_PATH", path)
    audit_mod.log_action("test.first")
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"audit.db{suffix}").unlink(missing_ok=True)
    audit_mod.log_action("test.second")
    assert [e["action"] for e in audit_mod.get_audit_log()] == ["test.second"]


def _delete(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


class _Conn:
    """A connection whose INSERT and commit can be hooked."""

    def __init__(self, conn, before_insert=None, after_commit=None):
        self._conn = conn
        self._before_insert = before_insert
        self._after_commit = after_commit
        self._inserted = False

    def execute(self, sql, *args):
        if sql.lstrip().upper().startswith("INSERT"):
            self._inserted = True
            if self._before_insert:
                self._before_insert()
        return self._conn.execute(sql, *args)

    def commit(self):
        self._conn.commit()
        if self._inserted and self._after_commit:
            self._after_commit()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _hook_connections(monkeypatch, **hooks):
    real = audit_mod.open_sqlite_store

    def hooked(*args, **kwargs):
        return _Conn(real(*args, **kwargs), **hooks)

    monkeypatch.setattr(audit_mod, "open_sqlite_store", hooked)


def test_an_entry_written_while_the_database_is_recreated_is_kept(
    monkeypatch, tmp_path
):
    """Deleted between the "set up?" check and the open: the writer opens a
    new, empty file."""
    path = tmp_path / "audit.db"
    monkeypatch.setattr(audit_mod, "_DB_PATH", path)
    audit_mod.log_action("test.before")
    real = storage.private_sqlite_file
    pending = [True]

    def deleted_meanwhile(target):
        if pending and Path(target) == path:
            pending.clear()
            _delete(path)
        return real(target)

    monkeypatch.setattr(storage, "private_sqlite_file", deleted_meanwhile)
    audit_mod.log_action("test.during")
    assert [e["action"] for e in audit_mod.get_audit_log()] == ["test.during"]


@pytest.mark.parametrize("moment", ["before_insert", "after_commit"])
def test_an_entry_that_went_into_the_deleted_file_is_written_again(
    monkeypatch, tmp_path, moment
):
    """Deleted while the writer holds the old file open: the entry went into
    the deleted file, with no error. It must reach the new database."""
    path = tmp_path / "audit.db"
    monkeypatch.setattr(audit_mod, "_DB_PATH", path)
    audit_mod.log_action("test.before")
    pending = [True]

    def delete_once():
        if pending:
            pending.clear()
            _delete(path)

    _hook_connections(monkeypatch, **{moment: delete_once})
    audit_mod.log_action("test.during")
    monkeypatch.undo()  # read the database as it is, without the hooks
    monkeypatch.setattr(audit_mod, "_DB_PATH", path)
    assert [e["action"] for e in audit_mod.get_audit_log()] == ["test.during"]


def test_a_real_error_is_raised_at_once(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")
    attempts = []

    def full():
        attempts.append(1)
        raise sqlite3.OperationalError("database or disk is full")

    _hook_connections(monkeypatch, before_insert=full)
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        audit_mod.log_action("test.full")
    assert len(attempts) == 1


def test_a_store_that_keeps_disappearing_gives_up_in_time(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(audit_mod, "_WRITE_DEADLINE_S", 0.3, raising=False)
    attempts = []

    def gone():
        attempts.append(1)
        raise sqlite3.OperationalError("no such table: audit_log")

    _hook_connections(monkeypatch, before_insert=gone)
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        audit_mod.log_action("test.gone")
    elapsed = time.monotonic() - started
    assert len(attempts) > 1  # written again while the deadline lasts
    assert 0.3 <= elapsed < 5.0


# Ten deletions 20 ms apart, while four threads write without a pause.
DELETIONS = 10
DELETION_GAP_S = 0.02
JOIN_S = 60.0


def test_threads_writing_while_the_database_is_deleted_lose_nothing(
    monkeypatch, tmp_path
):
    path = tmp_path / "audit.db"
    monkeypatch.setattr(audit_mod, "_DB_PATH", path)
    audit_mod.log_action("test.seed")
    stop = threading.Event()
    errors: list[str] = []

    def writer(n: int) -> None:
        i = 0
        while not stop.is_set() and len(errors) < 5:
            try:
                audit_mod.log_action("test.race", target_id=f"{n}-{i}")
            except Exception as exc:  # noqa: BLE001 - the test records any failure
                errors.append(repr(exc))
            i += 1
        try:  # once the deletions are over, every entry must be kept
            audit_mod.log_action("test.after", target_id=str(n))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=writer, args=(n,), daemon=True) for n in range(THREADS)
    ]
    for t in threads:
        t.start()
    for _ in range(DELETIONS):
        time.sleep(DELETION_GAP_S)
        _delete(path)
    stop.set()
    for t in threads:
        t.join(timeout=JOIN_S)
    assert not any(t.is_alive() for t in threads), "a writer did not stop"
    assert errors == []
    assert audit_mod.count_audit_log(action="test.after") == THREADS
