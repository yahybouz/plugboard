"""
Audit trail — logs every admin action to SQLite.

Database: data/audit.db
Table: audit_log
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from plugboard.storage import data_dir

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
    ip          TEXT NOT NULL DEFAULT ''
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);",
]


def _get_conn() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE)
    for idx in _CREATE_INDEXES:
        conn.execute(idx)
    conn.commit()
    return conn


def log_action(
    action: str,
    actor: str = "admin",
    target_type: str = "",
    target_id: str = "",
    details: str = "",
    ip: str = "",
) -> Dict[str, Any]:
    """Record an admin action. Returns the audit entry."""
    entry = {
        "id": secrets.token_hex(8),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "action": action,
        "actor": actor,
        "target_type": target_type,
        "target_id": target_id,
        "details": details,
        "ip": ip,
    }
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO audit_log (id, ts, action, actor, target_type, target_id, details, ip)
               VALUES (:id, :ts, :action, :actor, :target_type, :target_id, :details, :ip)""",
            entry,
        )
        conn.commit()
    finally:
        conn.close()
    return entry


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
