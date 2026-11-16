"""Watch of the shared Claude quota, from real calls only (PLU-67).

One Claude account serves production, UAT and development (D-59): a
development session can spend the quota production needs. The router tells
this module, at the place it pauses a route after a quota error (PLU-9),
that a Claude provider said its limit is reached: a claude CLI whose error
is a usage limit ("Claude AI usage limit reached", "You've hit your limit",
"5-hour limit reached", "You've reached your weekly limit", an API 429 it
reports; :func:`plugboard.cli_subprocess.is_quota_error` and claude's
structured rate-limit event decide), or a 429 of an Anthropic API provider.
Nothing is ever sent to find out: no probe, no extra call.

What is kept, in ``data/claude_quota.json`` (atomic writes, read again on
each view so every worker and a restart see the same state):

- ``last``: when the last limit was met, on which provider and model, a
  short excerpt of the error (secrets masked, at most :data:`EXCERPT_MAX`
  characters), and when the quota reopens when the error says so (a
  ``Retry-After``, a "try again at HH:MM", or the ``|<epoch>`` that follows
  "usage limit reached"); ``null`` otherwise ("resets 3pm" has no zone).
- ``providers``: per provider, the limits met (``count``) and the last one.

An episode is the limits a provider meets less than :data:`AUDIT_WINDOW_S`
apart: its first limit writes one ``provider.quota_limit`` entry in the
audit log (actor ``system``), not every call of a burst.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set
from urllib.parse import urlsplit

from plugboard.storage import atomic_write_json, data_dir

log = logging.getLogger(__name__)

# One audit entry per provider and episode: limits less than an hour apart.
AUDIT_WINDOW_S = 3600
# Longest excerpt of the error kept and shown.
EXCERPT_MAX = 200

# "Claude AI usage limit reached|1731700000": the epoch the quota reopens.
_RESET_EPOCH = re.compile(r"limit reached\|(\d{9,11})\b", re.I)
# An epoch further than this from now is not believed.
_MAX_RESET_AHEAD_S = 8 * 24 * 3600

_lock = threading.Lock()
_tasks: Set[asyncio.Task] = set()

# Wall clock of the records; a test replaces it.
_now = time.time


def _state_path() -> Path:
    return data_dir() / "claude_quota.json"


def is_claude_provider(provider: Mapping[str, Any]) -> bool:
    """Whether ``provider`` draws on a Claude quota: the claude CLI, or an
    Anthropic API provider (its preset, or a base URL on anthropic.com)."""
    if provider.get("auth_source") == "subprocess:claude":
        return True
    if provider.get("preset_key") == "anthropic":
        return True
    host = (urlsplit(str(provider.get("base_url") or "")).hostname or "").lower()
    return host == "anthropic.com" or host.endswith(".anthropic.com")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> float:
    return (
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def _excerpt(message: str) -> str:
    from plugboard.log_redact import redact

    text = " ".join(redact(message or "", max_len=4096).split())
    if len(text) > EXCERPT_MAX:
        text = text[: EXCERPT_MAX - 3] + "..."
    return text


def _resumes_at(
    message: str, retry_after: Optional[int], now: float
) -> Optional[float]:
    if retry_after is not None:
        return now + max(0, int(retry_after))
    match = _RESET_EPOCH.search(message or "")
    if match:
        epoch = float(match[1])
        if now - 24 * 3600 <= epoch <= now + _MAX_RESET_AHEAD_S:
            return epoch
    return None


def _load() -> Dict[str, Any]:
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("claude quota: state unreadable (%s), read as empty", exc)
        return {}
    return state if isinstance(state, dict) else {}


def record_limit(
    provider_id: str,
    model: str,
    *,
    source: str,
    message: str,
    retry_after: Optional[int] = None,
) -> Dict[str, Any]:
    """Note one limit met by a real call; return the stored ``last`` record.

    Writes one audit entry when it opens a new episode for the provider.
    """
    now = _now()
    resumes = _resumes_at(message, retry_after, now)
    last = {
        "at": iso(now),
        "provider": provider_id,
        "model": model,
        "source": source,
        "message": _excerpt(message),
        "resumes_at": iso(resumes) if resumes is not None else None,
    }
    with _lock:
        state = _load()
        providers = state.get("providers")
        if not isinstance(providers, dict):
            providers = state["providers"] = {}
        entry = providers.get(provider_id)
        if not isinstance(entry, dict):
            entry = {}
        previous = entry.get("last_ts")
        new_episode = not isinstance(previous, (int, float)) or (
            now - previous >= AUDIT_WINDOW_S
        )
        providers[provider_id] = {
            "count": int(entry.get("count", 0) or 0) + 1,
            "last_ts": now,
            "last_limit_at": iso(now),
        }
        state["last"] = last
        try:
            atomic_write_json(_state_path(), state)
        except Exception as exc:  # the request's answer never depends on it
            log.warning("claude quota: state not saved (%s)", exc)
    log.warning(
        "Claude quota limit reached | provider=%s model=%s resumes_at=%s",
        provider_id,
        model,
        last["resumes_at"],
    )
    if new_episode:
        try:
            from plugboard import audit

            audit.log_action(
                "provider.quota_limit",
                actor="system",
                target_type="provider",
                target_id=provider_id,
                details=json.dumps(
                    {
                        "model": model,
                        "source": source,
                        "message": last["message"],
                        "resumes_at": last["resumes_at"],
                    }
                ),
            )
        except Exception as exc:
            log.warning("claude quota: audit entry failed (%s)", exc)
    return last


def note_limit(provider: Mapping[str, Any], model: str, exc: BaseException) -> None:
    """The router met ``exc``, a quota error, on ``provider``: record it when
    the provider is a Claude one. Never raises, never waits: the file and
    the audit log are written off the event loop."""
    try:
        if not is_claude_provider(provider):
            return
        from plugboard.cli_subprocess import CliSubprocessError

        kwargs = {
            "source": "cli" if isinstance(exc, CliSubprocessError) else "http",
            "message": str(exc),
            "retry_after": getattr(exc, "retry_after", None),
        }
        provider_id = str(provider.get("id") or "")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            record_limit(provider_id, model, **kwargs)
            return
        task = loop.create_task(
            asyncio.to_thread(record_limit, provider_id, model, **kwargs)
        )
        _tasks.add(task)  # keep a reference until it finishes
        task.add_done_callback(_tasks.discard)
    except Exception as err:
        log.warning("claude quota: limit not recorded (%s)", err)


async def settle() -> None:
    """Wait for the records still being written (tests, shutdown)."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def snapshot() -> Dict[str, Any]:
    """The admin view: the last limit met, and the count per provider."""
    state = _load()
    last = state.get("last") if isinstance(state.get("last"), dict) else {}
    providers = state.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    return {
        "last_limit_at": last.get("at"),
        "provider": last.get("provider"),
        "model": last.get("model"),
        "source": last.get("source"),
        "message": last.get("message"),
        "resumes_at": last.get("resumes_at"),
        "providers": {
            pid: {
                "count": int(entry.get("count", 0) or 0),
                "last_limit_at": entry.get("last_limit_at"),
            }
            for pid, entry in providers.items()
            if isinstance(entry, dict)
        },
    }


