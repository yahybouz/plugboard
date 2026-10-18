"""
Usage log — SQLite-backed append-only storage.

Database: data/usage.db
Table: usage_entries

Migrated from per-month JSON files (usage_YYYY_MM.json) to a single SQLite
database for scalability (>10k req/day without I/O degradation).

The public API is unchanged — all existing callers continue to work.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List

from plugboard import billing as _billing
from plugboard.storage import data_dir

_DATA_DIR = data_dir()
_DB_PATH = _DATA_DIR / "usage.db"

log = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS usage_entries (
    id              TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    month           TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    key_id          TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL,
    provider_id     TEXT NOT NULL,
    prompt_tokens   INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    total_tokens    INTEGER NOT NULL,
    pricing_month   TEXT NOT NULL,
    cost_usd        REAL NOT NULL,
    price_key       TEXT NOT NULL DEFAULT '',
    price_source    TEXT NOT NULL DEFAULT ''
);
"""

# Columns added after the first release; ALTERed into older databases.
_ADDED_COLUMNS = {
    "price_key": "TEXT NOT NULL DEFAULT ''",
    "price_source": "TEXT NOT NULL DEFAULT ''",
}

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_usage_month ON usage_entries(month);",
    "CREATE INDEX IF NOT EXISTS idx_usage_client ON usage_entries(month, client_id);",
    "CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_entries(month, key_id);",
]


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection with WAL mode."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _DB_PATH.exists():
        _schema_ready.clear()  # deleted or not created yet: set the schema up again
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    if _schema_ready.get("path") != str(_DB_PATH):
        conn.execute(_CREATE_TABLE)
        existing = {row[1] for row in conn.execute("PRAGMA table_info(usage_entries)")}
        for column, decl in _ADDED_COLUMNS.items():
            if column not in existing:
                try:
                    conn.execute(
                        f"ALTER TABLE usage_entries ADD COLUMN {column} {decl}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise  # another process added it first: fine
        for idx in _CREATE_INDEXES:
            conn.execute(idx)
        conn.commit()
        _schema_ready["path"] = str(_DB_PATH)
    return conn


# Schema creation and column migration run once per database per process.
_schema_ready: Dict[str, str] = {}


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def append_usage(
    client_id: str,
    model: str,
    provider_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    pricing_month: str,
    cost_usd: float,
    key_id: str = "",
    price_key: str = "",
    price_source: str = "",
) -> Dict[str, Any]:
    """Append a usage entry. Returns the written entry.

    ``provider_id`` is the provider that served the call; ``price_key`` and
    ``price_source`` say which grid entry priced it.
    """
    month = _billing.current_pricing_month()
    entry: Dict[str, Any] = {
        "id": secrets.token_hex(8),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "month": month,
        "client_id": client_id,
        "key_id": key_id,
        "model": model,
        "provider_id": provider_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "pricing_month": pricing_month,
        "cost_usd": cost_usd,
        "price_key": price_key,
        "price_source": price_source,
    }
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO usage_entries
               (id, ts, month, client_id, key_id, model, provider_id,
                prompt_tokens, completion_tokens, total_tokens, pricing_month, cost_usd,
                price_key, price_source)
               VALUES (:id, :ts, :month, :client_id, :key_id, :model, :provider_id,
                       :prompt_tokens, :completion_tokens, :total_tokens, :pricing_month, :cost_usd,
                       :price_key, :price_source)""",
            entry,
        )
        conn.commit()
    finally:
        conn.close()
    return entry


def get_usage(month: str) -> List[Dict[str, Any]]:
    """Return all entries for a given month."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM usage_entries WHERE month = ? ORDER BY ts", (month,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_usage_paginated(month: str, page: int = 1, limit: int = 100) -> tuple:
    """Return (entries_page, total_count)."""
    conn = _get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM usage_entries WHERE month = ?", (month,)
        ).fetchone()[0]
        offset = (page - 1) * limit
        rows = conn.execute(
            "SELECT * FROM usage_entries WHERE month = ? ORDER BY ts LIMIT ? OFFSET ?",
            (month, limit, offset),
        ).fetchall()
        return [_row_to_dict(r) for r in rows], total
    finally:
        conn.close()


def get_usage_by_client(month: str, client_id: str) -> List[Dict[str, Any]]:
    """Filter entries by client_id for a given month."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM usage_entries WHERE month = ? AND client_id = ? ORDER BY ts",
            (month, client_id),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_usage_by_key(month: str, key_id: str) -> List[Dict[str, Any]]:
    """Filter entries by key_id for a given month."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM usage_entries WHERE month = ? AND key_id = ? ORDER BY ts",
            (month, key_id),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_total_cost_by_client(month: str, client_id: str) -> float:
    """Return total monthly spend for a given client_id."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd
            FROM usage_entries
            WHERE month = ? AND client_id = ?
            """,
            (month, client_id),
        ).fetchone()
        return float(row["total_cost_usd"] or 0.0)
    finally:
        conn.close()


