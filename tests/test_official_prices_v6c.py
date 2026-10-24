"""V6c: the dated official price table flows through the pricing scheduler.

Offline: every network fetcher is stubbed, no provider or CLI is called.
"""

from __future__ import annotations

import json
import logging
from datetime import date

import pytest

from plugboard import billing, pricing_scraper

ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"
TYPESAFE = "https://docs.typesafe.ai/models#current-models"


def _price(entry):
    return (
        round(float(entry["input_price"]), 6),
        round(float(entry["output_price"]), 6),
    )


@pytest.fixture
def grid(tmp_path, monkeypatch):
    """Empty pricing directory; network fetchers fail like a dead network."""
    pricing = tmp_path / "pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    monkeypatch.setattr(billing, "_GRID_CACHE", {})
    for name in (
        "PLUGBOARD_PRICING_SOURCES",
        "PLUGBOARD_PRICING_SOURCE_URL",
        "PRICING_SOURCE_URL",
        "PLUGBOARD_OFFICIAL_PRICES_MAX_AGE_DAYS",
    ):
        monkeypatch.delenv(name, raising=False)

    async def dead_network():
        raise OSError("synthetic: network unreachable")

    for fetcher in (
        "_fetch_external_source",
        "_fetch_provider_native_pricing",
        "_fetch_ollama_pricing",
        "_fetch_litellm_pricing",
    ):
        monkeypatch.setattr(pricing_scraper, fetcher, dead_network)
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({}, {}))
    monkeypatch.setattr(pricing_scraper, "_openrouter_configured", lambda: True)
    return pricing


def _month(monkeypatch, month):
    monkeypatch.setattr(pricing_scraper, "current_pricing_month", lambda: month)
    monkeypatch.setattr(billing, "current_pricing_month", lambda: month)


def _seed(pricing_dir, month, models, **extra):
    (pricing_dir / f"{month}.json").write_text(
        json.dumps({"month": month, "models": models, **extra})
    )


# -- the table ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "argon", "flash"),
    [
        (date(2026, 11, 30), (2.0, 10.0), (0.75, 3.75)),
        (date(2026, 12, 1), (4.0, 20.0), (0.75, 3.75)),
        (date(2026, 12, 31), (4.0, 20.0), (0.75, 3.75)),
        (date(2027, 1, 1), (4.0, 20.0), (1.5, 7.5)),
    ],
)
def test_dated_prices_switch_on_their_effective_day(day, argon, flash):
    from plugboard import official_prices

    prices = official_prices.prices_on(day)
    for effort in ("low", "medium", "high"):
        assert _price(prices[f"gemini-4-argon-{effort}"]) == argon
        assert _price(prices[f"gemini-3.8-flash-{effort}"]) == flash


def test_grid_month_takes_the_price_in_force_on_its_first_day():
    from plugboard import official_prices

    assert _price(
        official_prices.prices_for_month("2026_11")["gemini-4-argon-low"]
    ) == (
        2.0,
        10.0,
    )
    assert _price(
        official_prices.prices_for_month("2026_12")["gemini-4-argon-low"]
    ) == (
        4.0,
        20.0,
    )


def test_table_has_one_price_per_model_on_every_day():
    from plugboard import official_prices

    entries = official_prices.entries()
    day = date(2026, 1, 1)
    while day <= date(2028, 1, 1):
        in_force = [
            e["model"]
            for e in entries
            if (e.get("effective_from") or "0000") <= day.isoformat()
            and day.isoformat() <= (e.get("effective_until") or "9999")
        ]
        assert len(in_force) == len(set(in_force)), day
        day = date.fromordinal(day.toordinal() + 1)


def test_table_prices_match_the_checked_vendor_pages():
    from plugboard import official_prices

    prices = official_prices.prices_for_month("2026_10")
    assert _price(prices["jev"]) == (0.042, 0.0)
    assert _price(prices["laya"]) == _price(prices["laya-multilingual"]) == (0.0, 0.0)
    assert _price(prices["claude-haiku-5-5"]) == (0.1, 0.5)
    assert _price(prices["claude-sonnet-5-5-medium"]) == (2.0, 10.0)
    assert prices["claude-opus-5-5"]["source_url"] == ANTHROPIC
    assert prices["jev"]["source_url"] == TYPESAFE


