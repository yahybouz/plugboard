"""Journal and webhooks for automatic catalog and price changes.

Every change found by a refresh pass is appended to
``data/catalog/changes.jsonl`` (the last 1 000 lines are kept) and sent as a
webhook (``models.changed`` or ``pricing.changed``). A pass sends at most
``PLUGBOARD_WEBHOOK_MAX_PER_RUN`` events (default 50); the rest is grouped in
one event per type carrying ``{"truncated": N}``. First populations and
source-only changes are not reported: callers only add real changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

log = logging.getLogger(__name__)

JOURNAL_MAX_LINES = 1000


def _journal_path() -> Path:
    from plugboard.storage import data_dir

    return data_dir() / "catalog" / "changes.jsonl"


def max_per_run() -> int:
    raw = os.environ.get("PLUGBOARD_WEBHOOK_MAX_PER_RUN", "").strip()
    if not raw:
        return 50
    try:
        value = int(raw)
    except ValueError:
        log.warning("PLUGBOARD_WEBHOOK_MAX_PER_RUN is not a number; using 50")
        return 50
    return max(0, value)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_journal(entries: List[Dict[str, Any]]) -> None:
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
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
