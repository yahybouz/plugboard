"""
Pricing grids and cost calculation.

Pricing files: data/pricing/YYYY_MM.json
Schema:
{
  "month": "2026_03",
  "models": {
    "gpt-4o": {"input_price": 2.50, "output_price": 10.00},
    ...
  },
  "note": "Prices in USD per 1M tokens"
}

Cost formula: (prompt_tokens/1_000_000 × input_price) + (completion_tokens/1_000_000 × output_price)
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from plugboard.storage import atomic_write_json, data_dir

_DATA_DIR = data_dir()
_PRICING_DIR = _DATA_DIR / "pricing"

log = logging.getLogger(__name__)

try:
    from filelock import FileLock as _FileLock

    def _lock(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # One reentrant instance per path, so a read-modify-write held under
        # pricing_lock() can call save_pricing() without deadlocking.
        return _FileLock(str(path) + ".lock", is_singleton=True)
except ImportError:
    _thread_locks: Dict[str, "threading.RLock"] = {}
    _thread_locks_guard = threading.Lock()

    def _lock(path: Path):
        with _thread_locks_guard:
            return _thread_locks.setdefault(str(path), threading.RLock())


def pricing_lock(month: str):
    """Hold the lock of one month's grid across a read-modify-write."""
    return _lock(_PRICING_DIR / f"{month}.json")