def get_total_cost_by_key(month: str, key_id: str) -> float:
    """Return total monthly spend for a given key_id."""
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd
            FROM usage_entries
            WHERE month = ? AND key_id = ?
            """,
            (month, key_id),
        ).fetchone()
        return float(row["total_cost_usd"] or 0.0)
    finally:
        conn.close()


def _summarize_scope(
    month: str, client_id: str = "", key_id: str = ""
) -> Dict[str, Any]:
    """Aggregate usage summary in SQL for a month, optionally filtered by client/key."""
    if client_id and key_id:
        params = (month, client_id, key_id)
        totals_sql = """
            SELECT
                COUNT(*) AS total_entries,
                COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd
            FROM usage_entries
            WHERE month = ? AND client_id = ? AND key_id = ?
        """
        by_model_sql = """
            SELECT
                model,
                COUNT(*) AS entries,
                COALESCE(SUM(total_tokens), 0) AS tokens,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd
            FROM usage_entries
            WHERE month = ? AND client_id = ? AND key_id = ?
            GROUP BY model
            ORDER BY model
        """
    elif client_id:
        params = (month, client_id)
        totals_sql = """
            SELECT
                COUNT(*) AS total_entries,
                COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd
            FROM usage_entries
            WHERE month = ? AND client_id = ?
        """
        by_model_sql = """
            SELECT
                model,
                COUNT(*) AS entries,
                COALESCE(SUM(total_tokens), 0) AS tokens,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd
            FROM usage_entries
            WHERE month = ? AND client_id = ?
            GROUP BY model
            ORDER BY model
        """
    elif key_id:
        params = (month, key_id)
        totals_sql = """
            SELECT
                COUNT(*) AS total_entries,
                COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd
            FROM usage_entries
            WHERE month = ? AND key_id = ?
        """
        by_model_sql = """
            SELECT
                model,
                COUNT(*) AS entries,
                COALESCE(SUM(total_tokens), 0) AS tokens,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd
            FROM usage_entries
            WHERE month = ? AND key_id = ?
            GROUP BY model
            ORDER BY model
        """
    else:
        params = (month,)
        totals_sql = """
            SELECT
                COUNT(*) AS total_entries,
                COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd
            FROM usage_entries
            WHERE month = ?
        """
        by_model_sql = """
            SELECT
                model,
                COUNT(*) AS entries,
                COALESCE(SUM(total_tokens), 0) AS tokens,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd
            FROM usage_entries
            WHERE month = ?
            GROUP BY model
            ORDER BY model
        """

    conn = _get_conn()
    try:
        totals = conn.execute(totals_sql, params).fetchone()
        by_model_rows = conn.execute(by_model_sql, params).fetchall()
    finally:
        conn.close()

    summary: Dict[str, Any] = {
        "total_entries": int(totals["total_entries"] or 0),
        "total_prompt_tokens": int(totals["total_prompt_tokens"] or 0),
        "total_completion_tokens": int(totals["total_completion_tokens"] or 0),
        "total_tokens": int(totals["total_tokens"] or 0),
        "total_cost_usd": round(float(totals["total_cost_usd"] or 0.0), 8),
        "by_model": {},
    }
    for row in by_model_rows:
        summary["by_model"][row["model"]] = {
            "entries": int(row["entries"] or 0),
            "tokens": int(row["tokens"] or 0),
            "cost_usd": round(float(row["cost_usd"] or 0.0), 8),
        }
    return summary


def summarize_usage_month(month: str) -> Dict[str, Any]:
    """Aggregate usage summary for a full month using SQL."""
    return _summarize_scope(month)


def summarize_usage_by_client(month: str, client_id: str) -> Dict[str, Any]:
    """Aggregate usage summary for a client in a month using SQL."""
    return _summarize_scope(month, client_id=client_id)


def summarize_usage_by_key(month: str, key_id: str) -> Dict[str, Any]:
    """Aggregate usage summary for a key in a month using SQL."""
    return _summarize_scope(month, key_id=key_id)


def delete_usage(month: str) -> bool:
    """Delete all entries for a month. Returns False if none found."""
    conn = _get_conn()
    try:
        cursor = conn.execute("DELETE FROM usage_entries WHERE month = ?", (month,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def purge_old_usage(keep_months: int = 6) -> List[str]:
    """Delete entries older than `keep_months`. Returns list of deleted months."""
    now = datetime.now(timezone.utc)
    conn = _get_conn()
    try:
        months = conn.execute(
            "SELECT DISTINCT month FROM usage_entries ORDER BY month"
        ).fetchall()
        deleted = []
        for row in months:
            m = row[0]
            parts = m.split("_")
            if len(parts) != 2:
                continue
            year, month_num = int(parts[0]), int(parts[1])
            age = (now.year - year) * 12 + (now.month - month_num)
            if age > keep_months:
                conn.execute("DELETE FROM usage_entries WHERE month = ?", (m,))
                deleted.append(m)
                log.info("Purged old usage: %s", m)
        if deleted:
            conn.commit()
        return deleted
    finally:
        conn.close()


def summarize_usage(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate entries into totals and per-model breakdown."""
    summary: Dict[str, Any] = {
        "total_entries": len(entries),
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "by_model": {},
    }
    for e in entries:
        summary["total_prompt_tokens"] += e.get("prompt_tokens", 0)
        summary["total_completion_tokens"] += e.get("completion_tokens", 0)
        summary["total_tokens"] += e.get("total_tokens", 0)
        summary["total_cost_usd"] += e.get("cost_usd", 0.0)
        m = e.get("model", "unknown")
        bm = summary["by_model"].setdefault(
            m, {"entries": 0, "tokens": 0, "cost_usd": 0.0}
        )
        bm["entries"] += 1
        bm["tokens"] += e.get("total_tokens", 0)
        bm["cost_usd"] += e.get("cost_usd", 0.0)
    summary["total_cost_usd"] = round(summary["total_cost_usd"], 8)
    for bm in summary["by_model"].values():
        bm["cost_usd"] = round(bm["cost_usd"], 8)
    return summary


