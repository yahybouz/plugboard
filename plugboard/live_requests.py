"""Live view of the chat requests being served (PLU-13).

A slow call (CLI spawn 5-13 s, a 27b Ollama model for minutes) looks the same
as a hung one from the outside. This module keeps, in memory, the requests in
flight and the last finished ones so that every surface (dashboard, MCP, API)
can answer "is it running or did it break?": provider, elapsed time, phase,
then the outcome.

Phases, in order of appearance:

* ``waiting_slot``  queued behind ``PLUGBOARD_MAX_INFLIGHT_CHAT``
* ``running``       admitted, the provider is being called
* ``waiting_provider``  every candidate provider is at its concurrency cap
* ``cli_queue``     CLI provider: waiting for a spawn slot
* ``cli_running``   CLI provider: child process started (``pid``)
* ``cli_parse``     CLI provider: process exited, reading its answer

Nothing is persisted and no prompt, answer or secret is stored: only ids,
the model name the caller asked for, the provider id and timings.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from plugboard.logging_config import _request_id_var

SURFACES = frozenset({"api", "playground", "mcp"})
SURFACE_HEADER = "X-Plugboard-Surface"

_RECENT_MAX = 50
# An entry whose request never reached ``finish`` (cancelled task, crash) must
# not stay "running" forever.
_STALE_AFTER_S = 3600.0
# Above this, a request is flagged ``slow`` (informational, not an error).
SLOW_AFTER_S = 60.0

# Fields only the admin sees: they reveal routing (provider) or the process.
_ADMIN_ONLY = frozenset({"provider", "pid", "client_id"})

_lock = threading.Lock()
_active: Dict[str, Dict[str, Any]] = {}
_recent: Deque[Dict[str, Any]] = collections.deque(maxlen=_RECENT_MAX)


def normalize_surface(value: Optional[str]) -> str:
    """Closed set: an unknown header value never reaches the output."""
    v = (value or "").strip().lower()
    return v if v in SURFACES else "api"


def begin(rid: str, path: str, surface: Optional[str] = None) -> None:
    if not rid or rid == "-":
        return
    now = time.time()
    with _lock:
        _active[rid] = {
            "id": rid,
            "path": path,
            "surface": normalize_surface(surface),
            "client_id": None,
            "model": None,
            "provider": None,
            "phase": "running",
            "pid": None,
            "started_at": now,
            "phase_since": now,
            "status": None,
            "ended_at": None,
        }


def update(rid: Optional[str] = None, **fields: Any) -> None:
    """Set fields on the request ``rid`` (default: the current one)."""
    rid = rid or _request_id_var.get()
    with _lock:
        entry = _active.get(rid)
        if entry is None:
            return
        if "phase" in fields and fields["phase"] != entry["phase"]:
            entry["phase_since"] = time.time()
        for key, value in fields.items():
            if key in entry:
                entry[key] = value


def set_status(rid: str, status: int) -> None:
    """HTTP status of the answer (set by the middleware, which sees it)."""
    with _lock:
        entry = _active.get(rid)
        if entry is None:
            for old in _recent:
                if old["id"] == rid:
                    old["status"] = status
                    return
            return
        entry["status"] = status


def finish(rid: str) -> None:
    now = time.time()
    with _lock:
        entry = _active.pop(rid, None)
        if entry is None:
            return
        entry["ended_at"] = now
        entry["phase"] = "done"
        _recent.appendleft(entry)


def reset() -> None:
    """Test helper."""
    with _lock:
        _active.clear()
        _recent.clear()


def _view(entry: Dict[str, Any], admin: bool, now: float) -> Dict[str, Any]:
    ended = entry["ended_at"]
    elapsed = (ended or now) - entry["started_at"]
    out = {
        k: v
        for k, v in entry.items()
        if k not in ("started_at", "phase_since", "ended_at")
        and (admin or k not in _ADMIN_ONLY)
    }
    out["elapsed_s"] = round(elapsed, 1)
    if ended is None:
        out["phase_elapsed_s"] = round(now - entry["phase_since"], 1)
        out["slow"] = elapsed > SLOW_AFTER_S
    else:
        status = entry["status"]
        out["outcome"] = (
            "unknown" if status is None else ("ok" if status < 400 else "error")
        )
    return out


def snapshot(client_id: Optional[str] = None, admin: bool = False) -> Dict[str, Any]:
    """Active and recent requests.

    ``admin`` sees everything, with provider and pid; otherwise only the
    requests of ``client_id``, without routing details.
    """
    now = time.time()
    with _lock:
        for rid in [
            r for r, e in _active.items() if now - e["started_at"] > _STALE_AFTER_S
        ]:
            entry = _active.pop(rid)
            entry["ended_at"] = now
            entry["phase"] = "done"
            entry["status"] = entry["status"] or 499
            _recent.appendleft(entry)
        entries_active: List[Dict[str, Any]] = list(_active.values())
        entries_recent: List[Dict[str, Any]] = list(_recent)

    def visible(e: Dict[str, Any]) -> bool:
        return admin or (client_id is not None and e["client_id"] == client_id)

    active = [_view(e, admin, now) for e in entries_active if visible(e)]
    active.sort(key=lambda e: -e["elapsed_s"])
    recent = [_view(e, admin, now) for e in entries_recent if visible(e)]
    return {
        "active_count": len(active),
        "active": active,
        "recent": recent[:20],
        "server_time": round(now, 1),
    }