@pytest.mark.parametrize(
    ("days_after", "env", "stale", "max_age"),
    [
        (35, None, False, 35),
        (36, None, True, 35),
        (60, "100", False, 100),
        (36, "not-a-number", True, 35),
        (36, "0", True, 35),
    ],
)
def test_staleness_threshold_is_configurable(
    monkeypatch, days_after, env, stale, max_age
):
    from plugboard import official_prices

    if env is None:
        monkeypatch.delenv("PLUGBOARD_OFFICIAL_PRICES_MAX_AGE_DAYS", raising=False)
    else:
        monkeypatch.setenv("PLUGBOARD_OFFICIAL_PRICES_MAX_AGE_DAYS", env)
    checked = date.fromisoformat(official_prices.status()["checked"])
    result = official_prices.status(
        today=date.fromordinal(checked.toordinal() + days_after)
    )
    assert result["stale"] is stale
    assert result["max_age_days"] == max_age


# -- the scheduler pass --------------------------------------------------------------


async def test_dead_network_still_writes_official_prices(grid, monkeypatch):
    _month(monkeypatch, "2026_10")
    summary = await pricing_scraper.update_current_pricing()
    models = billing.load_pricing("2026_10")["models"]
    assert summary["sources"] == ["official"]
    assert _price(models["claude-sonnet-5-5"]) == (2.0, 10.0)
    assert models["claude-sonnet-5-5"]["source"] == "official"
    assert models["claude-sonnet-5-5"]["source_url"] == ANTHROPIC
    assert _price(models["jev"]) == (0.042, 0.0)


async def test_official_replaces_inherited_known_price_without_review(
    grid, monkeypatch
):
    _month(monkeypatch, "2026_12")
    _seed(
        grid,
        "2026_11",
        {
            "gemini-4-argon-low": {
                "input_price": 2.0,
                "output_price": 10.0,
                "source": "known",
            }
        },
    )
    summary = await pricing_scraper.update_current_pricing()
    assert summary["drifts"] == 0
    assert _price(billing.load_pricing("2026_12")["models"]["gemini-4-argon-low"]) == (
        4.0,
        20.0,
    )


async def test_rollover_updates_the_review_baseline_and_keeps_drift_review(
    grid, monkeypatch
):
    _month(monkeypatch, "2026_12")
    _seed(
        grid,
        "2026_12",
        {
            "gemini-4-argon-low": {
                "input_price": 2.0,
                "output_price": 10.0,
                "source": "official",
            },
            "other": {"input_price": 2.0, "output_price": 8.0, "source": "models_dev"},
        },
        reviewed_prices={
            "": {"gemini-4-argon-low": {"input_price": 2.0, "output_price": 10.0}}
        },
    )
    monkeypatch.setattr(
        pricing_scraper,
        "_models_dev_pricing",
        lambda: ({"other": {"input_price": 4.0, "output_price": 16.0}}, {}),
    )
    summary = await pricing_scraper.update_current_pricing()
    saved = billing.load_pricing("2026_12")
    assert saved["reviewed_prices"][""]["gemini-4-argon-low"] == {
        "input_price": 4.0,
        "output_price": 20.0,
    }
    assert _price(saved["models"]["other"]) == (2.0, 8.0)  # still held for review
    assert summary["drifts"] == 1


async def test_a_higher_source_drops_the_official_url(grid, monkeypatch):
    _month(monkeypatch, "2026_10")
    monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", "https://prices.test/p")

    async def operator_grid():
        return {"claude-opus-5-5": {"input_price": 5.0, "output_price": 25.0}}

    monkeypatch.setattr(pricing_scraper, "_fetch_external_source", operator_grid)
    _seed(
        grid,
        "2026_10",
        {
            "claude-opus-5-5": {
                "input_price": 4.0,
                "output_price": 20.0,
                "source": "official",
                "source_url": ANTHROPIC,
            }
        },
    )
    await pricing_scraper.update_current_pricing()
    entry = billing.load_pricing("2026_10")["models"]["claude-opus-5-5"]
    assert entry == {"input_price": 5.0, "output_price": 25.0, "source": "external"}


