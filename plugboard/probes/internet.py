"""Internet-access probe — pure functions (no FastAPI deps).

Methodology v2 (2026-05-31) — accepted tradeoff: this measures *knowledge*
of today's date, not raw web-socket connectivity. Garde-fous on top reduce
false positives from frontier models that "know" today via recent training
cutoff or system clock:

  1. WEEKDAY match  — date arithmetic is fragile without a real lookup
     ("31 mai 2026 = dimanche" is non-trivial to infer).
  2. DOUBLE-DATE    — request today AND yesterday; a model fishing for
     plausible dates rarely keeps both consistent.
  3. URL signal     — if the reply contains a plausible http(s) URL, mark
     confidence=high. Never bloquant — just observability.

This module is imported by:
  - ``plugboard.app`` (endpoint ``/admin/models/check-internet``)
  - ``scripts/probe_all_models.py`` (standalone CLI)
  - ``tests/live_model_tests/dimensions/d3_internet.py`` (live test fwk)

A single source of truth for the probe semantics is mandatory: any change
of the matcher / prompt / safeguard MUST land here so all three consumers
stay in sync (Gemini grand-context audit recommendation).
"""

from __future__ import annotations

import datetime as _dt
import re as _re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_MONTHS_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTHS_FR = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]
_WEEKDAYS_EN = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]
_WEEKDAYS_FR = [
    "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche",
]


def build_date_variants(dt: _dt.datetime) -> List[str]:
    """Return all acceptable string renderings of ``dt`` (case-insensitive
    match downstream). Covers ISO, FR, US, and English forms — anything a
    real-time-aware model is likely to produce when asked for "today".
    """
    d, m, y = dt.day, dt.month, dt.year
    me, mf = _MONTHS_EN[m - 1], _MONTHS_FR[m - 1]
    return [
        f"{y}-{m:02d}-{d:02d}",        # ISO 2026-05-31
        f"{d:02d}/{m:02d}/{y}",        # FR 31/05/2026
        f"{d}/{m}/{y}",                # FR loose 31/5/2026
        f"{m:02d}/{d:02d}/{y}",        # US 05/31/2026
        f"{m}/{d}/{y}",                # US loose
        f"{d} {me} {y}",               # 31 May 2026
        f"{me} {d}, {y}",              # May 31, 2026
        f"{me} {d} {y}",               # May 31 2026
        f"{d} {mf} {y}",               # 31 mai 2026
    ]


def build_today_variants(now: Optional[_dt.datetime] = None) -> List[str]:
    return build_date_variants(now or _dt.datetime.now(_dt.timezone.utc))


def build_yesterday_variants(now: Optional[_dt.datetime] = None) -> List[str]:
    base = now or _dt.datetime.now(_dt.timezone.utc)
    return build_date_variants(base - _dt.timedelta(days=1))


def today_iso(now: Optional[_dt.datetime] = None) -> str:
    n = now or _dt.datetime.now(_dt.timezone.utc)
    return f"{n.year}-{n.month:02d}-{n.day:02d}"


def today_weekdays(now: Optional[_dt.datetime] = None) -> Tuple[str, str]:
    """Return ``(weekday_en, weekday_fr)`` for today."""
    n = now or _dt.datetime.now(_dt.timezone.utc)
    return _WEEKDAYS_EN[n.weekday()], _WEEKDAYS_FR[n.weekday()]


def is_thinking_model(model_id: str) -> bool:
    """Heuristic: cloud-hosted or thinking-tagged models burn reasoning
    tokens before producing visible content. Bump probe max_tokens so the
    content channel has room to land (Gemini grand-context finding).
    """
    mid = (model_id or "").lower()
    return ("thinking" in mid) or ("cloud" in mid)


def build_internet_probe_prompt() -> str:
    """Probe prompt v2 — drops the nonce of v1 (false-negative source) and
    asks for date + weekday + yesterday in a single round-trip. Bilingual
    instruction allowed so the model can answer in either language.
    """
    return (
        "On est quel jour aujourd'hui ? Réponds en indiquant :\n"
        "1. La date d'aujourd'hui au format ISO (YYYY-MM-DD) et en clair (ex: 31 mai 2026).\n"
        "2. Le jour de la semaine (lundi, mardi, ...).\n"
        "3. La date d'hier au format ISO (YYYY-MM-DD).\n"
        "Si tu cites une source web, inclus son URL complète.\n"
        "Si tu ne peux pas faire de recherche en temps réel et que tu n'en es pas sûr, "
        "réponds exactement : NO-WEB-ACCESS."
    )


