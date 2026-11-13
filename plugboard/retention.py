"""Data retention: purge old rows from the SQLite stores.

One synchronous pass deletes the rows older than each store's retention,
compacts the store when enough pages are free (never the usage and session
stores, which requests write: BUG-76), writes a report to
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
# A scheduled pass more than this long after the last one is held: a clock
# running ahead would delete rows that should be kept (BUG-70).
MAX_CLOCK_STEP_DAYS = 7

_TRUE = {"1", "true", "yes"}
_ERROR_MAX_CHARS = 200


# Shortest retention a non-zero value may give (BUG-71): an audit kept 12
# days, or usage kept one month, leaves too little to investigate or bill.
FLOORS = {
    "PLUGBOARD_USAGE_RETENTION_MONTHS": 3,
    "PLUGBOARD_AUDIT_RETENTION_DAYS": 90,
    "PLUGBOARD_WATCHDOG_RETENTION_DAYS": 7,
    "PLUGBOARD_MONITORING_RETENTION_DAYS": 7,
}
_floor_warned: set = set()


def _retention(name: str, default: int) -> int:
    value = envcompat.get_int(name, default=default, minimum=0)
    floor = FLOORS.get(name, 0)
    if 0 < value < floor:
        if (name, value) not in _floor_warned:
            _floor_warned.add((name, value))
            log.warning(
                "%s=%d is below its floor: %d is used (0 keeps everything)",
                name,
                value,
                floor,
            )
        return floor
    return value


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


def _purge_and_compact(
    path: Path, purge: Callable[[], int], *, compact: bool = True
) -> Dict[str, Any]:
    """Purge one store, then compact it; a failure is reported, never raised.

    The purge is committed before compaction starts: when compaction fails,
    the rows are gone for good and the report keeps their count. The purge
    commits in batches: when it fails after some of them, its
    ``storage.PartialPurgeError`` gives the rows already deleted, which the
    report keeps too, with the error that stopped it.
    """
    entry: Dict[str, Any] = {"deleted": 0, "compacted": False}
    try:
        entry["deleted"] = purge()
        if compact:
            entry["compacted"] = storage.compact_sqlite(path)
    except (sqlite3.Error, OSError) as exc:
        reported: BaseException = exc
        if isinstance(exc, storage.PartialPurgeError):
            entry["deleted"] = exc.deleted
            reported = exc.__cause__ or exc
        message = f"{type(reported).__name__}: {reported}"[:_ERROR_MAX_CHARS]
        log.warning("Retention pass: %s", message)
        entry["error"] = message
    return entry


def _purge_store(
    path: Path, table: str, where: str, cutoff: Optional[str], *, compact: bool = True
) -> Dict[str, Any]:
    if cutoff is None:
        return {"deleted": 0, "compacted": False, "kept": "all"}
    return _purge_and_compact(
        path,
        lambda: storage.purge_sqlite_rows(path, table, where, (cutoff,)),
        compact=compact,
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
            # Never compacted (BUG-76): VACUUM locks the store while it runs,
            # and every billing write waited up to 5 s on the event loop, then
            # failed. SQLite reuses the freed pages for the next rows instead.
            compact=False,
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
            # Written by chat requests too: not compacted, as usage (BUG-76).
            session_store._DB_PATH,
            session_store.purge_expired,
            compact=False,
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


# A daily pass older than this when the server starts was missed (BUG-72).
OVERDUE_AFTER = timedelta(hours=25)


def _last_ran_at() -> Optional[datetime]:
    try:
        ran_at = datetime.strptime(
            str((last_report() or {}).get("ran_at")), "%Y-%m-%dT%H:%M:%SZ"
        )
    except ValueError:
        return None
    return ran_at.replace(tzinfo=timezone.utc)


def pass_overdue(now: Optional[datetime] = None) -> bool:
    """True when the last pass is more than a day old (BUG-72).

    The server was not running at the daily hour: the pass runs soon after
    the start instead of a day later (and the clock check still applies).
    Never true before the first pass, which waits for the daily hour.
    """
    ran_at = _last_ran_at()
    if ran_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now - ran_at > OVERDUE_AFTER


def clock_check(now: Optional[datetime] = None) -> Optional[str]:
    """Why a scheduled pass must not run now, or None (BUG-70).

    The clock is compared with the last pass: more than
    ``MAX_CLOCK_STEP_DAYS`` after it, the server clock may be running ahead,
    and a pass would delete every row older than that wrong date. A pass
    held this way writes nothing, so the next one is held too until an
    admin checks the clock and runs it from the Jobs page (manual runs are
    not held). Without a previous pass there is nothing to compare with.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ran_at = _last_ran_at()
    if ran_at is None:
        return None
    gap = now - ran_at
    if gap <= timedelta(days=MAX_CLOCK_STEP_DAYS):
        return None
    return (
        f"held: the clock reads {now:%Y-%m-%d %H:%M} UTC, {gap.days} days after "
        f"the last pass ({ran_at:%Y-%m-%dT%H:%M:%SZ}); a clock running ahead would delete "
        "data that should be kept. Check the server clock, then run the pass "
        "from the Jobs page."
    )


def last_report() -> Optional[Dict[str, Any]]:
    """The last pass's report, or None when there is none or it is unreadable."""
    try:
        data = json.loads(report_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