async def test_revert_gives_back_the_official_price(grid, monkeypatch):
    _month(monkeypatch, "2026_10")
    await pricing_scraper.update_current_pricing()
    saved = billing.load_pricing("2026_10")
    saved["models"]["claude-opus-5-5"] = {
        "input_price": 1.0,
        "output_price": 1.0,
        "source": "manual",
    }
    billing.save_pricing("2026_10", saved)
    new = pricing_scraper.revert_model("2026_10", "claude-opus-5-5")
    assert new == {
        "input_price": 4.0,
        "output_price": 20.0,
        "source": "official",
        "source_url": ANTHROPIC,
    }


async def test_known_table_never_contradicts_the_official_one(grid, monkeypatch):
    # With only the built-in table enabled, a routable model that the official
    # table prices must not get a stale built-in price written for it.
    _month(monkeypatch, "2026_12")
    monkeypatch.setenv("PLUGBOARD_PRICING_SOURCES", "known")
    await pricing_scraper.update_current_pricing(
        provider_models=[
            "agy-cli-subprocess:gemini-4-argon-low",
            "agy-cli-subprocess:claude-opus-5-5-medium",
        ]
    )
    models = billing.load_pricing("2026_12").get("models") or {}
    assert "gemini-4-argon-low" not in models
    assert "claude-opus-5-5-medium" not in models
    match = billing.resolve_price("gemini-4-argon-low", "2026_12")
    assert match is not None and _price(match.entry) == (4.0, 20.0)


async def test_stale_table_warns_once_per_pass(grid, monkeypatch, caplog):
    from plugboard import official_prices

    _month(monkeypatch, "2026_10")
    monkeypatch.setattr(official_prices, "_today", lambda: date(2027, 6, 1))
    caplog.set_level(logging.WARNING, logger="plugboard.pricing_scraper")

    def warnings():
        return [
            r
            for r in caplog.records
            if r.name == "plugboard.pricing_scraper"
            and "official price table" in r.getMessage().lower()
        ]

    await pricing_scraper.update_current_pricing()
    assert len(warnings()) == 1
    await pricing_scraper.update_current_pricing()
    assert len(warnings()) == 2


async def test_decision_names_never_become_chat_models(grid, monkeypatch):
    from plugboard import model_catalog

    _month(monkeypatch, "2026_10")
    await pricing_scraper.update_current_pricing()
    assert "jev" in billing.load_pricing("2026_10")["models"]
    for kind in ("claude", "codex", "gemini"):
        found = model_catalog.discover_subprocess_models_from_pricing(kind)
        assert not [m for m in found if m == "jev" or m.startswith("laya")], kind


# -- scheduler status ----------------------------------------------------------------


def _scheduler(tmp_path):
    from plugboard.pricing_scheduler import PricingScheduler

    async def update():
        return {}

    return PricingScheduler(
        update_fn=update, providers_fn=lambda: [], cb_state_file=tmp_path / "cb.json"
    )


def test_status_alias_reports_official_freshness_without_starting(tmp_path):
    from plugboard import official_prices

    scheduler = _scheduler(tmp_path)
    status = scheduler.status()
    assert status["official"] == official_prices.status()
    assert scheduler.status_dict["official"] == status["official"]
    assert status["running"] is False and scheduler.task is None


@pytest.mark.parametrize(
    ("sources", "fresh"),
    [(["official"], False), (["official", "litellm"], True), (["known"], True)],
)
def test_an_offline_only_pass_does_not_count_as_fresh(tmp_path, sources, fresh):
    scheduler = _scheduler(tmp_path)
    scheduler._record_success("daily", {"sources": sources})
    assert (scheduler.last_success_at is not None) is fresh


# -- billing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("month", "expected"), [("2026_11", (2.0, 10.0)), ("2026_12", (4.0, 20.0))]
)
def test_unrefreshed_grid_bills_the_official_price_of_that_month(grid, month, expected):
    match = billing.resolve_price("gemini-4-argon-high", month)
    assert match is not None and _price(match.entry) == expected
    assert match.entry["source"] == "official"


def test_estimate_from_a_local_zero_is_not_free_on_other_hosts(grid):
    _seed(
        grid,
        "2026_10",
        {"gemma4": {"input_price": 0.0, "output_price": 0.0, "source": "known"}},
    )
    custom = {"id": "my-gateway", "base_url": "http://llm.internal.test/v1"}
    assert billing.resolve_price("gemma5", "2026_10", provider=custom) is None


