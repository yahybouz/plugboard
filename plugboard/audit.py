"""
Audit trail — logs every admin action to SQLite.

Database: data/audit.db
Table: audit_log

``actor`` says which channel acted (``admin``, ``mcp``, ``system``).
``principal`` names the admin credential behind an entry (PB-AUTH-01):
``admin-key`` for ``ADMIN_KEY``, ``person:<id>`` for a personal key, with
the person's name in ``principal_name``. An empty ``principal`` means no
admin credential was known: an entry written outside an admin request, or
one written before these columns existed (an older database gains them,
empty, when it is opened).

The principal travels with the request: ``begin_request`` puts an empty
holder in a context variable, the authentication fills it
(``set_principal``, also from a worker thread, which shares the holder),
and ``log_action`` reads it. Each HTTP request opens its own holder (the
request middleware of ``plugboard.app``), so two requests never share one.

A background task started while serving an admin request copies that
context, holder included. Every audit entry such a task writes today is a
``system`` one (retention, quota limit, egress refusal, unreadable key), and
a ``system`` entry never takes the request's principal: it names none
unless one is passed explicitly.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
import time
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from plugboard.storage import data_dir, forget_sqlite_store, open_sqlite_store

_DATA_DIR = data_dir()
_DB_PATH = _DATA_DIR / "audit.db"

log = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'admin',
    target_type TEXT NOT NULL DEFAULT '',
    target_id   TEXT NOT NULL DEFAULT '',
    details     TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    principal       TEXT NOT NULL DEFAULT '',
    principal_name  TEXT NOT NULL DEFAULT ''
);
"""

# Columns added after the first release: an older database gains them on open.
_ADDED_COLUMNS = (
    ("principal", "TEXT NOT NULL DEFAULT ''"),
    ("principal_name", "TEXT NOT NULL DEFAULT ''"),
)

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);",
]


def _get_conn() -> sqlite3.Connection:
    # Set-up (WAL mode, table, indexes) runs once per database per process,
    # under a lock (BUG-122, BUG-128): threads that switched a new database to
    # WAL at once got "database is locked" and their audit entry was lost.
    return open_sqlite_store(_DB_PATH, _set_up)


def _set_up(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_TABLE)
    present = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)")}
    for name, decl in _ADDED_COLUMNS:
        if name in present:
            continue
        try:
            conn.execute(f"ALTER TABLE audit_log ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError as exc:
            # Another process migrated the same database meanwhile.
            if "duplicate column" not in str(exc).lower():
                raise
    for idx in _CREATE_INDEXES:
        conn.execute(idx)


# The admin principal of the request being served: a mutable holder, so that
# a worker thread (which runs in a copy of the context) fills the same one.
_principal_var: ContextVar[Optional[Dict[str, str]]] = ContextVar(
    "audit_principal", default=None
)


def begin_request() -> Token:
    """Open an empty principal holder for the request being served."""
    return _principal_var.set({})


def end_request(token: Token) -> None:
    """Forget the request's principal holder."""
    _principal_var.reset(token)


def set_principal(principal: str, name: str = "") -> None:
    """Name the admin credential of the current request; outside one, do nothing."""
    holder = _principal_var.get()
    if holder is None:
        return
    holder["principal"] = principal
    holder["name"] = name


def current_principal() -> Tuple[str, str]:
    """``(principal, name)`` of the current request, ``("", "")`` if none."""
    holder = _principal_var.get()
    if not holder:
        return ("", "")
    return (holder.get("principal", ""), holder.get("name", ""))


def log_action(
    action: str,
    actor: str = "admin",
    target_type: str = "",
    target_id: str = "",
    details: str = "",
    ip: str = "",
    principal: Optional[str] = None,
    principal_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Record an admin action. Returns the audit entry.

    ``principal`` defaults to the current request's (``current_principal``),
    except for a ``system`` entry, which defaults to none. Given without
    ``principal_name``, the name is empty; without a principal, the name is
    always empty.
    """
    if principal is None:
        if actor == "system":
            principal, current_name = "", ""
        else:
            principal, current_name = current_principal()
        if principal_name is None:
            principal_name = current_name
    if not principal or principal_name is None:
        principal_name = ""
    entry = {
        "id": secrets.token_hex(8),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "action": action,
        "actor": actor,
        "target_type": target_type,
        "target_id": target_id,
        "details": details,
        "ip": ip,
        "principal": principal,
        "principal_name": principal_name,
    }
    with _write_lock:
        deadline = time.monotonic() + _WRITE_DEADLINE_S
        while True:
            lost = _write_once(entry)
            if lost is None:
                return entry
            if time.monotonic() >= deadline:
                raise lost
            forget_sqlite_store(_DB_PATH)  # set the new database up again
            time.sleep(_RETRY_PAUSE_S)


# An entry written while the database is deleted and recreated is never lost
# (BUG-131). Under one lock, a write checks the schema on its own connection
# (and recreates it), writes, then reads the entry back from the file now at
# the path: an entry that went into the deleted file, or met the new empty
# file ("no such table"), or lost its files under the connection ("disk I/O
# error"), is written again until the deadline. Any other error is raised at
# once.
_WRITE_DEADLINE_S = 2.0
_RETRY_PAUSE_S = 0.01
_write_lock = threading.Lock()

_INSERT = """INSERT OR IGNORE INTO audit_log
    (id, ts, action, actor, target_type, target_id, details, ip,
     principal, principal_name)
    VALUES (:id, :ts, :action, :actor, :target_type, :target_id, :details, :ip,
            :principal, :principal_name)"""
_FILE_GONE_ERRORS = ("disk i/o error", "readonly database")


def _file_id(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def _store_lost(exc: sqlite3.OperationalError, before: Optional[Tuple]) -> bool:
    """The error comes from a database deleted meanwhile, not a real fault."""
    message = str(exc).lower()
    if "no such table" in message:
        return True  # the schema is created under the lock: only a deletion
    if not any(marker in message for marker in _FILE_GONE_ERRORS):
        return False
    return before is None or _file_id(_DB_PATH) != before


def _write_once(entry: Dict[str, Any]) -> Optional[sqlite3.OperationalError]:
    """Write ``entry``; return the error if the database was lost meanwhile."""
    before = _file_id(_DB_PATH)
    try:
        conn = _get_conn()
        try:
            found = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_log'"
            ).fetchone()
            if found is None:
                _set_up(conn)
            conn.execute(_INSERT, entry)
            conn.commit()
        finally:
            conn.close()
        conn = _get_conn()  # the file now at the path holds the entry?
        try:
            kept = conn.execute(
                "SELECT 1 FROM audit_log WHERE id = ?", (entry["id"],)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if _store_lost(exc, before):
            return exc
        raise
    if kept is None:
        return sqlite3.OperationalError(
            "the audit database was deleted while the entry was written"
        )
    return None


def get_audit_log(
    limit: int = 100,
    offset: int = 0,
    action: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Retrieve audit log entries, newest first."""
    conn = _get_conn()
    try:
        if action:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE action = ? ORDER BY ts DESC LIMIT ? OFFSET ?",
                (action, limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_audit_log(action: Optional[str] = None) -> int:
    """Count total audit entries."""
    conn = _get_conn()
    try:
        if action:
            return conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = ?", (action,)
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    finally:
        conn.close()
