"""Probe library — pure functions reusable by the API endpoint, the boot probe,
the standalone CLI script, and the live test framework.

Refactor 2026-05-31 (TASK P0-02): extracted from ``plugboard/app.py``
``check_model_internet_access`` so callers can share a single source of
truth for the probe methodology v2 (date + weekday + yesterday match +
retry-on-fail + thinking-aware max_tokens).
"""

from plugboard.probes.internet import (
    InternetProbeResult,
    build_internet_probe_body,
    build_internet_probe_prompt,
    build_today_variants,
    build_yesterday_variants,
    evaluate_probe_response,
    is_thinking_model,
    today_iso,
    today_weekdays,
)

__all__ = [
    "InternetProbeResult",
    "build_internet_probe_body",
    "build_internet_probe_prompt",
    "build_today_variants",
    "build_yesterday_variants",
    "evaluate_probe_response",
    "is_thinking_model",
    "today_iso",
    "today_weekdays",
]
