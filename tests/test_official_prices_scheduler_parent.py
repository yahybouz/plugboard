"""Parent oracles V6c: official prices flow through the pricing scheduler (registre #115).

User: « Les mise a jour des prix doit se faire avec les crons etc ».
Contract: a curated, dated official price table (``plugboard.official_prices``) is a
structured source named ``official`` of the existing in-app scheduler. Each refresh
writes the official price in force for the grid month; dated vendor changes apply
by themselves at month rollover; admin ``manual`` entries still win; decisions
(Jev/Laya) are priced through the same grid. Offline: every network fetcher stubbed.
"""

from datetime import date

import pytest

from plugboard import billing, pricing_merge, pricing_scraper

PRICE_KEYS = ("input_price", "output_price")


@pytest.fixture(autouse=True)
def _offline_grid(tmp_path, monkeypatch):
    pricing = tmp_path / "pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    monkeypatch.delenv("PLUGBOARD_PRICING_SOURCES", raising=False)
    monkeypatch.delenv("PLUGBOARD_PRICING_SOURCE_URL", raising=False)
    monkeypatch.delenv("PRICING_SOURCE_URL", raising=False)

    async def offline(order):
        return {}, {}

    monkeypatch.setattr(pricing_scraper, "_fetch_sources", offline)
    events = []

    from plugboard import change_events

    monkeypatch.setattr(
        change_events.ChangeBatch,
        "add",
        lambda self, kind, data: events.append((kind, data)),
    )
    monkeypatch.setattr(change_events.ChangeBatch, "__len__", lambda self: 0)
    return events


def _month(monkeypatch, month):
    monkeypatch.setattr(pricing_scraper, "current_pricing_month", lambda: month)
    monkeypatch.setattr(billing, "current_pricing_month", lambda: month)


async def _refresh(monkeypatch, month):
    _month(monkeypatch, month)
    return await pricing_scraper.update_current_pricing()


def _grid(month):
    return billing.load_pricing(month).get("models") or {}


def _price(entry):
    return tuple(round(float(entry[k]), 6) for k in PRICE_KEYS)


def test_official_is_a_structured_source_ranked_above_catalogues():
    assert "official" in pricing_merge.STRUCTURED_SOURCES
    order = pricing_merge.source_order(external_configured=False)
    assert "official" in order
    for lower in ("provider_native", "models_dev", "litellm", "known"):
        if lower in order:
            assert order.index("official") < order.index(lower)
    assert pricing_merge.source_order(external_configured=True)[:2] == [
        "external",
        "official",
    ]


async def test_refresh_writes_official_prices_offline(monkeypatch):
    summary = await _refresh(monkeypatch, "2026_10")
    assert "official" in summary["sources"]
    grid = _grid("2026_10")
    for model, expected in {
        "claude-opus-5-5": (4.0, 20.0),
        "claude-fable-5-1": (10.0, 50.0),
        "gemini-4-argon-high": (2.0, 10.0),
        "gemini-3.8-flash-low": (0.75, 3.75),
        "gpt-4o-mini": (0.15, 0.6),
    }.items():
        assert _price(grid[model]) == expected, model
        assert grid[model].get("source") == "official", model
        assert str(grid[model].get("source_url", "")).startswith("https://"), model


@pytest.mark.parametrize(
    ("month", "argon", "flash"),
    [
        ("2026_11", (2.0, 10.0), (0.75, 3.75)),
        ("2026_12", (4.0, 20.0), (0.75, 3.75)),
        ("2027_01", (4.0, 20.0), (1.5, 7.5)),
    ],
)
async def test_dated_vendor_changes_apply_by_month(monkeypatch, month, argon, flash):
    await _refresh(monkeypatch, month)
    grid = _grid(month)
    assert _price(grid["gemini-4-argon-medium"]) == argon
    assert _price(grid["gemini-3.8-flash-high"]) == flash


async def test_rollover_overrides_inherited_official_price_and_announces_it(
    monkeypatch, _offline_grid
):
    await _refresh(monkeypatch, "2026_11")
    _offline_grid.clear()
    await _refresh(monkeypatch, "2026_12")
    assert _price(_grid("2026_12")["gemini-4-argon-low"]) == (4.0, 20.0)
    assert _price(_grid("2026_11")["gemini-4-argon-low"]) == (
        2.0,
        10.0,
    )  # past month untouched
    changed = [
        d
        for kind, d in _offline_grid
        if kind == "pricing.changed" and d["model"] == "gemini-4-argon-low"
    ]
    assert changed and changed[0]["status"] == "applied"


