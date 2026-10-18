"""Trends aggregator — detect pass_rate regressions between consecutive runs.

Pure ``detect_regressions(history)`` — no network, no filesystem.
Council C10 (developer + codex-coder).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

_DEFAULT_REGRESSION_THRESHOLD = 0.05  # 5 percentage-point drop


def _check(
    dim_key: str, prev: Optional[float], curr: Optional[float], threshold: float
) -> Optional[Dict[str, Any]]:
    # A None pass_rate (skipped dim) is not numerically comparable.
    if prev is None or curr is None:
        return None
    delta = curr - prev
    if delta >= -threshold:
        return None
    return {
        "dim_key": dim_key,
        "prev_pass_rate": prev,
        "curr_pass_rate": curr,
        "delta": delta,
        "severity": "MAJOR",
    }


def detect_regressions(
    history: List[Dict[str, Any]],
    regression_threshold: float = _DEFAULT_REGRESSION_THRESHOLD,
) -> Dict[str, Any]:
    """Compare the last two run summaries (oldest→newest) and flag dimensions
    whose pass_rate dropped by more than ``regression_threshold``."""
    if len(history) < 2:
        return {
            "has_regressions": False,
            "regressions": [],
            "prev_run_id": None,
            "curr_run_id": history[-1].get("run_id") if history else None,
            "compared_dims": 0,
            "note": "Need at least 2 runs to detect regressions.",
        }

    prev_run, curr_run = history[-2], history[-1]
    prev_aggs = prev_run.get("dim_aggregates", {})
    curr_aggs = curr_run.get("dim_aggregates", {})
    common = sorted(set(prev_aggs) & set(curr_aggs))

    regressions = []
    for dim_key in common:
        reg = _check(
            dim_key,
            prev_aggs[dim_key].get("pass_rate"),
            curr_aggs[dim_key].get("pass_rate"),
            regression_threshold,
        )
        if reg is not None:
            regressions.append(reg)

    return {
        "has_regressions": bool(regressions),
        "regressions": regressions,
        "prev_run_id": prev_run.get("run_id"),
        "curr_run_id": curr_run.get("run_id"),
        "compared_dims": len(common),
    }
