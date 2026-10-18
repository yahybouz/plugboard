"""Apply a discovered model list to a provider: one diff for every path.

The scheduler, a manual ``POST /providers/{id}/discover`` and the family
pruning of ``refresh_all_model_catalogs`` all write a provider's ``models``
through :func:`apply`, under the providers store lock. The one exception is
the background discovery right after a provider is created from a preset
(``app._schedule_initial_discovery``): it replaces the preset's starting list
with what the key can use, which is a first population, not a change.

Rules:
- a discovery that failed or returned nothing changes nothing;
- the scheduler removes a model only after ``PLUGBOARD_MODEL_REMOVAL_PASSES``
  (default 2) valid discoveries in a row without it; a manual discover is an
  explicit admin action and removes at once;
- a scheduler pass that would drop more than half of the list is treated as
  an anomaly of the provider and not applied;
- ``default_model`` and ``pinned_models`` are never removed;
- the first population of a provider is not reported as a change.

Removing a model from the list hides it from ``GET /v1/models``; a request
naming ``provider:model`` explicitly is still forwarded to the provider.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

MODES = ("scheduler", "manual")
MAX_DROP_RATIO = 0.5


def removal_passes() -> int:
    raw = os.environ.get("PLUGBOARD_MODEL_REMOVAL_PASSES", "").strip()
    if not raw:
        return 2
    try:
        return max(1, int(raw))
    except ValueError:
        log.warning("PLUGBOARD_MODEL_REMOVAL_PASSES is not a number; using 2")
        return 2


def _state_path() -> Path:
    from plugboard.storage import data_dir

    return data_dir() / "catalog" / "model_absences.json"


def _read_state() -> Dict[str, Dict[str, int]]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Cannot read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: Dict[str, Dict[str, int]]) -> None:
    from plugboard.storage import atomic_write_json

    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state)


def _ids(entries: Iterable[Any]) -> List[str]:
    out = []
    for e in entries or []:
        mid = e.get("id") if isinstance(e, dict) else e
        if isinstance(mid, str) and mid:
            out.append(mid)
    return out


def _entry(mid: str, by_id: Dict[str, Any]) -> Dict[str, Any]:
    e = by_id.get(mid)
    return dict(e) if isinstance(e, dict) else {"id": mid}


@dataclass
class DiffResult:
    provider: str
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    pending_removal: Dict[str, int] = field(default_factory=dict)  # id -> absences
    first_population: bool = False
    error: str = ""
    applied: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "added": self.added,
            "removed": self.removed,
            "pending_removal": self.pending_removal,
            "first_population": self.first_population,
            "error": self.error,
            "applied": self.applied,
        }


def apply(
    provider_id: str,
    discovered: Optional[List[Any]],
    mode: str = "scheduler",
    *,
    pruned: Iterable[str] = (),
    default_model: Optional[str] = None,
    dry_run: bool = False,
    allow_removals: bool = True,
) -> DiffResult:
    """Write ``discovered`` (model entries or ids) into the provider's list.

    ``discovered=None`` or an empty list means the discovery was not valid
    (error, non-OpenAI answer, empty list): nothing changes. ``pruned`` ids
    are removed even when discovered (family pruning). ``default_model``,
    when given, replaces the provider's default. ``dry_run`` computes the
    diff without writing anything (preview before a manual discover).
    ``allow_removals=False`` keeps every current model: a list that did not
    come from the provider's own model API may add models, never remove them.
    """
    from plugboard.config import modify_provider
    from plugboard.billing import _lock

    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    result = DiffResult(provider=provider_id)
    new_ids = list(dict.fromkeys(_ids(discovered or [])))
    if not new_ids:
        result.error = "discovery returned no valid model list; nothing changed"
        return result
    pruned_set = set(pruned)
    by_id = {
        e["id"]: e for e in (discovered or []) if isinstance(e, dict) and e.get("id")
    }
    passes = removal_passes()

    with _lock(_state_path()):
        state = _read_state()
        absences = dict(state.get(provider_id) or {})

        def change(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            current = _ids(record.get("models") or [])
            current_by_id = {
                e["id"]: e
                for e in (record.get("models") or [])
                if isinstance(e, dict) and e.get("id")
            }
            protected = set(record.get("pinned_models") or [])
            default = default_model or record.get("default_model") or ""
            if default:
                protected.add(default)
            first = not current
            result.first_population = first
            new_set = set(new_ids)
            missing = [m for m in current if m not in new_set]
            if mode == "scheduler" and not first and len(current) >= 2:
                if len(missing) > len(current) * MAX_DROP_RATIO:
                    result.error = (
                        f"discovery dropped {len(missing)} of {len(current)} models in one pass; "
                        "not applied (provider anomaly?)"
                    )
                    return None
            keep_missing: List[str] = []
            for mid in missing:
                if mid in protected or not allow_removals:
                    keep_missing.append(mid)
                    absences.pop(mid, None)
                    continue
                if mode == "manual":
                    result.removed.append(mid)
                    absences.pop(mid, None)
                    continue
                count = int(absences.get(mid, 0)) + 1
                if count >= passes:
                    result.removed.append(mid)
                    absences.pop(mid, None)
                else:
                    absences[mid] = count
                    result.pending_removal[mid] = count
                    keep_missing.append(mid)
            for mid in new_ids:
                absences.pop(mid, None)
            final_ids = [m for m in new_ids if m not in pruned_set or m in protected]
            final_ids += [
                m for m in keep_missing if m not in pruned_set or m in protected
            ]
            for mid in current:
                if (
                    mid in pruned_set
                    and mid not in protected
                    and mid not in result.removed
                ):
                    result.removed.append(mid)
            current_set = set(current)
            if not first:
                result.added = [m for m in final_ids if m not in current_set]
            final = [
                _entry(m, by_id) if m in by_id else _entry(m, current_by_id)
                for m in final_ids
            ]
            updates: Dict[str, Any] = {}
            if final_ids != current:
                updates["models"] = final
            if default_model and default_model != record.get("default_model"):
                updates["default_model"] = default_model
            if dry_run:
                return None
            result.applied = bool(updates)
            return updates or None

        record = modify_provider(provider_id, change)
        if record is None:
            result.error = f"unknown provider {provider_id}"
            return result
        if not dry_run:
            if absences:
                state[provider_id] = absences
            else:
                state.pop(provider_id, None)
            _write_state(state)
    if result.first_population:
        result.added, result.removed = [], []
    return result
