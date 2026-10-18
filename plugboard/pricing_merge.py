"""Merge prices from several sources into a monthly pricing grid.

Pure functions only: no I/O, no clock. ``pricing_scraper`` fetches the sources
and writes the grid; this module decides, model by model, which price wins.

Priority of an entry, highest first::

    manual  >  external > provider_native > models_dev > litellm > known  >  (no source)  >  llm
    (admin)    (structured sources, in PLUGBOARD_PRICING_SOURCES order)      (old grids)    (suggestion applied)

Rules:
- a source only replaces an entry of the same or a lower priority, so a
  source that does not answer during a pass never lets a lower one take over;
- ``manual`` entries are never replaced;
- a price change above the drift threshold, or a first price that disagrees
  with a lower-priority source by more than the threshold, is returned as a
  drift to review instead of being written (unless the action is ``apply``);
- ``external`` is the operator's own source and is never held for review;
- an entry priced 0/0 by no one in particular (older grids used it as a
  placeholder) is treated as unpriced, so a real price fills it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

log = logging.getLogger(__name__)

STRUCTURED_SOURCES = ("external", "provider_native", "models_dev", "litellm", "known")
DEFAULT_ORDER = list(STRUCTURED_SOURCES)
MAX_PRICE = 1000.0  # USD per 1M tokens
# output / input. Published prices reach 40x (Ollama nemotron-3-super:
# $0.015 in, $0.60 out), so the ceiling sits at 50.
MIN_RATIO, MAX_RATIO = 0.1, 50.0
_EPSILON = 1e-9
TRUSTED_SOURCES = frozenset({"external"})

_warned: set = set()


def _log_once(level: int, key: str, message: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        log.log(level, message, *args)


def _warn_once(key: str, message: str, *args: Any) -> None:
    _log_once(logging.WARNING, key, message, *args)


def pricing_source_url() -> str:
    """PLUGBOARD_PRICING_SOURCE_URL, or the older PRICING_SOURCE_URL."""
    from plugboard import envcompat

    return envcompat.get("PLUGBOARD_PRICING_SOURCE_URL", "PRICING_SOURCE_URL")


def source_order(external_configured: Optional[bool] = None) -> List[str]:
    """Structured sources to use, highest priority first.

    Unset ``PLUGBOARD_PRICING_SOURCES`` with an external source configured
    keeps the behaviour of earlier releases (external, then built-in prices).
    An invalid value falls back to the default order and logs an error.
    """
    raw = os.environ.get("PLUGBOARD_PRICING_SOURCES", "").strip()
    if not raw:
        if external_configured is None:
            external_configured = bool(pricing_source_url())
        if external_configured:
            _warn_once(
                "unchanged",
                "Pricing sources unchanged (external, known). Set PLUGBOARD_PRICING_SOURCES "
                "to add models.dev and LiteLLM. See docs/guide/pricing.md#sources",
            )
            return ["external", "known"]
        return list(DEFAULT_ORDER)
    order = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [part for part in order if part not in STRUCTURED_SOURCES]
    if unknown or not order:
        _log_once(
            logging.ERROR,
            f"bad:{raw}",
            "PLUGBOARD_PRICING_SOURCES: unknown source %s (valid: %s); using the default order. "
            "See docs/guide/configuration.md#pricing",
            ", ".join(unknown) or "(empty)",
            ", ".join(STRUCTURED_SOURCES),
        )
        return list(DEFAULT_ORDER)
    return list(dict.fromkeys(order))


def drift_threshold_pct() -> float:
    try:
        value = float(os.environ.get("PLUGBOARD_PRICE_DRIFT_PCT", "") or 20)
    except ValueError:
        _warn_once("drift", "PLUGBOARD_PRICE_DRIFT_PCT is not a number; using 20")
        return 20.0
    return value if value > 0 else 20.0


def drift_action() -> str:
    value = (
        os.environ.get("PLUGBOARD_PRICE_DRIFT_ACTION", "review").strip().lower()
        or "review"
    )
    if value not in ("review", "apply"):
        _warn_once(
            "action",
            "PLUGBOARD_PRICE_DRIFT_ACTION must be review or apply; using review",
        )
        return "review"
    return value


def validate_price(entry: Any) -> Optional[Dict[str, float]]:
    """Normalised {input_price, output_price}, or None when out of bounds."""
    if not isinstance(entry, Mapping):
        return None
    try:
        inp = float(entry.get("input_price"))  # type: ignore[arg-type]  # None -> TypeError
        out = float(entry.get("output_price"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (0 <= inp <= MAX_PRICE and 0 <= out <= MAX_PRICE):
        return None
    if inp > 0 and out > 0 and not (MIN_RATIO <= out / inp <= MAX_RATIO):
        return None
    return {"input_price": round(inp, 6), "output_price": round(out, 6)}


def per_token_to_per_million(value: Any) -> Optional[float]:
    """OpenRouter-style per-token price (often a string) to USD per 1M tokens."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if price < 0:
        return None
    return round(price * 1_000_000, 6)


def _rank(source: Optional[str], order: List[str]) -> int:
    if source == "manual":
        return -1
    if source in order:
        return order.index(source)
    if source == "llm":
        return len(order) + 1
    return len(order)  # older grids carry no source, or a source no longer listed


