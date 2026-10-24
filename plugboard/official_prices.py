"""Official vendor prices, curated by hand and dated (USD per 1M tokens).

The pricing refresh (``pricing_scraper.update_current_pricing``, run by the
in-app ``PricingScheduler``) writes these prices into the month's grid as the
``official`` source. Nothing is fetched: the table ships with Plugboard, so it
applies even when every network source fails.

Granularity is monthly: a grid month takes the price in force on the first day
of that month (``prices_for_month``). A vendor change dated mid-month applies
from the next month's grid.

To update the table when a vendor changes its prices, see
docs/runbooks/pricing-scheduler.md#official-price-table.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

MAX_AGE_ENV = "PLUGBOARD_OFFICIAL_PRICES_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 35
_warned: set = set()

CHECKED = "2026-10-24"
_ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"
_GOOGLE = "https://ai.google.dev/gemini-api/docs/pricing"
_OPENAI = "https://developers.openai.com/api/docs/pricing"
_TYPESAFE = "https://docs.typesafe.ai/models#current-models"
_LAYA = "https://github.com/NandhaKishorM/laya"

_ARGON = ("gemini-4-argon-low", "gemini-4-argon-medium", "gemini-4-argon-high")
_FLASH = ("gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high")


def _entry(
    model: str, inp: float, out: float, url: str, **extra: str
) -> Dict[str, Any]:
    return {
        "model": model,
        "input_price": inp,
        "output_price": out,
        "source_url": url,
        "checked": CHECKED,
        **extra,
    }


# No official price, deliberately absent: gpt-oss-120b-medium (not sold on the
# OpenAI API; a host's price is not the publisher's), meta-llama-3.1-405b-instruct.
_TABLE = (
    _entry("jev", 0.042, 0.0, _TYPESAFE, note="input tokens only; output free"),
    _entry("laya", 0.0, 0.0, _LAYA, note="runs locally; free by user decision"),
    _entry(
        "laya-multilingual", 0.0, 0.0, _LAYA, note="runs locally; free by user decision"
    ),
    _entry("claude-fable-5-1", 10.0, 50.0, _ANTHROPIC),
    _entry("claude-opus-5-5", 4.0, 20.0, _ANTHROPIC),
    _entry("claude-sonnet-5-5", 2.0, 10.0, _ANTHROPIC),
    _entry(
        "claude-haiku-5-5",
        0.10,
        0.50,
        _ANTHROPIC,
        note="prompts up to 100k tokens; above, 0.50/2.50 for the whole request (no tier support)",
    ),
    # agy effort levels are billed as their base model.
    _entry("claude-opus-5-5-medium", 4.0, 20.0, _ANTHROPIC, note="agy effort level"),
    _entry("claude-sonnet-5-5-medium", 2.0, 10.0, _ANTHROPIC, note="agy effort level"),
    *(_entry(m, 2.0, 10.0, _GOOGLE, effective_until="2026-11-30") for m in _ARGON),
    *(_entry(m, 4.0, 20.0, _GOOGLE, effective_from="2026-12-01") for m in _ARGON),
    *(_entry(m, 0.75, 3.75, _GOOGLE, effective_until="2026-12-31") for m in _FLASH),
    *(_entry(m, 1.50, 7.50, _GOOGLE, effective_from="2027-01-01") for m in _FLASH),
    _entry("gpt-4o-mini", 0.15, 0.60, _OPENAI),
)


def entries() -> List[Dict[str, Any]]:
    """Every entry of the table, including those not or no longer in force."""
    return [dict(e) for e in _TABLE]


def prices_on(day: date) -> Dict[str, Dict[str, Any]]:
    """``{model: {input_price, output_price, source_url}}`` in force on ``day``."""
    iso = day.isoformat()
    prices: Dict[str, Dict[str, Any]] = {}
    for e in _TABLE:
        if e.get("effective_from", iso) <= iso <= e.get("effective_until", iso):
            prices[e["model"]] = {
                "input_price": e["input_price"],
                "output_price": e["output_price"],
                "source_url": e["source_url"],
            }
    return prices


def prices_for_month(month: str) -> Dict[str, Dict[str, Any]]:
    """Prices for the grid ``YYYY_MM``: those in force on its first day."""
    year, mon = month.split("_")
    return prices_on(date(int(year), int(mon), 1))


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _max_age_days() -> int:
    raw = os.environ.get(MAX_AGE_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_AGE_DAYS
    except ValueError:
        value = 0
    if value <= 0:
        if raw not in _warned:
            _warned.add(raw)
            log.warning(
                "%s must be a positive integer; using %d",
                MAX_AGE_ENV,
                DEFAULT_MAX_AGE_DAYS,
            )
        return DEFAULT_MAX_AGE_DAYS
    return value


def status(today: Optional[date] = None) -> Dict[str, Any]:
    """``{checked, stale, max_age_days}``: stale once the newest check is too old."""
    checked = max(date.fromisoformat(e["checked"]) for e in _TABLE)
    max_age = _max_age_days()
    age = ((today or _today()) - checked).days
    return {
        "checked": checked.isoformat(),
        "stale": age > max_age,
        "max_age_days": max_age,
    }