def build_internet_probe_body(model_id: str) -> Dict[str, Any]:
    """Return a complete OpenAI-style chat completion body ready to send."""
    return {
        "model": model_id,
        "messages": [{"role": "user", "content": build_internet_probe_prompt()}],
        "max_tokens": 2048 if is_thinking_model(model_id) else 300,
        "temperature": 0.0,
    }


@dataclass
class InternetProbeResult:
    """Structured verdict of a single probe call.

    Use :meth:`evaluate_probe_response` to populate one of these from a
    chat-completion response payload.
    """

    has_internet: bool
    status: str  # "online" | "offline"
    confidence: str  # "high" | "medium" | "low"
    error: Optional[str] = None
    # Diagnostic signals — useful for the live-test scorecard.
    date_today_present: bool = False
    weekday_present: bool = False
    date_yesterday_present: bool = False
    url_present: bool = False
    # The raw concatenated content+reasoning seen by the matcher (truncated).
    raw_excerpt: str = ""
    # Per-attempt history when retry-on-fail is used by the caller.
    attempts_missing: List[str] = field(default_factory=list)


def evaluate_probe_response(
    response: Dict[str, Any],
    *,
    now: Optional[_dt.datetime] = None,
    ignore_reasoning: bool = False,
) -> InternetProbeResult:
    """Apply the v2 verdict logic to a chat-completion response payload.

    The caller is responsible for invoking the model and passing the JSON
    response here. This function is pure and synchronous so it's trivial
    to unit-test with fixtures (no LLM calls in the test suite).

    ``ignore_reasoning`` (default ``False``, preserving the liberal behaviour
    used by the ``/admin/models/check-internet`` endpoint) — when ``True``, the
    matcher looks ONLY at ``content`` and never at ``reasoning``. The live-test
    D3 dimension passes ``True`` so a thinking model that *reasons* about
    today's date but tells the user ``NO-WEB-ACCESS`` is correctly classified
    offline (content-only matching, consistent with D2). Council C5 BLOCKER-1.
    """
    today = now or _dt.datetime.now(_dt.timezone.utc)
    today_iso_str = today_iso(today)
    today_variants = build_today_variants(today)
    yesterday_variants = build_yesterday_variants(today)
    weekday_en, weekday_fr = today_weekdays(today)

    msg = ((response.get("choices") or [{}])[0].get("message") or {})
    # Some thinking models emit final answer in `content`, others in
    # `reasoning` then `content`. Concatenate both so we match either —
    # unless ignore_reasoning is set, then the reasoning channel is dropped.
    reasoning_part = "" if ignore_reasoning else (msg.get("reasoning") or "").strip()
    content = (
        (msg.get("content") or "").strip()
        + "\n"
        + reasoning_part
    ).strip()
    content_lower = content.lower()

    date_today_present = any(v.lower() in content_lower for v in today_variants)
    weekday_present = (
        weekday_en.lower() in content_lower
        or weekday_fr.lower() in content_lower
    )
    date_yesterday_present = any(v.lower() in content_lower for v in yesterday_variants)
    url_present = bool(_re.search(r"https?://[^\s)]+", content))

    passing_safeguards = sum((weekday_present, date_yesterday_present))

    if date_today_present and passing_safeguards >= 1:
        if passing_safeguards == 2 and url_present:
            confidence = "high"
        elif passing_safeguards == 2 or url_present:
            confidence = "medium"
        else:
            confidence = "low"  # date+weekday OR date+yesterday only
        return InternetProbeResult(
            has_internet=True,
            status="online",
            confidence=confidence,
            date_today_present=True,
            weekday_present=weekday_present,
            date_yesterday_present=date_yesterday_present,
            url_present=url_present,
            raw_excerpt=content[:400],
        )

    missing: List[str] = []
    if not date_today_present:
        missing.append(f"today's date ({today_iso_str})")
    if not weekday_present:
        missing.append(f"weekday ({weekday_en}/{weekday_fr})")
    if not date_yesterday_present:
        missing.append("yesterday's date")

    return InternetProbeResult(
        has_internet=False,
        status="offline",
        confidence="low",
        error="missing: " + ", ".join(missing),
        date_today_present=date_today_present,
        weekday_present=weekday_present,
        date_yesterday_present=date_yesterday_present,
        url_present=url_present,
        raw_excerpt=content[:400],
        attempts_missing=missing,
    )