def same_price(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return all(
        abs(float(a.get(k, 0) or 0) - float(b.get(k, 0) or 0)) <= _EPSILON
        for k in ("input_price", "output_price")
    )


def _is_placeholder(entry: Mapping[str, Any]) -> bool:
    return (
        not entry.get("source")
        and float(entry.get("input_price", 0) or 0) == 0
        and float(entry.get("output_price", 0) or 0) == 0
    )


def change_pct(old: Mapping[str, Any], new: Mapping[str, Any]) -> float:
    """Largest relative change of the input or output price, in percent."""
    pct = 0.0
    for key in ("input_price", "output_price"):
        before = float(old.get(key, 0) or 0)
        after = float(new.get(key, 0) or 0)
        if before > 0:
            pct = max(pct, abs(after - before) / before * 100)
        elif after > 0:
            pct = max(pct, 100.0)
    return round(pct, 2)


@dataclass
class MergeResult:
    models: Dict[str, Dict[str, Any]]
    added: List[str] = field(default_factory=list)
    updated: List[Dict[str, Any]] = field(
        default_factory=list
    )  # {model, source, old, new, pct}
    drifts: List[Dict[str, Any]] = field(default_factory=list)  # held for review
    rejected: List[Dict[str, Any]] = field(default_factory=list)  # out of bounds


def merge_sources(
    existing: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, Mapping[str, Mapping[str, Any]]],
    order: List[str],
    *,
    drift_pct: float = 20.0,
    action: str = "review",
    reviewed: Optional[Dict[str, Dict[str, Any]]] = None,
) -> MergeResult:
    """Return the merged grid for one month; ``existing`` is not modified.

    ``reviewed`` maps a model to its last accepted price: a first price, an
    applied change, or the price before silent updates began. Drift is
    measured against it as well as against the current price, so a series of
    small steps cannot move a price past the threshold without review. It is
    updated in place.
    """
    reviewed = {} if reviewed is None else reviewed
    models: Dict[str, Dict[str, Any]] = {k: dict(v) for k, v in existing.items()}
    result = MergeResult(models=models)
    active = [name for name in order if sources.get(name)]

    # Validate every candidate once.
    valid: Dict[str, Dict[str, Dict[str, float]]] = {}
    for name in active:
        valid[name] = {}
        for model, entry in sources[name].items():
            price = validate_price(entry)
            if price is None:
                result.rejected.append(
                    {
                        "model": model,
                        "source": name,
                        "entry": dict(entry) if isinstance(entry, Mapping) else entry,
                    }
                )
            else:
                valid[name][model] = price

    for model in sorted({m for prices in valid.values() for m in prices}):
        candidates = [
            (name, valid[name][model]) for name in active if model in valid[name]
        ]
        best_name, best = candidates[0]
        current = models.get(model)
        if current is not None and _is_placeholder(current):
            current = None
        if current is None:
            # First price: hold it when a lower-priority source disagrees.
            disagreeing = next(
                (
                    (name, price)
                    for name, price in candidates[1:]
                    if change_pct(price, best) > drift_pct
                ),
                None,
            )
            if disagreeing and action == "review" and best_name not in TRUSTED_SOURCES:
                result.drifts.append(
                    {
                        "model": model,
                        "source": best_name,
                        "old": None,
                        "new": best,
                        "pct": change_pct(disagreeing[1], best),
                        "reason": f"disagrees with {disagreeing[0]}",
                    }
                )
                continue
            if model in models and same_price(models[model], best):
                models[model] = {**models[model], "source": best_name}
            elif model in models:
                old = {k: models[model].get(k) for k in ("input_price", "output_price")}
                models[model] = {**models[model], **best, "source": best_name}
                result.updated.append(
                    {
                        "model": model,
                        "source": best_name,
                        "old": old,
                        "new": best,
                        "pct": 100.0,
                    }
                )
            else:
                models[model] = {**best, "source": best_name}
                result.added.append(model)
            reviewed[model] = dict(best)
            continue
        if _rank(best_name, order) > _rank(current.get("source"), order):
            continue  # a lower-priority source never replaces a higher one
        if same_price(current, best):
            if current.get("source") != best_name:
                models[model] = {**current, "source": best_name}  # silent: same price
            continue
        old = {
            "input_price": current.get("input_price"),
            "output_price": current.get("output_price"),
        }
        baseline = reviewed.get(model) or old
        pct = max(change_pct(current, best), change_pct(baseline, best))
        held = (
            action == "review"
            and best_name not in TRUSTED_SOURCES
            and current.get("source") != "llm"
        )
        if pct > drift_pct and held:
            result.drifts.append(
                {
                    "model": model,
                    "source": best_name,
                    "old": old,
                    "new": best,
                    "pct": pct,
                }
            )
            continue
        models[model] = {**current, **best, "source": best_name}
        result.updated.append(
            {"model": model, "source": best_name, "old": old, "new": best, "pct": pct}
        )
        if pct > drift_pct or model not in reviewed:
            # Accepted beyond the threshold (trusted source or apply mode), or
            # first silent step: this becomes / stays the reference.
            reference: Dict[str, Any] = (
                dict(best) if pct > drift_pct else dict(baseline)
            )
            reviewed[model] = reference
    return result
