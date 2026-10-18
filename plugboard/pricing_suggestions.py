"""Price suggestions waiting for an admin: ``data/pricing/suggestions.json``.

A suggestion is never billed. It enters the grid only when an admin applies
it. Two types share the store:

- ``drift``: a structured source moved a price by more than
  ``PLUGBOARD_PRICE_DRIFT_PCT``, or disagrees with another source on a first
  price (see ``pricing_merge``);
- ``llm``: a price read from a provider page by a model (MR-D);
- ``estimate``: a model with no price was billed at the price of the closest
  version of its family; applying it makes that price the model's own.

Each suggestion has a stable id derived from its type, model, price space and
proposed prices, so the same proposal keeps its id across refreshes and months,
and a rejected proposal stays rejected until the proposed price changes.
Pending items of a closed month expire.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugboard import billing
from plugboard.pricing_merge import same_price
from plugboard.storage import atomic_write_json

log = logging.getLogger(__name__)

TYPES = ("drift", "llm", "estimate")


def _path() -> Path:
    return billing._PRICING_DIR / "suggestions.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def suggestion_id(kind: str, model: str, price: Dict[str, Any], space: str = "") -> str:
    """Stable id of a proposal: same type, model, space and price, same id.

    The month is not part of it, so a rejected proposal stays rejected after
    the month rolls over and the same price is proposed again.
    """
    parts = [
        kind,
        model,
        f"{float(price['input_price']):.6f}",
        f"{float(price['output_price']):.6f}",
    ]
    if space:
        parts.append(space)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _read() -> Dict[str, Any]:
    path = _path()
    if not path.exists():
        return {"items": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("Failed to read pricing/suggestions.json: %s", exc)
        return {"items": {}}
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        return {"items": {}}
    return data


def _write(data: Dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)


def _lock():
    return billing._lock(_path())


def _expire_closed_months(data: Dict[str, Any], month: str) -> bool:
    """Pending items of a closed month can no longer be applied: expire them."""
    changed = False
    for item in data["items"].values():
        if item.get("status") == "pending" and item.get("month") != month:
            item["status"] = "expired"
            item["updated_at"] = _now()
            changed = True
    return changed


def list_suggestions(status: Optional[str] = "pending") -> List[Dict[str, Any]]:
    with _lock():
        data = _read()
        if _expire_closed_months(data, billing.current_pricing_month()):
            _write(data)
        items = list(data["items"].values())
    if status:
        items = [s for s in items if s.get("status") == status]
    return sorted(items, key=lambda s: (s.get("created_at", ""), s.get("model", "")))


def get(sid: str) -> Optional[Dict[str, Any]]:
    with _lock():
        return _read()["items"].get(sid)


def _record(kind: str, month: str, rows: List[Dict[str, Any]], fields) -> List[str]:
    """Queue ``rows`` of one type; returns the ids of newly pending items.

    A pending or rejected proposal keeps its decision. One that expired, was
    superseded, applied or went stale comes back as a fresh pending item when
    the source proposes it again. Another pending proposal for the same model
    and space is superseded.
    """
    created: List[str] = []
    with _lock():
        data = _read()
        items = data["items"]
        _expire_closed_months(data, month)
        for row in rows:
            model, new, space = row["model"], row["new"], row.get("space", "")
            sid = suggestion_id(kind, model, new, space)
            known = items.get(sid)
            if known is not None and known.get("status") in ("pending", "rejected"):
                continue  # still waiting, or refused: the decision stands
            for other in items.values():
                if (
                    other.get("type") == kind
                    and other.get("model") == model
                    and other.get("space", "") == space
                    and other.get("status") == "pending"
                ):
                    other["status"] = "superseded"
                    other["updated_at"] = _now()
            items[sid] = {
                "id": sid,
                "type": kind,
                "month": month,
                "model": model,
                "space": space,
                "price_key": f"{space}|{model}" if space else model,
                "proposed": dict(new),
                "status": "pending",
                "created_at": _now(),
                **fields(row),
            }
            created.append(sid)
        _write(data)
    return created


def record_drifts(month: str, drifts: List[Dict[str, Any]]) -> List[str]:
    """Queue price changes held by pricing_merge; returns the new pending ids."""
    return _record(
        "drift",
        month,
        drifts,
        lambda d: {
            "source": d.get("source", ""),
            "current": d.get("old"),
            "pct": d.get("pct"),
            "reason": d.get("reason", ""),
        },
    )


def record_llm(month: str, items: List[Dict[str, Any]]) -> List[str]:
    """Queue prices read by a model; returns the new pending ids.

    Each item: ``model``, ``space`` (provider price space), ``new`` price,
    ``source`` (the page read) and ``catalog_sig`` (models.dev entry hash, so
    a rejection is remembered until that entry changes).
    """
    return _record(
        "llm",
        month,
        items,
        lambda i: {
            "source": i.get("source", ""),
            "catalog_sig": i.get("catalog_sig", ""),
            "current": None,
            "pct": None,
        },
    )


def record_estimates(month: str, items: List[Dict[str, Any]]) -> List[str]:
    """Queue prices borrowed from the nearest model; returns the new pending ids.

    Each item: ``model``, ``space``, ``new`` price and ``source`` (the model
    whose price was borrowed).
    """
    return _record(
        "estimate",
        month,
        items,
        lambda i: {
            "source": i.get("source", ""),
            "current": None,
            "pct": None,
            "reason": f"no price found; nearest model is {i.get('source', '?')}",
        },
    )


class SuggestionConflict(Exception):
    """The suggestion or the grid entry changed since it was proposed."""

    def __init__(self, message: str, current: Any = None):
        super().__init__(message)
        self.current = current


def _set_status(data: Dict[str, Any], sid: str, status: str) -> Dict[str, Any]:
    item = data["items"][sid]
    item["status"] = status
    item["updated_at"] = _now()
    _write(data)
    return item


def reject(sid: str) -> Dict[str, Any]:
    with _lock():
        data = _read()
        item = data["items"].get(sid)
        if item is None:
            raise KeyError(sid)
        if item.get("status") != "pending":
            raise SuggestionConflict(f"suggestion is {item.get('status')}", item)
        return _set_status(data, sid, "rejected")


def apply(sid: str) -> Dict[str, Any]:
    """Write the proposed price into the current month's grid.

    The suggestion store stays locked for the whole operation, so a
    concurrent reject cannot land between the check and the grid write.
    Raises ``SuggestionConflict`` when the suggestion is not pending, belongs
    to a closed month, or the grid entry changed since it was proposed.
    """
    month = billing.current_pricing_month()
    with _lock():
        data = _read()
        item = data["items"].get(sid)
        if item is None:
            raise KeyError(sid)
        if item.get("status") != "pending":
            raise SuggestionConflict(f"suggestion is {item.get('status')}", item)
        if item.get("month") != month:
            _set_status(data, sid, "expired")
            raise SuggestionConflict("suggestion belongs to a closed month", item)
        with billing.pricing_lock(month):
            grid = billing.load_pricing(month, strict=True) or {"models": {}}
            space = item.get("space", "")
            table = (
                grid.setdefault("models_by_provider", {}).setdefault(space, {})
                if space
                else grid.setdefault("models", {})
            )
            entry = table.get(item["model"])
            current = item.get("current")
            if current is None:
                changed = entry is not None and bool(
                    entry.get("source")
                    or entry.get("input_price")
                    or entry.get("output_price")
                )
            else:
                changed = entry is None or not same_price(entry, current)
            if changed:
                _set_status(data, sid, "stale")
                raise SuggestionConflict(
                    "the grid entry changed since this suggestion was made", entry
                )
            if item["type"] == "drift":
                source = item.get("source")
            elif item["type"] == "estimate":
                source = "manual"  # an admin confirmed the borrowed price
            else:
                source = "llm"
            table[item["model"]] = {
                **(entry or {}),
                **item["proposed"],
                "source": source or "llm",
            }
            if item["type"] == "llm" and item.get("source"):
                table[item["model"]]["page"] = item[
                    "source"
                ]  # where the price was read
            # The applied price becomes the reference for later drift checks.
            grid.setdefault("reviewed_prices", {}).setdefault(space, {})[
                item["model"]
            ] = dict(item["proposed"])
            billing.save_pricing(month, grid)
        return _set_status(data, sid, "applied")