def prometheus_lines(label) -> List[str]:
    """``plugboard_claude_limit_total`` and its last timestamp, per provider.

    ``label`` escapes a label value (metrics._label).
    """
    providers = _load().get("providers")
    if not isinstance(providers, dict):
        providers = {}
    rows = [
        (pid, entry)
        for pid, entry in sorted(providers.items())
        if isinstance(entry, dict)
    ]
    lines = [
        "# HELP plugboard_claude_limit_total Claude quota limits met by real "
        "calls (claude CLI usage limit, Anthropic API 429), per provider",
        "# TYPE plugboard_claude_limit_total counter",
    ]
    for pid, entry in rows:
        lines.append(
            f'plugboard_claude_limit_total{{provider="{label(pid)}"}} '
            f"{int(entry.get('count', 0) or 0)}"
        )
    lines += [
        "# HELP plugboard_claude_limit_last_timestamp_seconds Unix time of the "
        "last Claude quota limit met, per provider",
        "# TYPE plugboard_claude_limit_last_timestamp_seconds gauge",
    ]
    for pid, entry in rows:
        ts = entry.get("last_ts")
        if isinstance(ts, (int, float)):
            lines.append(
                f'plugboard_claude_limit_last_timestamp_seconds{{provider="{label(pid)}"}} '
                f"{float(ts)!r}"
            )
    return lines
