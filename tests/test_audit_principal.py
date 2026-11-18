"""Audit entries name the admin credential behind them (PB-AUTH-01)."""

import asyncio
import sqlite3

import pytest

from plugboard import audit

OLD_TABLE = """CREATE TABLE audit_log (id TEXT PRIMARY KEY, ts TEXT NOT NULL,
    action TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'admin',
    target_type TEXT NOT NULL DEFAULT '', target_id TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '')"""


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_DB_PATH", tmp_path / "audit.db")


def test_an_entry_outside_a_request_has_an_empty_principal(audit_db):
    entry = audit.log_action("x.test")
    assert (entry["principal"], entry["principal_name"]) == ("", "")
    assert audit.get_audit_log()[0]["principal"] == ""


def test_the_request_principal_is_recorded_then_forgotten(audit_db):
    token = audit.begin_request()
    try:
        audit.set_principal("person:ap_0123456789ab", "person-one")
        audit.log_action("x.test")
    finally:
        audit.end_request(token)
    row = audit.get_audit_log(action="x.test")[0]
    assert (row["principal"], row["principal_name"]) == (
        "person:ap_0123456789ab",
        "person-one",
    )
    audit.log_action("y.test")
    assert audit.get_audit_log(action="y.test")[0]["principal"] == ""


def test_an_explicit_principal_wins(audit_db):
    token = audit.begin_request()
    try:
        audit.set_principal("person:ap_0123456789ab", "person-one")
        entry = audit.log_action("x.test", principal="admin-key")
    finally:
        audit.end_request(token)
    assert (entry["principal"], entry["principal_name"]) == ("admin-key", "")


def test_set_principal_outside_a_request_does_nothing(audit_db):
    audit.set_principal("admin-key")
    assert audit.current_principal() == ("", "")


async def test_a_worker_thread_fills_the_request_principal(audit_db):
    token = audit.begin_request()
    try:
        await asyncio.to_thread(audit.set_principal, "admin-key", "")
        audit.log_action("z.test")
    finally:
        audit.end_request(token)
    assert audit.get_audit_log(action="z.test")[0]["principal"] == "admin-key"


def test_an_old_database_gains_the_columns(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(OLD_TABLE)
        conn.execute(
            "INSERT INTO audit_log (id, ts, action) VALUES ('old1', '2026-01-01T00:00:00.000Z', 'key.create')"
        )
    conn.close()
    monkeypatch.setattr(audit, "_DB_PATH", db)
    audit.log_action("new.test", principal="admin-key")
    rows = {r["action"]: r for r in audit.get_audit_log()}
    assert (rows["key.create"]["principal"], rows["key.create"]["principal_name"]) == (
        "",
        "",
    )
    assert rows["new.test"]["principal"] == "admin-key"
