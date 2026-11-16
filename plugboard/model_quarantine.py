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
a model that came back to life. A real call that succeeds counts as such a
success for its provider+model pair (BUG-105, :func:`lift_after_success`):
the probes are off in production, so they cannot be the only way back.

An entry belongs to the provider it was observed on (BUG-102): the same
model name can be refused by one provider and served by another, so a
refusal only ever hides, labels or removes the model at its own provider.
Entries recorded without a provider (every entry written before 4.14.1)
cannot say whose refusal they are: they stay in the file, under ``models``,
and are applied to no provider.

State lives in ``data/quarantined_models.json``: ``providers`` maps a
provider ID to its entries, ``models`` holds the entries without a provider.
Atomic writes via a temp file so a crash mid-write cannot corrupt the file.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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


def _entries(state: Dict[str, Any], provider_id: str) -> Dict[str, Any]:
    """The entries of ``provider_id`` in ``state``, created when missing;
    without a provider, the entries recorded for none."""
    if not provider_id:
        models = state.get("models")
        if not isinstance(models, dict):
            models = state["models"] = {}
        return models
    providers = state.get("providers")
    if not isinstance(providers, dict):
        providers = state["providers"] = {}
    entries = providers.get(provider_id)
    if not isinstance(entries, dict):
        entries = providers[provider_id] = {}
    return entries


def _quarantined(entries: Any) -> Set[str]:
    if not isinstance(entries, dict):
        return set()
    return {
        suffix
        for suffix, entry in entries.items()
        if isinstance(entry, dict)
        and int(entry.get("consecutive_fails", 0) or 0) >= QUARANTINE_THRESHOLD
    }


def get_quarantined_suffixes(
    path: Path = _QUARANTINE_FILE, *, provider_id: Optional[str] = None
) -> Set[str]:
    """Return the model suffixes quarantined at ``provider_id``.

    A model is "quarantined" when its consecutive fail count is >=
    :data:`QUARANTINE_THRESHOLD`. Whoever hides, labels or removes a model
    must name the provider it asks about: one provider's refusal says nothing
    of another's (BUG-102). Without ``provider_id`` the names recorded for no
    provider are returned, for the record only: they apply to nobody.
    """
    state = _load_raw(path)
    if provider_id is None:
        return _quarantined(state.get("models"))
    providers = state.get("providers")
    if not provider_id or not isinstance(providers, dict):
        return set()
    return _quarantined(providers.get(provider_id))


def get_quarantined_by_provider(path: Path = _QUARANTINE_FILE) -> Dict[str, Set[str]]:
    """Every provider's quarantined suffixes, in one file read. Called by
    :func:`router.list_models` on every uncached refresh."""
    providers = _load_raw(path).get("providers")
    if not isinstance(providers, dict):
        return {}
    found = {pid: _quarantined(entries) for pid, entries in providers.items()}
    return {pid: suffixes for pid, suffixes in found.items() if suffixes}


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
    provider_id: str = "",
    path: Path = _QUARANTINE_FILE,
) -> Dict[str, Any]:
    """Increment the consecutive-fail counter of ``suffix`` at ``provider_id``.

    Returns the updated entry. The caller (smoke runner) logs the
    transition to quarantine when ``consecutive_fails`` crosses
    :data:`QUARANTINE_THRESHOLD`. Without ``provider_id`` the entry is kept
    for the record and applies to no provider.
    """
    state = _load_raw(path)
    models = _entries(state, provider_id)
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
    provider_id: str = "",
    path: Path = _QUARANTINE_FILE,
) -> Dict[str, Any]:
    """Immediately quarantine a model the current account cannot use."""
    return record_terminal_unavailable(
        suffix,
        reason=reason,
        terminal_reason="account_unsupported",
        provider_id=provider_id,
        path=path,
    )


def record_terminal_unavailable(
    suffix: str,
    *,
    reason: str = "",
    terminal_reason: str = "model_unavailable",
    provider_id: str = "",
    path: Path = _QUARANTINE_FILE,
) -> Dict[str, Any]:
    """Immediately quarantine a model, at ``provider_id``, for a non-transient
    upstream refusal."""
    state = _load_raw(path)
    models = _entries(state, provider_id)
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


def record_success(
    suffix: str, path: Path = _QUARANTINE_FILE, *, provider_id: str = ""
) -> None:
    """Reset the fail counter of ``suffix`` at ``provider_id`` and remove it
    from that provider's quarantine.

    A single successful probe is enough to rehabilitate a model — the
    smoke test is conservative (real prompt, real provider), so a green
    run is a strong signal the upstream issue is gone.
    """
    state = _load_raw(path)
    models = _entries(state, provider_id)
    if suffix in models:
        del models[suffix]
        if provider_id and not models:
            del state["providers"][provider_id]
        _save_raw(state, path)


# (path, inode, mtime_ns, size) of the file last read by lift_after_success,
# and the (provider, model) pairs it held: a success reads the file only when
# it changed (every save is a new file, so a new inode), and the chat path
# pays one stat per call (BUG-105).
_pairs_seen: Tuple[Optional[Tuple[str, int, int, int]], frozenset] = (
    None,
    frozenset(),
)


def _recorded_pairs(path: Path) -> frozenset:
    """The (provider, model) pairs with an entry in ``path``, any count."""
    global _pairs_seen
    try:
        st = path.stat()
    except OSError:
        return frozenset()
    stamp = (str(path), st.st_ino, st.st_mtime_ns, st.st_size)
    seen_stamp, pairs = _pairs_seen
    if stamp == seen_stamp:
        return pairs
    providers = _load_raw(path).get("providers")
    found = set()
    if isinstance(providers, dict):
        for pid, entries in providers.items():
            if isinstance(entries, dict):
                found.update((pid, suffix) for suffix in entries)
    pairs = frozenset(found)
    _pairs_seen = (stamp, pairs)
    return pairs


def lift_after_success(
    suffix: str, *, provider_id: str, path: Optional[Path] = None
) -> bool:
    """A real call to ``suffix`` at ``provider_id`` succeeded: drop that
    pair's entry, quarantined or only counting failures (BUG-105).

    Only that pair: the same name at another provider keeps its entry
    (BUG-102). Returns True when an entry was dropped. Cheap when there is
    none: one ``stat`` of the store, which is read again only once changed.
    """
    if not suffix or not provider_id:
        return False
    store = path if path is not None else _QUARANTINE_FILE
    if (provider_id, suffix) not in _recorded_pairs(store):
        return False
    record_success(suffix, store, provider_id=provider_id)
    return True


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
