"""BUG-122: threads that open a new audit database at once lose no entry.

Several threads ran ``PRAGMA journal_mode=WAL`` on a brand-new database at
the same time; SQLite answered "database is locked" to the losers (the busy
handler is not called on that lock upgrade) and their audit entry was lost.
That made ``test_netguard.py::test_report_refusal_once_per_window`` flaky.
"""

from __future__ import annotations

import threading

from plugboard import audit as audit_mod

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
