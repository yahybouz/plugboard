"""Data retention: purge old rows from the SQLite stores.

One synchronous pass deletes the rows older than each store's retention,
compacts the store when enough pages are free, writes a report to
``data/retention.json`` and records an audit line when rows were deleted.

- usage (``usage_entries``): whole months, ``PLUGBOARD_USAGE_RETENTION_MONTHS``;
- audit (``audit_log``): ``PLUGBOARD_AUDIT_RETENTION_DAYS``;
- watchdog (``watchdog_events``): ``PLUGBOARD_WATCHDOG_RETENTION_DAYS``;
- monitoring (``monitoring_snapshots``): ``PLUGBOARD_MONITORING_RETENTION_DAYS``;
- sessions (``chat_sessions``): the session TTL (``PLUGBOARD_SESSION_TTL_S``).

``0`` keeps a store's rows for ever, and so does a retention reaching before
year 1. A value that is not a whole number, or is negative, gives the
default. A store that is missing is skipped without being created; one that
is locked or corrupt is reported in error and the others are still purged.

The pass blocks on SQLite: call it from a worker thread, never on the
event loop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from plugboard import (
    audit,
    envcompat,
    monitoring,
    provider_watchdog,
    session_store,
    storage,
    usage,
)

log = logging.getLogger(__name__)

DEFAULT_USAGE_MONTHS = 13
DEFAULT_AUDIT_DAYS = 365
DEFAULT_WATCHDOG_DAYS = 14
DEFAULT_MONITORING_DAYS = 90
DEFAULT_HOUR_UTC = 4

_TRUE = {"1", "true", "yes"}
_ERROR_MAX_CHARS = 200


def _retention(name: str, default: int) -> int:
    return envcompat.get_int(name, default=default, minimum=0)


def settings() -> Dict[str, Any]:
    """Retention settings read from the environment now."""
    hour = envcompat.get_int(
        "PLUGBOARD_RETENTION_HOUR_UTC", default=DEFAULT_HOUR_UTC, minimum=0
    )
    disabled = envcompat.get("PLUGBOARD_RETENTION_DISABLED").lower() in _TRUE
    return {
        "disabled": disabled,
        "hour_utc": min(hour, 23),
        "usage_months": _retention(
            "PLUGBOARD_USAGE_RETENTION_MONTHS", DEFAULT_USAGE_MONTHS
        ),
        "audit_days": _retention("PLUGBOARD_AUDIT_RETENTION_DAYS", DEFAULT_AUDIT_DAYS),
        "watchdog_days": _retention(
            "PLUGBOARD_WATCHDOG_RETENTION_DAYS", DEFAULT_WATCHDOG_DAYS
        ),
        "monitoring_days": _retention(
            "PLUGBOARD_MONITORING_RETENTION_DAYS", DEFAULT_MONITORING_DAYS
        ),
        "session_ttl_s": session_store._TTL_S,
    }


def report_path() -> Path:
    """Where the last pass's report is written."""
    return storage.data_dir() / "retention.json"


def _month_cutoff(now: datetime, months: int) -> Optional[str]:
    """First month kept (``YYYY_MM``), or None to keep every month."""
    if months == 0:
        return None
    index = now.year * 12 + (now.month - 1) - months
    year, month = divmod(index, 12)
    if year < 1:
        return None
    return f"{year:04d}_{month + 1:02d}"


def _day_cutoff(now: datetime, days: int) -> Optional[str]:
    """Oldest timestamp kept, or None to keep every row.

    Formatted with a four-digit year: ``strftime("%Y")`` drops the leading
    zeros of a year before 1000, and ``"3-..."`` would compare as newer than
    every stored ``"2026-..."`` timestamp.
    """
    if days == 0:
        return None
    try:
        cutoff = now - timedelta(days=days)
    except OverflowError:
        return None
    return cutoff.replace(tzinfo=None).isoformat(timespec="seconds")


def _purge_and_compact(path: Path, purge: Callable[[], int]) -> Dict[str, Any]:
    """Purge one store, then compact it; a failure is reported, never raised.

    The purge is committed before compaction starts: when compaction fails,
    the rows are gone for good and the report keeps their count.
    """
    entry: Dict[str, Any] = {"deleted": 0, "compacted": False}
    try:
        entry["deleted"] = purge()
        entry["compacted"] = storage.compact_sqlite(path)
    except (sqlite3.Error, OSError) as exc:
        message = f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_CHARS]
        log.warning("Retention pass: %s", message)
        entry["error"] = message
    return entry


def _purge_store(
    path: Path, table: str, where: str, cutoff: Optional[str]
) -> Dict[str, Any]:
    if cutoff is None:
        return {"deleted": 0, "compacted": False, "kept": "all"}
    return _purge_and_compact(
        path, lambda: storage.purge_sqlite_rows(path, table, where, (cutoff,))
    )


def run_retention(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Purge every store once and return the report, also written to disk.

    A naive ``now`` is read as UTC. Store paths are read at each call.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    conf = settings()
    month_cutoff = _month_cutoff(now, conf["usage_months"])
    stores = {
        "usage": _purge_store(
            usage._DB_PATH,
            "usage_entries",
            "month < ? AND month GLOB '[0-9][0-9][0-9][0-9]_[0-9][0-9]'",
            month_cutoff,
        ),
        "audit": _purge_store(
            audit._DB_PATH,
            "audit_log",
            "ts < ?",
            _day_cutoff(now, conf["audit_days"]),
        ),
        "watchdog": _purge_store(
            provider_watchdog._DB_PATH,
            "watchdog_events",
            "ts < ?",
            _day_cutoff(now, conf["watchdog_days"]),
        ),
        "monitoring": _purge_store(
            monitoring._DB_PATH,
            "monitoring_snapshots",
            "ts < ?",
            _day_cutoff(now, conf["monitoring_days"]),
        ),
        "sessions": _purge_and_compact(
            session_store._DB_PATH, session_store.purge_expired
        ),
    }
    deleted_total = sum(store["deleted"] for store in stores.values())
    report = {
        "ran_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stores": stores,
        "deleted_total": deleted_total,
        "errors": sum(1 for store in stores.values() if "error" in store),
    }
    if deleted_total > 0:
        details = " ".join(
            f"{name}={store['deleted']}" for name, store in stores.items()
        )
        try:
            audit.log_action("retention.run", actor="system", details=details)
        except Exception:
            log.exception("Retention pass: cannot write the audit line")
    try:
        storage.atomic_write_json(report_path(), report, mode=0o600)
    except Exception:
        log.exception("Retention pass: cannot write %s", report_path())
    return report


def last_report() -> Optional[Dict[str, Any]]:
    """The last pass's report, or None when there is none or it is unreadable."""
    try:
        data = json.loads(report_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
