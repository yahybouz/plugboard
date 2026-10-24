"""Tests for the pricing scraper inheritance logic (BUG-2).

The cron used to end up with a single-model grid whenever the current
month's JSON was seeded with a test fixture (or written empty). The
fix: inherit from the previous month as a fill-only baseline so the
catalog always stays complete.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugboard import billing as _billing
from plugboard import pricing_scraper as _scraper
from plugboard.pricing_scraper import _previous_month, update_current_pricing


@pytest.fixture(autouse=True)
def _mute_litellm_fetcher(monkeypatch):
    """Inheritance tests assert exact counts ; LiteLLM's live fetch
    (~2000 models) must not pollute them. Short-circuit it to {}."""

    async def _empty():
        return {}

    monkeypatch.setattr(_scraper, "_fetch_litellm_pricing", _empty)


# ── _previous_month ──────────────────────────────────────────────────────


def test_previous_month_rolls_back_one_month():
    assert _previous_month("2026_04") == "2026_03"


def test_previous_month_rolls_back_across_year_boundary():
    assert _previous_month("2026_01") == "2025_12"


def test_previous_month_handles_bad_input():
    assert _previous_month("not-a-month") is None
    assert _previous_month("") is None


# ── update_current_pricing — inheritance ────────────────────────────────


def _seed_month(dir_: Path, month: str, models: dict, note: str = "") -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{month}.json").write_text(
        json.dumps({"models": models, "note": note, "month": month}, indent=2)
    )


@pytest.mark.asyncio
async def test_inherits_full_catalog_when_current_has_one_model(tmp_path, monkeypatch):
    """Exact reproduction of BUG-2: current month was overwritten with a
    lone test model; the scraper must rebuild from the previous month."""
    monkeypatch.setattr(_billing, "_PRICING_DIR", tmp_path)

    prev_catalog = {
        "claude-opus-4-6": {"input_price": 5.0, "output_price": 25.0},
        "gpt-5-4": {"input_price": 5.0, "output_price": 20.0},
        "gemini-3-pro": {"input_price": 0.5, "output_price": 3.0},
    }
    current_month = _billing.current_pricing_month()
    prev_month = _previous_month(current_month)
    _seed_month(tmp_path, prev_month, prev_catalog)
    _seed_month(
        tmp_path,
        current_month,
        {"fake:model-v1": {"input_price": 3.0, "output_price": 15.0}},
    )

    result = await update_current_pricing()
    assert result["inherited"] == 3
    from plugboard import official_prices

    official = len(official_prices.prices_for_month(current_month))
    assert result["total"] == 4 + official  # 3 inherited + 1 existing + official

    # Re-read from disk to confirm it was persisted
    reloaded = _billing.load_pricing(current_month)
    assert "claude-opus-4-6" in reloaded["models"]
    assert "gpt-5-4" in reloaded["models"]
    assert "fake:model-v1" in reloaded["models"]  # existing entries preserved


@pytest.mark.asyncio
async def test_inheritance_never_overwrites_existing_entries(tmp_path, monkeypatch):
    """If a model already has a price in the current month, the previous
    month's entry must not clobber it."""
    monkeypatch.setattr(_billing, "_PRICING_DIR", tmp_path)

    current_month = _billing.current_pricing_month()
    prev_month = _previous_month(current_month)
    _seed_month(
        tmp_path,
        prev_month,
        {"shared-model": {"input_price": 1.0, "output_price": 2.0}},
    )
    _seed_month(
        tmp_path,
        current_month,
        {"shared-model": {"input_price": 9.0, "output_price": 99.0}},
    )

    await update_current_pricing()

    reloaded = _billing.load_pricing(current_month)
    assert reloaded["models"]["shared-model"]["input_price"] == 9.0
    assert reloaded["models"]["shared-model"]["output_price"] == 99.0


@pytest.mark.asyncio
async def test_empty_previous_month_is_handled_gracefully(tmp_path, monkeypatch):
    """If there is no previous-month file, the scraper must not crash."""
    monkeypatch.setattr(_billing, "_PRICING_DIR", tmp_path)

    current_month = _billing.current_pricing_month()
    _seed_month(
        tmp_path, current_month, {"foo": {"input_price": 1.0, "output_price": 2.0}}
    )

    result = await update_current_pricing()
    assert result["inherited"] == 0
    assert result["total"] >= 1