def current_pricing_month() -> str:
    """Return 'YYYY_MM' for today's UTC date."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}_{now.month:02d}"


class PricingFileError(RuntimeError):
    """A grid file exists but cannot be read; writing over it would lose it."""


def load_pricing(month: str, strict: bool = False) -> Dict[str, Any]:
    """
    Load data/pricing/{month}.json.
    Returns empty dict if file does not exist. An unreadable file also reads
    as empty unless ``strict``: read-modify-write paths pass strict=True so
    they refuse to replace a damaged grid with a nearly empty one.
    """
    path = _PRICING_DIR / f"{month}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        return data
    except Exception as exc:
        log.error("Failed to read pricing/%s.json: %s", month, exc)
        if strict:
            raise PricingFileError(
                f"pricing/{month}.json cannot be read ({exc}); fix or restore it before editing prices"
            ) from exc
        return {}


def save_pricing(month: str, pricing: Dict[str, Any]) -> None:
    """
    Write data/pricing/{month}.json.
    Only writes the new month file — never touches previous months.
    """
    _PRICING_DIR.mkdir(parents=True, exist_ok=True)
    path = _PRICING_DIR / f"{month}.json"
    pricing["month"] = month
    with _lock(path):
        atomic_write_json(path, pricing)
    log.info(
        "Saved pricing for month %s (%d models)", month, len(pricing.get("models", {}))
    )


def _normalize(s: str) -> str:
    """Normalize model name for fuzzy matching: dots→dashes, strip dates."""
    import re

    s = s.replace(".", "-")
    # Strip trailing date suffixes like -20251001
    s = re.sub(r"-\d{8}$", "", s)
    return s


def _candidate_keys(real_model: str) -> list:
    """Enumerate plausible pricing keys for a routed model ID.

    PRICE-1: the real model string reaching this function can be any of:

      ollama:gemma4:latest               # 2 colons — Ollama name:tag
      ollama:deepseek-v3.1:671b-cloud
      openai/gpt-4o                      # GitHub Models namespaced
      meta/llama-3.3-70b-instruct
      openclaw/anthropic--claude-haiku-4-5
      claude-3-5-haiku-latest
      claude-haiku-4-5

    We need pricing entries to match regardless of which provider's
    catalogue the call came through. This helper returns every
    reasonable key variation, in priority order.
    """
    candidates = [real_model]

    # Ollama `name:tag` → try the bare name (without tag).
    if ":" in real_model:
        head, tail = real_model.split(":", 1)
        # Keep both the full `name:tag` AND just the name.
        candidates.append(head)
        # Some known prices have `name:tag` form (e.g. `llama3:8b`), so keep raw.

    # GitHub Models: `vendor/model-id` → try `model-id` alone.
    if "/" in real_model:
        candidates.append(real_model.split("/", 1)[-1])

    # OpenClaw: `vendor--model-id` → try the `model-id`.
    if "--" in real_model:
        candidates.append(real_model.split("--", 1)[-1])

    # Strip "-latest" suffix (aliases share pricing with the dated entry).
    for c in list(candidates):
        if c.endswith("-latest"):
            candidates.append(c[: -len("-latest")])
        elif c.endswith(":latest"):
            candidates.append(c[: -len(":latest")])

    # Strip trailing ISO date suffixes "-YYYYMMDD" so
    # `claude-opus-4-1-20250805` also matches `claude-opus-4-1`.
    import re as _re

    _DATE_RE = _re.compile(r"-20\d{6}$")
    for c in list(candidates):
        stripped = _DATE_RE.sub("", c)
        if stripped != c:
            candidates.append(stripped)

    # De-dup while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


# -- Price resolution ------------------------------------------------------------
#
# A grid has two kinds of entries:
#   models               vendor prices, keyed by model id;
#   models_by_provider   {space: {model: price}} for resellers whose price
#                        differs from the vendor's (OpenRouter, OpenCode...).
# A space is a models.dev provider id or a Plugboard provider id. Lookups in a
# space never fall through to another space by suffix: the provider's space is
# searched first, then the vendor prices.


@dataclass
class PriceMatch:
    entry: Dict[str, Any]
    key: str
    space: str  # "models", a provider space, or "builtin"
    month: str = ""

    @property
    def source(self) -> str:
        return str(self.entry.get("source") or "")

    @property
    def price_key(self) -> str:
        return self.key if self.space == "models" else f"{self.space}|{self.key}"


class _KeyIndex:
    """Exact and normalised lookups over one map of prices."""

    __slots__ = ("keys", "norm")

    def __init__(self, models: Dict[str, Any]):
        self.keys = models
        self.norm: Dict[str, str] = {}
        for key in models:
            nk = _normalize(key)
            current = self.norm.get(nk)
            if current is None or len(key) > len(current):
                self.norm[nk] = key

    def lookup(self, candidates: List[str]) -> Optional[str]:
        # 1. Exact match on any candidate form.
        for c in candidates:
            if c in self.keys:
                return c
        # 2. Longest pricing key that a candidate ends with
        #    (`openclaw/anthropic--claude-sonnet-4-6` -> `claude-sonnet-4-6`).
        best: Optional[str] = None
        for c in candidates:
            for i in range(1, len(c)):
                suffix = c[i:]
                if best is not None and len(suffix) <= len(best):
                    break
                if suffix in self.keys:
                    best = suffix
                    break
        if best is not None:
            return best
        # 3. Normalised forms (dots -> dashes, dates stripped).
        for c in candidates:
            norm = _normalize(c)
            if norm in self.keys:
                return norm
            for i in range(len(norm)):
                original = self.norm.get(norm[i:])
                if original is not None and (best is None or len(original) > len(best)):
                    best = original
        return best


_INDEX_CACHE: "OrderedDict[int, Tuple[Dict[str, Any], _KeyIndex]]" = OrderedDict()
_INDEX_CACHE_SIZE = 64
_index_guard = threading.Lock()


def _index(models: Dict[str, Any]) -> _KeyIndex:
    """Index of a price map, reused while the same dict object is passed."""
    with _index_guard:
        hit = _INDEX_CACHE.get(id(models))
        if hit is not None and hit[0] is models and len(hit[1].keys) == len(models):
            _INDEX_CACHE.move_to_end(id(models))
            return hit[1]
    index = _KeyIndex(models)
    with _index_guard:
        _INDEX_CACHE[id(models)] = (models, index)
        while len(_INDEX_CACHE) > _INDEX_CACHE_SIZE:
            _INDEX_CACHE.popitem(last=False)
    return index


def provider_spaces(provider: Any) -> List[str]:
    """Price spaces to search for a provider, most specific first.

    A provider created from a catalog preset ``catalog-openrouter`` uses the
    ``openrouter`` space; its own id comes last so an admin can price one
    provider record differently.
    """
    if not provider:
        return []
    if isinstance(provider, str):
        return [provider]
    spaces: List[str] = []
    preset = str(provider.get("preset_key") or "")
    if preset:
        spaces.append(preset)
        if preset.startswith(CATALOG_PREFIX):
            spaces.append(preset[len(CATALOG_PREFIX):])
    if provider.get("id"):
        spaces.append(str(provider["id"]))
    host_space = _space_for_host(str(provider.get("base_url") or ""))
    if host_space:
        spaces.append(host_space)  # a reseller added by hand: after its own prices
    return list(dict.fromkeys(spaces))


CATALOG_PREFIX = "catalog-"


def _space_for_host(base_url: str) -> str:
    """models.dev id of a known reseller host (openrouter.ai -> openrouter)."""
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    if not host:
        return ""
    try:
        from plugboard.models_dev import KNOWN_BASE_URLS
    except ImportError:  # pragma: no cover
        return ""
    for space, url in KNOWN_BASE_URLS.items():
        if (urlsplit(url).hostname or "").lower() == host:
            return space
    return ""


def price_space(provider: Dict[str, Any]) -> str:
    """The space where a price for this provider is written.

    A catalog preset writes under its models.dev id, which every provider of
    that reseller reads; other providers write under their own id, which they
    read before any shared reseller space.
    """
    preset = str(provider.get("preset_key") or "")
    if preset.startswith(CATALOG_PREFIX):
        return preset[len(CATALOG_PREFIX):]
    return str(provider.get("id") or "")


def _lookup(table: Dict[str, Any], candidates: List[str]) -> Optional[str]:
    key = _index(table).lookup(candidates)
    if key is not None and key not in table:
        # The dict was changed in place since it was indexed: rebuild.
        with _index_guard:
            _INDEX_CACHE.pop(id(table), None)
        key = _index(table).lookup(candidates)
    return key


def match_in_grid(
    pricing: Dict[str, Any], spaces: List[str], candidates: List[str]
) -> Optional[PriceMatch]:
    """First match for ``candidates`` in the given price spaces, then in vendor prices."""
    by_provider = pricing.get("models_by_provider") or {}
    for space in spaces:
        sub = by_provider.get(space)
        if isinstance(sub, dict) and sub:
            key = _lookup(sub, candidates)
            if key is not None:
                return PriceMatch(sub[key], key, space)
    models = pricing.get("models") or {}
    if models:
        key = _lookup(models, candidates)
        if key is not None:
            return PriceMatch(models[key], key, "models")
    return None


def _builtin_match(candidates: List[str]) -> Optional[PriceMatch]:
    # `_KNOWN_PRICES` ships with plugboard and covers what the scraper would
    # add on its next run. Hitting this path means the grid is stale; the
    # request is still billed and the next update persists the entry.
    try:
        from plugboard.pricing_scraper import _KNOWN_PRICES, _match_known_price

        for c in candidates:
            if c in _KNOWN_PRICES:
                return PriceMatch(dict(_KNOWN_PRICES[c]), c, "builtin")
        for c in candidates:
            match = _match_known_price(c)
            if match is not None:
                return PriceMatch(dict(match), c, "builtin")
    except Exception as exc:  # pragma: no cover — best-effort fallback
        log.debug("pricing fallback error for %s: %s", candidates[:1], exc)
    return None


def resolve_model_price(
    real_model: str, pricing: Dict[str, Any], provider: Any = None
) -> Optional[Dict[str, Any]]:
    """Find a pricing entry for real_model in one grid. None if unmatched.

    With ``provider`` (a provider record or id), its price space is searched
    first, then the vendor prices; see ``provider_spaces``. Within a map the
    first hit wins: exact key of any candidate form, longest key that a
    candidate ends with, then normalised forms. The built-in table is the
    last resort.
    """
    candidates = _candidate_keys(real_model)
    match = match_in_grid(pricing, provider_spaces(provider), candidates)
    if match is None:
        match = _builtin_match(candidates)
    return match.entry if match else None


# Grids read by the billing path, keyed by file path and invalidated by mtime.
_GRID_CACHE: Dict[str, Dict[str, Any]] = {}
_MAX_MISSES = 10_000
_grid_guard = threading.Lock()


def _cached_grid(month: str) -> Dict[str, Any]:
    """Cache record ``{sig, grid, miss}`` of one month's grid, for billing.

    ``grid`` is shared and read-only: admin endpoints and the scraper use
    load_pricing(), which always reads the file. ``miss`` remembers lookups
    that found nothing until the file changes.
    """
    path = _PRICING_DIR / f"{month}.json"
    try:
        st = path.stat()
        sig: Tuple[int, int] = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = (0, -1)
    key = str(path)
    with _grid_guard:
        record = _GRID_CACHE.get(key)
        if record is not None and record["sig"] == sig:
            return record
    grid = load_pricing(month) if sig[1] >= 0 else {}
    record = {"sig": sig, "grid": grid, "miss": set()}
    with _grid_guard:
        _GRID_CACHE[key] = record
    return record


def _walk_months(month: str, max_months_back: int) -> List[str]:
    try:
        y_str, m_str = month.split("_")
        y, m = int(y_str), int(m_str)
    except (ValueError, AttributeError):
        return [month]
    months = []
    for _ in range(max(1, max_months_back)):
        months.append(f"{y:04d}_{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return months


def _is_ollama(provider: Any) -> bool:
    if isinstance(provider, str):
        return provider == "ollama"
    if not isinstance(provider, dict):
        return False
    base = str(provider.get("base_url") or "").lower()
    ident = str(provider.get("id") or provider.get("name") or "").lower()
    return ident.startswith("ollama") or ":11434" in base


def _is_ollama_cloud(model: str) -> bool:
    bare = model.split(":", 1)[1] if model.startswith("ollama:") else model
    return bare.endswith(":cloud") or bare.endswith("-cloud")


def _ollama_price(
    real_model: str, spaces: List[str], month: str, max_months_back: int
) -> Optional[PriceMatch]:
    """Price of a model served by Ollama.

    A cloud model takes the price Ollama publishes (the ``ollama`` space, filled
    from ollama.com/pricing by the pricing refresh, or set by an admin): its own
    id first (``gpt-oss:120b-cloud``), then its family (``gemma4:cloud`` for
    ``gemma4:31b-cloud``). A local model runs on the gateway host: 0. A cloud
    model Ollama does not list returns None and goes through the usual lookup.
    """
    bare = real_model.split(":", 1)[1] if real_model.startswith("ollama:") else real_model
    keys = [bare]
    if ":" in bare:
        keys.append(bare.split(":", 1)[0] + ":cloud")
    for m in _walk_months(month, max_months_back):
        by_provider = _cached_grid(m)["grid"].get("models_by_provider") or {}
        for space in spaces:
            sub = by_provider.get(space)
            if not isinstance(sub, dict):
                continue
            for key in keys:
                if key in sub:
                    return PriceMatch(sub[key], key, space, m)
    if not _is_ollama_cloud(bare):
        local = {"input_price": 0.0, "output_price": 0.0, "source": "local"}
        return PriceMatch(local, bare, "ollama", month)
    return None


def resolve_price(
    real_model: str,
    month: Optional[str] = None,
    provider: Any = None,
    max_months_back: int = 12,
) -> Optional[PriceMatch]:
    """Price of ``real_model`` served by ``provider``, with the key that matched.

    Walks back up to ``max_months_back`` monthly grids when the current one
    cannot price the model (a grid left in a sentinel state must not bill a
    new model at 0), then tries the built-in table. Returns None only when
    every lookup fails.
    """
    month = month or current_pricing_month()
    spaces = provider_spaces(provider)
    candidates = _candidate_keys(real_model)
    if _is_ollama(provider):
        ollama_match = _ollama_price(real_model, spaces, month, max_months_back)
        if ollama_match is not None:
            return ollama_match
    miss_key = (tuple(spaces), real_model)
    for m in _walk_months(month, max_months_back):
        record = _cached_grid(m)
        if miss_key in record["miss"]:
            continue
        match = match_in_grid(record["grid"], spaces, candidates)
        if match is not None:
            match.month = m
            return match
        with _grid_guard:
            if len(record["miss"]) >= _MAX_MISSES:
                record["miss"].clear()  # model names come from clients: keep it bounded
            record["miss"].add(miss_key)
    builtin = _builtin_match(candidates)
    if builtin is not None:
        return builtin
    # Last resort: the closest version of the same family, flagged for review.
    for m in _walk_months(month, max_months_back):
        grid = _cached_grid(m)["grid"]
        if not grid:
            continue
        estimate = nearest_price(real_model, grid, spaces)
        if estimate is not None:
            estimate.month = m
            _notify_estimate(estimate, month)
            return estimate
    return None


# Effort / tier suffixes some CLIs append to a model id (agy: `-medium`).
_EFFORT_SUFFIX_RE = re.compile(r"-(?:minimal|low|medium|high|xhigh|max)$")
_VERSION_RE = re.compile(r"\d+(?:[.-]\d+)*")
_estimate_notified: set = set()


def _family_version(name: str) -> Tuple[str, Tuple[int, ...]]:
    """``claude-opus-5-5`` -> (``claude-opus``, (5, 5)); dates are ignored."""
    name = _normalize(name.lower())
    name = _EFFORT_SUFFIX_RE.sub("", name)
    numbers = [int(n) for n in re.findall(r"\d+", name) if len(n) < 8]
    family = re.sub(r"-+", "-", _VERSION_RE.sub("", name)).strip("-")
    return family, tuple(numbers)


def _version_value(version: Tuple[int, ...]) -> float:
    major = version[0] if version else 0
    minor = version[1] if len(version) > 1 else 0
    return major + minor / 100.0


def _bare_model(real_model: str) -> str:
    """Model id without the ``provider:`` prefix or a ``vendor/`` namespace."""
    bare = real_model.split(":", 1)[1] if ":" in real_model else real_model
    return bare.rsplit("/", 1)[-1]


def nearest_price(
    real_model: str, pricing: Dict[str, Any], spaces: List[str]
) -> Optional[PriceMatch]:
    """Price of the closest version of the same family, or None.

    ``claude-opus-5-6`` with no price of its own takes ``claude-opus-5-5``'s.
    Only models of the same family are considered; on a tie the newer version
    wins. The returned entry is marked ``source: estimate``.
    """
    family, version = _family_version(_bare_model(real_model))
    if not family or not version:
        return None
    target = _version_value(version)
    tables: List[Tuple[str, Dict[str, Any]]] = []
    by_provider = pricing.get("models_by_provider") or {}
    for space in spaces:
        sub = by_provider.get(space)
        if isinstance(sub, dict):
            tables.append((space, sub))
    tables.append(("models", pricing.get("models") or {}))
    best: Optional[Tuple[float, float, str, str, Dict[str, Any]]] = None
    for space, table in tables:
        for key, entry in table.items():
            if not isinstance(entry, dict) or entry.get("input_price") is None:
                continue
            if entry.get("source") == "estimate":
                continue  # never chain estimates
            fam, ver = _family_version(_bare_model(key))
            if fam != family or not ver:
                continue
            value = _version_value(ver)
            rank = (abs(value - target), -value)
            if best is None or rank < (best[0], best[1]):
                best = (rank[0], rank[1], space, key, entry)
        if best is not None:
            break  # a provider's own space wins over vendor prices
    if best is None:
        return None
    _, _, space, key, entry = best
    estimate = {
        "input_price": entry.get("input_price"),
        "output_price": entry.get("output_price"),
        "source": "estimate",
        "estimated_from": key,
    }
    return PriceMatch(estimate, _bare_model(real_model), space)


def _notify_estimate(match: PriceMatch, month: str) -> None:
    """Queue an estimated price for review and journal it, once per model."""
    marker = (match.space, match.key, month)
    if marker in _estimate_notified:
        return
    _estimate_notified.add(marker)
    source = str(match.entry.get("estimated_from", ""))
    log.warning(
        "No price for %s: billed at the price of %s (estimate, waiting for review)",
        match.key,
        source,
    )
    try:
        from plugboard import change_events, pricing_suggestions

        space = "" if match.space == "models" else match.space
        price = {"input_price": match.entry["input_price"], "output_price": match.entry["output_price"]}
        pricing_suggestions.record_estimates(
            month, [{"model": match.key, "space": space, "new": price, "source": source}]
        )
        batch = change_events.ChangeBatch()
        batch.add("price.estimated", {"model": match.key, "space": space, "from": source, **price})
        batch.flush_in_background()
    except Exception as exc:  # noqa: BLE001 - billing never fails on a notification
        log.warning("estimate notification for %s failed: %s", match.key, exc)


def resolve_model_price_with_fallback(
    real_model: str,
    month: Optional[str] = None,
    max_months_back: int = 12,
    provider: Any = None,
) -> Optional[Dict[str, Any]]:
    """Entry-only form of :func:`resolve_price`."""
    match = resolve_price(real_model, month, provider, max_months_back)
    return match.entry if match else None


def calculate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    price_entry: Optional[Dict[str, Any]],
) -> float:
    """
    cost = (prompt_tokens / 1_000_000 * input_price)
         + (completion_tokens / 1_000_000 * output_price)
    Returns 0.0 if price_entry is None.
    """
    if not price_entry:
        return 0.0
    input_price = price_entry.get("input_price", 0.0)
    output_price = price_entry.get("output_price", 0.0)
    cost = (prompt_tokens / 1_000_000 * input_price) + (
        completion_tokens / 1_000_000 * output_price
    )
    return round(cost, 8)
