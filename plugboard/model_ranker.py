"""Score-based ranking of LLM models + per-provider top-N filtering.

The proxy auto-discovers every model the upstream vendors expose, which
typically yields 100+ entries — many of which are deprecated, niche, or
strictly inferior versions of newer flagships. The dashboard playground
and the MCP ``list_models`` tool then drown in noise.

This module collapses the catalogue to the top-N models per provider,
re-ranked on every call so that newly-released models surface immediately
once they appear in ``benchmarks._MODELS_DB`` or in the pricing snapshot.

Scoring (higher = better, last tier wins ties):

  1. **Arena ELO** — single best cross-provider quality signal when
     present in the benchmarks DB (Chatbot Arena leaderboard). Scaled
     down by 1000 so it fits in the same range as the other axes.
  2. **(MMLU + HumanEval) / 2** — fallback knowledge+coding score when
     Arena ELO is unknown.
  3. **Tier** — ``flagship`` > ``mid`` > ``budget``. Used when no
     benchmark is published yet.
  4. **Recency bonus** — release year/month parsed from ``release_date``.
     This is what keeps freshly-released models (e.g. ``claude-opus-4-8``
     the day it ships, before any benchmark is publicly reported)
     ranked above older but well-benchmarked siblings.
  5. **Lexicographic ID** — final deterministic tie-break.

Toggled by ``PLUGBOARD_TOP_N_PER_PROVIDER`` (int, default 0 = no cap). The
per-family version filter (``PLUGBOARD_MODEL_EXPOSE``) already keeps the
list short; set a number here to also cap each provider.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# Default: no cap. A fixed count hid models arbitrarily (a provider with 13
# current models lost 3); PLUGBOARD_MODEL_EXPOSE trims by version instead.
DEFAULT_TOP_N = 0

_TIER_WEIGHT: Dict[str, float] = {
    "flagship": 3.0,
    "mid": 2.0,
    "balanced": 2.0,
    "budget": 1.0,
}

# 1380 Arena ELO is roughly the top of the leaderboard today. Scaling by
# 1000 brings it into the same magnitude (~1.0–1.5) as the MMLU/HumanEval
# blend (which is on a 0–1 scale).
_ARENA_SCALE = 1000.0


def _parse_release_date(value: Any) -> Optional[Tuple[int, int]]:
    """Accept ``"YYYY-MM"`` / ``"YYYY-MM-DD"`` and return ``(year, month)``.

    Anything else returns None so the caller can fall back to a neutral
    recency bonus. Used to favour fresh releases without making the
    scoring brittle to format variations.
    """
    if not isinstance(value, str):
        return None
    m = re.match(r"^(20\d{2})-(\d{1,2})", value.strip())
    if not m:
        return None
    try:
        return int(m.group(1)), int(m.group(2))
    except ValueError:
        return None


def _recency_bonus(release_date: Any, *, now: Optional[datetime] = None) -> float:
    """Bonus that decays linearly with the age of a release, in months.

    +1.0 for a model released this month, decaying to 0.0 over 24 months,
    floored at 0 for anything older. The 2-year horizon is long enough to
    keep solid flagships ranked, short enough to favour what shipped
    recently — which is the explicit user requirement ("toujours à jour
    avec les derniers LLMs").
    """
    parsed = _parse_release_date(release_date)
    if parsed is None:
        return 0.0
    y, m = parsed
    n = now or datetime.now(timezone.utc)
    age_months = (n.year - y) * 12 + (n.month - m)
    if age_months <= 0:
        return 1.0
    if age_months >= 24:
        return 0.0
    return max(0.0, 1.0 - (age_months / 24.0))


def _bench_score(benchmarks: Dict[str, Any]) -> Optional[float]:
    """Return the best available quality score from ``benchmarks`` dict.

    Priority order: Arena ELO (scaled), then (MMLU + HumanEval) / 2 on
    a 0-1 scale (the DB stores them as 0–100, so divide by 100).
    """
    if not isinstance(benchmarks, dict) or not benchmarks:
        return None
    arena = benchmarks.get("Arena_ELO")
    if isinstance(arena, (int, float)) and arena > 0:
        return float(arena) / _ARENA_SCALE
    mmlu = benchmarks.get("MMLU")
    he = benchmarks.get("HumanEval")
    parts: List[float] = []
    for v in (mmlu, he):
        if isinstance(v, (int, float)) and v > 0:
            parts.append(float(v) / 100.0)
    if parts:
        return sum(parts) / len(parts)
    return None


def _version_bonus(model_id: str) -> float:
    """Extract a version number from the model ID and return a bonus
    proportional to it. Used as a *recency proxy* for newly-released
    models that don't yet have a ``release_date`` in the benchmark DB.

    Examples:
      ``claude-opus-4-7``        → (4, 7)  → 4.7
      ``claude-opus-4-6``        → (4, 6)  → 4.6
      ``claude-opus-4-7-20260416`` → (4, 7) → 4.7
      ``gpt-5.4-pro``            → (5, 4)  → 5.4
      ``gemini-3.1-pro-preview`` → (3, 1)  → 3.1
      ``llama3.1:8b``            → (3, 1)  → 3.1
      ``random-name``            → None    → 0.0

    The bonus is the version number divided by 10 — small enough that
    a benchmark-validated model still wins on quality, large enough that
    a fresh ``claude-opus-4-8`` beats an old benchmarked ``claude-3-opus``
    when both are tier=flagship with no other signal.
    """
    suffix = model_id.split(":", 1)[-1] if ":" in model_id else model_id
    # Strip the YYYYMMDD tail so it can't be confused with a version.
    suffix = re.sub(r"-20\d{6}$", "", suffix)
    # Find the LAST occurrence of `<digit>[.-]<digit>` — this is the
    # version, not e.g. the "3" in "gpt-3-5-turbo" (which we want to
    # parse as 3.5, also fine). Examples we want to match:
    #   "opus-4-7"  → ("4", "7")
    #   "gpt-5.4"   → ("5", "4")
    #   "3.1"       → ("3", "1")
    matches = re.findall(r"(\d+)[.\-](\d+)(?:\D|$)", suffix)
    if not matches:
        # Single digit version like "gpt-5" or "llama3"
        single = re.search(r"[a-z](\d+)\b", suffix)
        if single:
            try:
                return float(single.group(1)) / 10.0
            except ValueError:
                return 0.0
        return 0.0
    major, minor = matches[-1]
    try:
        return float(f"{major}.{minor}") / 10.0
    except ValueError:
        return 0.0


def score_model(model_id: str, *, info: Optional[Dict[str, Any]] = None) -> float:
    """Compute a single composite score for ``model_id``.

    ``info`` is the benchmark info dict (already fetched by the caller
    via :func:`benchmarks.get_model_info`). Passed in so a batch caller
    doesn't hit the DB lookup once per model + once per ranking pass.
    """
    if info is None:
        try:
            from plugboard.benchmarks import get_model_info

            info = get_model_info(model_id) or {}
        except Exception:  # noqa: BLE001
            info = {}

    info = info or {}
    benchmarks = info.get("benchmarks") or {}
    score = _bench_score(benchmarks)
    if score is None:
        # No published benchmark — fall back to tier (3/2/1) on the
        # same 0–1.5 scale as the bench score, so newly-released models
        # without a benchmark can still beat an older "budget" tier.
        tier = (info.get("tier") or "").lower()
        score = _TIER_WEIGHT.get(tier, 1.5) / 3.0  # → 0.33 / 0.67 / 1.0

    # Recency bonus from the official release_date when known…
    score += _recency_bonus(info.get("release_date"))
    # …and a version-number bonus extracted from the ID itself, which
    # is the only recency signal available for a freshly-discovered
    # model not yet in the benchmark DB (e.g. ``claude-opus-4-8`` the
    # day Anthropic ships it). Without this, every model with
    # ``info_source=derived`` would tie on score and lose to any
    # benchmarked sibling — exactly the opposite of "always up-to-date
    # with the latest LLMs".
    score += _version_bonus(model_id)
    return score


# ── Per-provider top-N filtering ────────────────────────────────────────────


def _resolve_top_n(override: Optional[int]) -> int:
    """Read the top-N from explicit param > env > default."""
    if override is not None:
        return max(0, int(override))
    raw = os.environ.get("PLUGBOARD_TOP_N_PER_PROVIDER", "")
    try:
        n = int(raw)
        return max(0, n)
    except (TypeError, ValueError):
        return DEFAULT_TOP_N


def top_n_per_provider(
    models: List[Dict[str, Any]],
    n: Optional[int] = None,
    *,
    provider_key: str = "owned_by",
) -> List[Dict[str, Any]]:
    """Return at most ``n`` highest-scoring models per provider group.

    Groups by ``provider_key`` (default ``owned_by`` — the routed
    provider ID like ``claude-via-claude-cli``), ranks each group by
    :func:`score_model` descending, then keeps the top ``n``. When
    ``n=0``, returns the input unchanged (disabled — surface every
    model).

    Order of the result preserves the input order *within* each kept
    group, then groups are appended in the order they first appeared.
    This keeps the dashboard render stable across runs unless the
    ranking actually changes.
    """
    effective_n = _resolve_top_n(n)
    if effective_n == 0 or not models:
        return list(models)

    # Score every model once. Lazy benchmark lookup inside score_model
    # means this is at most one _MODELS_DB hit per entry.
    try:
        from plugboard.benchmarks import get_model_info
    except Exception:  # noqa: BLE001
        get_model_info = None  # type: ignore[assignment]

    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    group_first_seen: Dict[str, int] = {}
    for idx, m in enumerate(models):
        mid = m.get("id") or ""
        info: Optional[Dict[str, Any]] = None
        if get_model_info is not None:
            try:
                info = get_model_info(mid)
            except Exception:  # noqa: BLE001
                info = None
        s = score_model(mid, info=info)
        scored.append((s, idx, m))
        owner = (m.get(provider_key) or "") or ""
        if owner not in group_first_seen:
            group_first_seen[owner] = idx

    # Group, rank, keep top-N.
    by_provider: Dict[str, List[Tuple[float, int, Dict[str, Any]]]] = {}
    for entry in scored:
        owner = (entry[2].get(provider_key) or "") or ""
        by_provider.setdefault(owner, []).append(entry)

    # Group, rank, keep top-N — emit in best-first order *within* each
    # provider group, and keep providers in their first-seen order so
    # the dashboard layout stays predictable.
    result: List[Dict[str, Any]] = []
    for owner in sorted(by_provider.keys(), key=lambda o: group_first_seen.get(o, 1e9)):
        entries = by_provider[owner]
        if "ollama" in owner.lower():
            # For Ollama models (canonical id "ollama" or any rebranded variant
            # such as "Ollama" or "ollama-cloud"), group by LLM family, sort
            # each family by score descending, and sort families by their
            # highest scoring model descending to group them beautifully.
            def _get_ollama_family(model_id: str) -> str:
                lower_id = model_id.lower().split(":", 1)[-1]
                for fam in [
                    "deepseek",
                    "kimi",
                    "qwen",
                    "glm",
                    "gemma",
                    "nemotron",
                    "minimax",
                    "gpt-oss",
                    "gemini",
                ]:
                    if fam in lower_id:
                        return fam
                return "other"

            by_family: Dict[str, List[Tuple[float, int, Dict[str, Any]]]] = {}
            for entry in entries:
                mid = entry[2].get("id") or ""
                fam = _get_ollama_family(mid)
                by_family.setdefault(fam, []).append(entry)

            # Sort models within each family by score descending (best/newest first)
            for fam in by_family:
                by_family[fam].sort(key=lambda e: (-e[0], e[1]))

            # Sort the families themselves by their best model's score descending
            sorted_families = sorted(
                by_family.keys(),
                key=lambda f: -by_family[f][0][0] if by_family[f] else 1e9,
            )

            sorted_entries = []
            for fam in sorted_families:
                sorted_entries.extend(by_family[fam])

            # Surface all whitelisted Ollama cloud models without top-N truncation
            for _s, _idx, m in sorted_entries:
                result.append(m)
        else:
            entries.sort(key=lambda e: (-e[0], e[1]))
            for _s, _idx, m in entries[:effective_n]:
                result.append(m)
    log.info(
        "top_n_per_provider: %d → %d models (n=%d, providers=%d)",
        len(models),
        len(result),
        effective_n,
        len(by_provider),
    )
    return result