def test_grid_local_zero_is_not_free_for_an_ollama_cloud_model(grid):
    _seed(
        grid,
        "2026_10",
        {"qwen3": {"input_price": 0.0, "output_price": 0.0, "source": "known"}},
    )
    ollama = {"id": "ollama", "base_url": "http://localhost:11434"}
    match = billing.resolve_price("qwen3:8b-cloud", "2026_10", provider=ollama)
    assert (
        match is None
        or match.entry.get("input_price")
        or match.entry.get("output_price")
    )


def test_single_grid_lookup_applies_the_same_rules(grid):
    pricing = {
        "month": "2026_12",
        "models": {
            "llama3": {"input_price": 0.0, "output_price": 0.0, "source": "known"}
        },
    }
    custom = {"id": "my-gateway", "base_url": "http://llm.internal.test/v1"}
    assert billing.resolve_model_price("llama3", pricing, provider=custom) is None
    entry = billing.resolve_model_price("gemini-4-argon-low", pricing, provider=custom)
    assert entry is not None and _price(entry) == (4.0, 20.0)


def test_admin_zero_for_a_local_name_is_a_price_on_any_host(grid):
    _seed(
        grid,
        "2026_10",
        {"llama3": {"input_price": 0.0, "output_price": 0.0, "source": "manual"}},
    )
    custom = {"id": "my-gateway", "base_url": "http://llm.internal.test/v1"}
    match = billing.resolve_price("llama3", "2026_10", provider=custom)
    assert match is not None and match.source == "manual"
    assert _price(match.entry) == (0.0, 0.0)


# -- decisions ---------------------------------------------------------------------------


async def test_invalid_grid_decision_price_falls_back_to_the_official_one(
    client, grid, monkeypatch, tmp_path
):
    from tests.test_decision_api import ANSWERS, REQUEST
    from tests.test_pricing_decision_parent import _engine, _key, _rows

    _seed(
        grid,
        billing.current_pricing_month(),
        {"jev": {"input_price": -5.0, "output_price": 0.0, "source": "manual"}},
    )
    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    _engine(monkeypatch, {**ANSWERS, "usage": {"input_tokens": 1_000_000}}, [])
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.json()["billing"] == {"status": "billed", "cost_usd": 0.042}
    [row] = _rows(rec["key_id"])
    assert row["price_source"] == f"official:{TYPESAFE}"


async def test_decision_row_names_the_grid_price_it_used(
    client, grid, monkeypatch, tmp_path
):
    from tests.test_decision_api import ANSWERS, REQUEST
    from tests.test_pricing_decision_parent import _engine, _key, _rows

    month = billing.current_pricing_month()
    _seed(
        grid,
        month,
        {"jev": {"input_price": 0.05, "output_price": 0.0, "source": "manual"}},
    )
    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    _engine(monkeypatch, ANSWERS, [])  # no usage: the 64,000-token estimate
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.json()["billing"] == {"status": "estimated", "cost_usd": 0.0032}
    [row] = _rows(rec["key_id"])
    assert row["price_source"] == f"grid-estimate:manual:{month}"


async def test_laya_grid_price_is_published_and_named(
    client, grid, monkeypatch, tmp_path
):
    from tests.test_decision_api import REQUEST
    from tests.test_decision_hardening_v5 import GatedEngine
    from tests.test_pricing_decision_parent import _key, _rows

    from plugboard import decision_api

    month = billing.current_pricing_month()
    _seed(
        grid,
        month,
        {"laya": {"input_price": 1.0, "output_price": 0.0, "source": "manual"}},
    )
    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["laya"]
    )
    monkeypatch.setattr(decision_api, "dispatch", GatedEngine(hold_first=False))
    response = await client.post(
        "/v1/decisions", headers=headers, json={**REQUEST, "model": "laya"}
    )
    assert response.json()["billing"] == {"status": "billed", "cost_usd": 0.0}
    [row] = _rows(rec["key_id"])
    assert row["price_source"] == f"grid:manual:{month}"
    listing = (await client.get("/v1/decisions/models", headers=headers)).json()
    assert listing["data"][0]["pricing"]["input_usd_per_mtok"] == 1.0