async def test_manual_admin_price_is_never_overwritten(monkeypatch):
    _month(monkeypatch, "2026_10")
    billing.save_pricing(
        "2026_10",
        {
            "models": {
                "claude-opus-5-5": {
                    "input_price": 3.0,
                    "output_price": 15.0,
                    "source": "manual",
                }
            }
        },
    )
    await pricing_scraper.update_current_pricing()
    assert _price(_grid("2026_10")["claude-opus-5-5"]) == (3.0, 15.0)


async def test_refresh_is_idempotent(monkeypatch):
    await _refresh(monkeypatch, "2026_10")
    first = _grid("2026_10")
    await pricing_scraper.update_current_pricing()
    assert _grid("2026_10") == first


def test_official_table_is_dated_and_sourced():
    from plugboard import official_prices

    entries = official_prices.entries()
    assert entries
    for entry in entries:
        assert entry["source_url"].startswith("https://")
        date.fromisoformat(entry["checked"])
        assert float(entry["input_price"]) >= 0 and float(entry["output_price"]) >= 0
    models = {e["model"] for e in entries}
    assert {
        "jev",
        "laya",
        "laya-multilingual",
        "claude-opus-5-5",
        "gpt-4o-mini",
    } <= models
    assert not {"gpt-oss-120b-medium", "meta-llama-3.1-405b-instruct"} & models


def test_official_staleness_is_reported():
    from plugboard import official_prices

    newest = max(date.fromisoformat(e["checked"]) for e in official_prices.entries())
    fresh = official_prices.status(today=newest)
    assert fresh["stale"] is False and fresh["checked"] == newest.isoformat()
    old = official_prices.status(today=date.fromordinal(newest.toordinal() + 60))
    assert old["stale"] is True


async def test_scheduler_status_exposes_official_freshness(monkeypatch):
    from plugboard import pricing_scheduler

    status = (
        pricing_scheduler.status() if hasattr(pricing_scheduler, "status") else None
    )
    if status is None:
        scheduler = pricing_scheduler.PricingScheduler()
        status = scheduler.status()
    assert "official" in status and "stale" in status["official"]


async def test_decisions_are_priced_through_the_grid(client, monkeypatch, tmp_path):
    from tests.test_decision_api import ANSWERS, REQUEST
    from tests.test_pricing_decision_parent import _engine, _key

    _month(monkeypatch, billing.current_pricing_month())
    month = billing.current_pricing_month()
    billing.save_pricing(
        month,
        {
            "models": {
                "jev": {"input_price": 0.05, "output_price": 0.0, "source": "manual"}
            }
        },
    )
    headers, _ = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    _engine(
        monkeypatch,
        {**ANSWERS, "usage": {"input_tokens": 1_000_000, "output_tokens": 3}},
        [],
    )
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    assert response.json()["billing"]["cost_usd"] == pytest.approx(0.05)
    data = (await client.get("/v1/decisions/models", headers=headers)).json()["data"]
    assert {m["id"]: m["pricing"]["input_usd_per_mtok"] for m in data}[
        "jev"
    ] == pytest.approx(0.05)


async def test_decisions_fall_back_to_official_table_without_grid(
    client, monkeypatch, tmp_path
):
    from tests.test_decision_api import ANSWERS, REQUEST
    from tests.test_pricing_decision_parent import _engine, _key

    headers, _ = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    _engine(
        monkeypatch,
        {**ANSWERS, "usage": {"input_tokens": 1_000_000, "output_tokens": 3}},
        [],
    )
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.json()["billing"]["cost_usd"] == pytest.approx(0.042)


@pytest.mark.parametrize("model", ["llama3", "qwen3:8b"])
@pytest.mark.parametrize("source", ["known", None, "official"])
def test_grid_zero_by_name_is_not_free_on_non_ollama_hosts(monkeypatch, model, source):
    entry = {"input_price": 0.0, "output_price": 0.0}
    if source:
        entry["source"] = source
    billing.save_pricing("2026_10", {"models": {model: entry}})
    custom = {"id": "my-gateway", "base_url": "http://llm.internal.test/v1"}
    match = billing.resolve_price(model, "2026_10", provider=custom)
    free = (
        match is not None
        and not match.entry.get("input_price")
        and not match.entry.get("output_price")
    )
    assert not free
    ollama = {"id": "ollama", "base_url": "http://localhost:11434"}
    local = billing.resolve_price(model, "2026_10", provider=ollama)
    assert (
        local is not None
        and not local.entry.get("input_price")
        and not local.entry.get("output_price")
    )


def test_manual_zero_by_admin_stays_authoritative():
    billing.save_pricing(
        "2026_10",
        {
            "models": {
                "house-model": {"input_price": 0, "output_price": 0, "source": "manual"}
            }
        },
    )
    custom = {"id": "my-gateway", "base_url": "http://llm.internal.test/v1"}
    match = billing.resolve_price("house-model", "2026_10", provider=custom)
    assert match is not None and match.entry["input_price"] == 0