def migrate_json_to_sqlite() -> int:
    """Import existing usage_YYYY_MM.json files into SQLite. Returns count of entries migrated."""
    import json
    import re

    total = 0
    conn = _get_conn()
    try:
        for f in sorted(_DATA_DIR.glob("usage_*.json")):
            m = re.match(r"usage_(\d{4}_\d{2})\.json$", f.name)
            if not m:
                continue
            month = m.group(1)
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries = data.get("entries", [])
            for e in entries:
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO usage_entries
                           (id, ts, month, client_id, key_id, model, provider_id,
                            prompt_tokens, completion_tokens, total_tokens, pricing_month, cost_usd)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            e.get("id", secrets.token_hex(8)),
                            e.get("ts", ""),
                            month,
                            e.get("client_id", ""),
                            e.get("key_id", ""),
                            e.get("model", ""),
                            e.get("provider_id", ""),
                            e.get("prompt_tokens", 0),
                            e.get("completion_tokens", 0),
                            e.get("total_tokens", 0),
                            e.get("pricing_month", month),
                            e.get("cost_usd", 0.0),
                        ),
                    )
                    total += 1
                except Exception as exc:
                    log.warning("Skipping entry in %s: %s", f.name, exc)
            conn.commit()
            log.info("Migrated %d entries from %s", len(entries), f.name)
    finally:
        conn.close()
    return total


def get_all_used_models() -> set[str]:
    """Get the set of all unique model IDs ever logged in the usage database.

    Called by model availability refresh to safely preserve used models from pruning.
    """
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT DISTINCT model FROM usage_entries").fetchall()
        used = set()
        for r in rows:
            m = r["model"]
            if m:
                used.add(m)
                # Also add just the suffix in case it was logged without provider prefix
                if ":" in m:
                    used.add(m.split(":", 1)[-1])
        return used
    except Exception as exc:
        log.warning("Failed to query used models from usage_entries: %s", exc)
        return set()
    finally:
        conn.close()
