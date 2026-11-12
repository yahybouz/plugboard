"""Journal and webhooks for automatic catalog and price changes.

Every change found by a refresh pass is appended to
``data/catalog/changes.jsonl`` and sent as a webhook (``models.changed`` or
``pricing.changed``). A pass sends at most ``PLUGBOARD_WEBHOOK_MAX_PER_RUN``
events (default 50); the rest is grouped in one event per type carrying
``{"truncated": N}``. First populations and source-only changes are not
reported: callers only add real changes.

The journal keeps a change for ``PLUGBOARD_CHANGE_JOURNAL_DAYS`` days (365;
``0`` = no age limit) and at most ``JOURNAL_MAX_LINES`` lines (10 000); older
lines are dropped when a change is written.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from plugboard import envcompat

log = logging.getLogger(__name__)

JOURNAL_MAX_LINES = 10000
DEFAULT_RETENTION_DAYS = 365


def _journal_path() -> Path:
    from plugboard.storage import data_dir

    return data_dir() / "catalog" / "changes.jsonl"


def max_per_run() -> int:
    return max(0, envcompat.get_int("PLUGBOARD_WEBHOOK_MAX_PER_RUN", default=50))


def _utcnow() -> datetime:
    """The module's only clock (tests replace it to pin fixed dates)."""
    return datetime.now(timezone.utc)


def _now() -> str:
    return _utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def retention_days() -> int:
    """Days a change stays in the journal; 0 keeps it until the line cap."""
    return envcompat.get_int(
        "PLUGBOARD_CHANGE_JOURNAL_DAYS", default=DEFAULT_RETENTION_DAYS, minimum=0
    )


def cutoff(now: Optional[datetime] = None) -> Optional[datetime]:
    """Oldest date still kept, or None when there is no age limit."""
    days = retention_days()
    if days == 0:
        return None
    return (now or _utcnow()) - timedelta(days=days)


def _parse_at(value: Any) -> Optional[datetime]:
    """A journal ``at`` as an aware UTC datetime; None when unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _too_old(line: str, limit: datetime) -> bool:
    """True for a line whose readable ``at`` is before ``limit``."""
    try:
        entry = json.loads(line)
    except ValueError:
        return False
    if not isinstance(entry, dict):
        return False
    at = _parse_at(entry.get("at"))
    return at is not None and at < limit


def append_journal(
    entries: List[Dict[str, Any]], now: Optional[datetime] = None
) -> None:
    """Append ``entries``, then drop lines past the age limit and the line cap.

    A line without a readable ``at`` is only dropped by the line cap.
    """
    if not entries:
        return
    from plugboard.billing import _lock
    from plugboard.storage import atomic_write_text

    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(path):
        lines: List[str] = []
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                log.warning("Cannot read %s: %s", path, exc)
        lines.extend(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in entries)
        limit = cutoff(now)
        if limit is not None:
            lines = [line for line in lines if not _too_old(line, limit)]
        atomic_write_text(path, "\n".join(lines[-JOURNAL_MAX_LINES:]) + "\n")


def read_journal(limit: int = 100) -> List[Dict[str, Any]]:
    path = _journal_path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines()[-max(1, limit) :]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class ChangeBatch:
    """Changes of one refresh pass, journaled and sent together."""

    def __init__(self) -> None:
        self.events: List[Tuple[str, Dict[str, Any]]] = []

    def add(self, event: str, data: Dict[str, Any]) -> None:
        self.events.append((event, data))

    def __len__(self) -> int:
        return len(self.events)

    async def flush(self) -> Dict[str, int]:
        """Journal every change, then send webhooks up to the per-run cap."""
        if not self.events:
            return {"journaled": 0, "sent": 0, "truncated": 0}
        append_journal([{"at": _now(), "event": e, **d} for e, d in self.events])
        return await self._send()

    async def _send(self) -> Dict[str, int]:
        from plugboard import webhooks

        cap = max_per_run()
        sent = 0
        truncated: Dict[str, int] = {}
        for index, (event, data) in enumerate(self.events):
            if index < cap:
                try:
                    await webhooks.emit(event, data)
                    sent += 1
                except Exception as exc:  # noqa: BLE001 - a webhook never breaks a refresh
                    log.warning("Webhook %s failed: %s", event, exc)
            else:
                truncated[event] = truncated.get(event, 0) + 1
        for event, count in truncated.items():
            try:
                await webhooks.emit(event, {"truncated": count})
            except Exception as exc:  # noqa: BLE001
                log.warning("Webhook %s summary failed: %s", event, exc)
        journaled = len(self.events)
        self.events = []
        return {
            "journaled": journaled,
            "sent": sent,
            "truncated": sum(truncated.values()),
        }

    def flush_in_background(self, deadline_s: float = 120.0) -> Dict[str, int]:
        """Journal now; send the webhooks in a background task.

        A slow or unreachable receiver must not stretch a pricing update or a
        discover request (nor count against the pricing circuit breaker).
        Returns what was journaled; webhook delivery is best effort.
        """
        if not self.events:
            return {"journaled": 0, "sent": 0, "truncated": 0}
        events, self.events = self.events, []
        append_journal([{"at": _now(), "event": e, **d} for e, d in events])
        batch = ChangeBatch()
        batch.events = events

        async def _send() -> None:
            try:
                await asyncio.wait_for(batch._send(), timeout=deadline_s)
            except asyncio.TimeoutError:
                log.warning("Webhooks: delivery stopped after %ss", deadline_s)
            except Exception as exc:  # noqa: BLE001
                log.warning("Webhooks: delivery failed: %s", exc)

        try:
            task = asyncio.get_running_loop().create_task(_send())
        except RuntimeError:  # no running loop
            return {"journaled": len(events), "sent": 0, "truncated": 0}
        _pending.add(task)
        task.add_done_callback(_pending.discard)
        return {"journaled": len(events), "sent": 0, "truncated": 0}


_pending: set = set()


async def drain() -> None:
    """Wait for background webhook deliveries (tests, shutdown)."""
    # Only the unfinished ones: a delivery that ended stays listed until its
    # done callback runs, and gather() of finished tasks returns without
    # giving the loop that turn, so waiting on it again would spin for ever.
    while True:
        running = [task for task in _pending if not task.done()]
        if not running:
            return
        await asyncio.gather(*running, return_exceptions=True)
