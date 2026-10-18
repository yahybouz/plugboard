"""Model quarantine store — auto-disable broken models after repeated fails.

The nightly :mod:`scripts.smoke_models` run probes every model in
``/v1/models`` with an identification prompt. Models that fail with a
non-recoverable error (404, 400, ``model_not_found``, deprecation
sentinels) are marked here. Once the fail counter reaches
``QUARANTINE_THRESHOLD`` consecutive nightly fails, the model ID is
considered "quarantined" and :func:`router.list_models` filters it out
of every API response.

A single subsequent success resets the counter and removes the model
from quarantine — so a temporary upstream outage cannot durably blacklist
a model that came back to life.

State lives in ``data/quarantined_models.json``. Atomic writes via a
temp file so a crash mid-write cannot corrupt the file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from plugboard.storage import data_dir

log = logging.getLogger(__name__)

# 3 consecutive nightly fails before a model is hard-disabled. Lower than
# this and a single bad night could blacklist a flagship model; higher
# and obviously-dead models linger in the dashboard for a full week.
QUARANTINE_THRESHOLD = 3

# These are not transient provider failures: Codex is telling us the
# authenticated ChatGPT account cannot use that model. Hide such entries
# immediately instead of waiting for three nightly probes.
_ACCOUNT_UNSUPPORTED_MARKERS = (
    "model is not supported when using codex with a chatgpt account",
    "not supported when using codex with a chatgpt account",
    "model is not supported for chatgpt accounts",
    "not supported for chatgpt accounts",
    "not available for your account",
    "not enabled for your account",
    "not allowed for this account",
    "you do not have access to this model",
    "you don't have access to this model",
    "account does not have access to",
    "permission_denied",
)

_TERMINAL_UNAVAILABLE_MARKERS = (
    "model_not_found",
    "does not exist",
    "unknown model",
    "model is not available",
    "has been deprecated",
    "deprecated and will reach end-of-life",
    "end-of-life",
    "model is retired",
    "no longer supported",
)

# Located under data/ alongside the pricing snapshots and CB state so the
# whole stateful surface of the proxy stays in one directory.
_QUARANTINE_FILE = data_dir() / "quarantined_models.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_raw(path: Path = _QUARANTINE_FILE) -> Dict[str, Any]:
    """Return the on-disk state. Empty dict on missing or corrupt file —
    a broken state file must never crash the proxy at boot."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"models": {}}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("quarantine: %s unreadable (%s) — starting empty", path, exc)
        return {"models": {}}


def _save_raw(state: Dict[str, Any], path: Path = _QUARANTINE_FILE) -> None:
    """Atomic write — never partial. Best-effort: a write failure must not
    crash the smoke runner; the in-memory state simply doesn't persist."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=".quarantine.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(state, tmp, indent=2, sort_keys=True)
            tmp_path = tmp.name
        os.replace(tmp_path, path)
    except OSError as exc:
        log.warning("quarantine: failed to persist (%s)", exc)


def get_quarantined_suffixes(path: Path = _QUARANTINE_FILE) -> Set[str]:
    """Return the set of model suffixes that are currently quarantined.

    A model is "quarantined" when its consecutive fail count is >=
    :data:`QUARANTINE_THRESHOLD`. Called by :func:`router.list_models` on
    every uncached refresh, so this stays cheap (single file read).
    """
    state = _load_raw(path)
    models = state.get("models") or {}
    if not isinstance(models, dict):
        return set()
    return {
        suffix
        for suffix, entry in models.items()
        if isinstance(entry, dict)
        and int(entry.get("consecutive_fails", 0) or 0) >= QUARANTINE_THRESHOLD
    }


def is_account_unsupported_error(reason: str) -> bool:
    """Return True for hard account/model compatibility failures."""
    text = (reason or "").lower()
    return any(marker in text for marker in _ACCOUNT_UNSUPPORTED_MARKERS)


def is_terminal_model_unavailable_error(reason: str) -> bool:
    """Return True for hard failures that should hide a model immediately."""
    text = (reason or "").lower()
    model_not_available = (
        re.search(r"model ['\"`]?[^'\"`\\s]+['\"`]? is not available", text) is not None
    )
    return (
        is_account_unsupported_error(text)
        or any(marker in text for marker in _TERMINAL_UNAVAILABLE_MARKERS)
        or model_not_available
    )


def record_failure(
    suffix: str,
    *,
    reason: str = "",
    path: Path = _QUARANTINE_FILE,
) -> Dict[str, Any]:
    """Increment the consecutive-fail counter for ``suffix``.

    Returns the updated entry. The caller (smoke runner) logs the
    transition to quarantine when ``consecutive_fails`` crosses
    :data:`QUARANTINE_THRESHOLD`.
    """
    state = _load_raw(path)
    models = state.setdefault("models", {})
    entry = models.get(suffix) or {}
    entry["consecutive_fails"] = int(entry.get("consecutive_fails", 0) or 0) + 1
    entry["last_failure_at"] = _now_iso()
    if reason:
        entry["last_failure_reason"] = reason[:200]
    models[suffix] = entry
    _save_raw(state, path)
    return entry


def record_account_unsupported(
    suffix: str,
    *,
    reason: str = "",
    path: Path = _QUARANTINE_FILE,
) -> Dict[str, Any]:
    """Immediately quarantine a model the current account cannot use."""
    return record_terminal_unavailable(
        suffix,
        reason=reason,
        terminal_reason="account_unsupported",
        path=path,
    )


def record_terminal_unavailable(
    suffix: str,
    *,
    reason: str = "",
    terminal_reason: str = "model_unavailable",
    path: Path = _QUARANTINE_FILE,
) -> Dict[str, Any]:
    """Immediately quarantine a model for a non-transient upstream refusal."""
    state = _load_raw(path)
    models = state.setdefault("models", {})
    entry = models.get(suffix) or {}
    entry["consecutive_fails"] = QUARANTINE_THRESHOLD
    entry["last_failure_at"] = _now_iso()
    entry["terminal"] = True
    entry["terminal_reason"] = terminal_reason
    if reason:
        entry["last_failure_reason"] = reason[:200]
    models[suffix] = entry
    _save_raw(state, path)
    return entry


def record_success(suffix: str, path: Path = _QUARANTINE_FILE) -> None:
    """Reset the fail counter for ``suffix`` and remove it from quarantine.

    A single successful probe is enough to rehabilitate a model — the
    smoke test is conservative (real prompt, real provider), so a green
    run is a strong signal the upstream issue is gone.
    """
    state = _load_raw(path)
    models = state.get("models") or {}
    if suffix in models:
        del models[suffix]
        state["models"] = models
        _save_raw(state, path)


def get_state(path: Path = _QUARANTINE_FILE) -> Dict[str, Any]:
    """Return the full on-disk state. Surfaced via /admin endpoints so
    SREs can see *why* a model is hidden from /v1/models."""
    return _load_raw(path)


def manually_clear(
    suffix: Optional[str] = None, path: Path = _QUARANTINE_FILE
) -> List[str]:
    """Operator escape hatch: drop one specific model from the store, or
    everything if ``suffix`` is None. Returns the list of cleared IDs."""
    state = _load_raw(path)
    models = state.get("models") or {}
    if suffix is None:
        cleared = sorted(models.keys())
        state["models"] = {}
    elif suffix in models:
        cleared = [suffix]
        del models[suffix]
        state["models"] = models
    else:
        cleared = []
    if cleared:
        _save_raw(state, path)
    return cleared
