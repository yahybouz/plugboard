"""MR-A2: merging prices from several sources, manual edits and drift review."""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses

from plugboard import billing, pricing_merge, pricing_scraper, pricing_suggestions
from plugboard.pricing_merge import merge_sources

ORDER = ["external", "provider_native", "models_dev", "litellm", "known"]
AUTH = {"Authorization": "Bearer testkey"}


def p(inp, out, **extra):
    return {"input_price": inp, "output_price": out, **extra}


# -- merge_sources -------------------------------------------------------------


def test_order_picks_highest_priority_source():
    r = merge_sources({}, {"litellm": {"m": p(2, 8)}, "models_dev": {"m": p(2.1, 8.4)}}, ORDER)
    assert r.models["m"] == p(2.1, 8.4, source="models_dev")
    assert r.added == ["m"]


def test_lower_source_fills_a_gap():
    r = merge_sources({}, {"models_dev": {"a": p(1, 2)}, "litellm": {"b": p(3, 4)}}, ORDER)
    assert r.models["a"]["source"] == "models_dev"
    assert r.models["b"]["source"] == "litellm"


def test_silent_source_leaves_entry_unchanged():
    existing = {"m": p(2, 8, source="models_dev")}
    r = merge_sources(existing, {"litellm": {"m": p(2.2, 8.8)}}, ORDER)
    assert r.models["m"] == existing["m"]
    assert r.updated == [] and r.drifts == []


def test_same_source_updates_its_own_entry():
    r = merge_sources({"m": p(2, 8, source="litellm")}, {"litellm": {"m": p(2.2, 8.8)}}, ORDER)
    assert r.models["m"] == p(2.2, 8.8, source="litellm")
    assert r.updated[0]["pct"] == 10.0


def test_stable_when_nothing_changes():
    existing = {"m": p(2, 8, source="litellm")}
    r = merge_sources(existing, {"litellm": {"m": p(2, 8)}}, ORDER)
    assert r.models == existing and not r.added and not r.updated


def test_manual_is_never_replaced():
    existing = {"m": p(9, 9, source="manual")}
    r = merge_sources(existing, {"external": {"m": p(1, 2)}}, ORDER)
    assert r.models["m"] == existing["m"]


def test_llm_entry_is_replaced_by_any_structured_source_even_far_off():
    r = merge_sources({"m": p(10, 40, source="llm")}, {"known": {"m": p(1, 4)}}, ORDER)
    assert r.models["m"] == p(1, 4, source="known")


def test_out_of_bounds_and_bad_ratio_are_rejected():
    sources = {"litellm": {"big": p(1001, 1), "ratio": p(1, 60), "neg": p(-1, 1), "bad": {"input_price": "x"}}}
    r = merge_sources({}, sources, ORDER)
    assert r.models == {}
    assert {x["model"] for x in r.rejected} == {"big", "ratio", "neg", "bad"}


def test_free_model_is_valid():
    r = merge_sources({}, {"known": {"llama3": p(0, 0)}}, ORDER)
    assert r.models["llama3"] == p(0, 0, source="known")


def test_openrouter_per_token_strings():
    assert pricing_merge.per_token_to_per_million("0.000003") == 3.0
    assert pricing_merge.per_token_to_per_million("-1") is None
    assert pricing_merge.per_token_to_per_million(None) is None


def test_drift_is_held_for_review():
    existing = {"m": p(2, 8, source="litellm")}
    r = merge_sources(existing, {"litellm": {"m": p(3, 12)}}, ORDER, drift_pct=20, action="review")
    assert r.models["m"] == existing["m"]
    assert r.drifts[0]["pct"] == 50.0 and r.drifts[0]["new"] == p(3, 12)


def test_drift_apply_writes_it():
    r = merge_sources({"m": p(2, 8, source="litellm")}, {"litellm": {"m": p(3, 12)}}, ORDER, action="apply")
    assert r.models["m"] == p(3, 12, source="litellm")


def test_external_is_trusted_and_not_held():
    r = merge_sources({"m": p(2, 10)}, {"external": {"m": p(3, 15)}}, ORDER, action="review")
    assert r.models["m"] == p(3, 15, source="external")
    assert r.drifts == []


def test_first_population_disagreement_is_held():
    sources = {"models_dev": {"m": p(5, 20)}, "litellm": {"m": p(2, 8)}}
    r = merge_sources({}, sources, ORDER, action="review")
    assert "m" not in r.models
    assert r.drifts[0]["old"] is None and "litellm" in r.drifts[0]["reason"]


def test_placeholder_zero_entry_is_filled():
    r = merge_sources({"m": p(0, 0)}, {"known": {"m": p(1, 4)}}, ORDER)
    assert r.models["m"] == p(1, 4, source="known")


def test_existing_is_not_mutated():
    existing = {"m": p(2, 8, source="litellm")}
    merge_sources(existing, {"litellm": {"m": p(2.1, 8)}}, ORDER)
    assert existing == {"m": p(2, 8, source="litellm")}


# -- configuration --------------------------------------------------------------


@pytest.fixture
def fresh_warnings(monkeypatch):
    from plugboard import envcompat

    monkeypatch.setattr(pricing_merge, "_warned", set())
    monkeypatch.setattr(envcompat, "_warned", set())
    for name in (
        "PLUGBOARD_PRICING_SOURCES",
        "PLUGBOARD_PRICING_SOURCE_URL",
        "PRICING_SOURCE_URL",
        "PLUGBOARD_PRICE_DRIFT_PCT",
        "PLUGBOARD_PRICE_DRIFT_ACTION",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_order(fresh_warnings):
    assert pricing_merge.source_order() == ORDER


def test_legacy_url_keeps_old_behaviour_and_warns(fresh_warnings, monkeypatch, caplog):
    monkeypatch.setenv("PRICING_SOURCE_URL", "https://prices.example/p.json")
    assert pricing_merge.pricing_source_url() == "https://prices.example/p.json"
    assert pricing_merge.source_order() == ["external", "known"]
    assert "Pricing sources unchanged (external, known)" in caplog.text
    assert "deprecated" in caplog.text


def test_canonical_url_wins_over_legacy(fresh_warnings, monkeypatch):
    monkeypatch.setenv("PRICING_SOURCE_URL", "https://old.example/p.json")
    monkeypatch.setenv("PLUGBOARD_PRICING_SOURCE_URL", "https://new.example/p.json")
    assert pricing_merge.pricing_source_url() == "https://new.example/p.json"


def test_explicit_order_and_invalid_order(fresh_warnings, monkeypatch, caplog):
    monkeypatch.setenv("PLUGBOARD_PRICING_SOURCES", "litellm, known,litellm")
    assert pricing_merge.source_order() == ["litellm", "known"]
    monkeypatch.setenv("PLUGBOARD_PRICING_SOURCES", "litellm,nope")
    assert pricing_merge.source_order() == ORDER
    assert "unknown source nope" in caplog.text


def test_drift_settings(fresh_warnings, monkeypatch):
    assert pricing_merge.drift_threshold_pct() == 20.0
    assert pricing_merge.drift_action() == "review"
    monkeypatch.setenv("PLUGBOARD_PRICE_DRIFT_PCT", "abc")
    monkeypatch.setenv("PLUGBOARD_PRICE_DRIFT_ACTION", "maybe")
    assert pricing_merge.drift_threshold_pct() == 20.0
    assert pricing_merge.drift_action() == "review"
    monkeypatch.setenv("PLUGBOARD_PRICE_DRIFT_PCT", "35")
    monkeypatch.setenv("PLUGBOARD_PRICE_DRIFT_ACTION", "apply")
    assert pricing_merge.drift_threshold_pct() == 35.0
    assert pricing_merge.drift_action() == "apply"


# -- update_current_pricing -------------------------------------------------------


@pytest.fixture
def grid(tmp_path, monkeypatch, fresh_warnings):
    pricing_dir = tmp_path / "pricing"
    pricing_dir.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing_dir)
    monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", "")
    monkeypatch.setattr(pricing_scraper, "_openrouter_configured", lambda: False)
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({}, {}))

    async def _none():
        return {}

    monkeypatch.setattr(pricing_scraper, "_fetch_litellm_pricing", _none)
    month = billing.current_pricing_month()

    def seed(models, m=month):
        (pricing_dir / f"{m}.json").write_text(json.dumps({"models": models, "month": m}))

    return month, seed


@pytest.mark.asyncio
async def test_refresh_uses_models_dev_and_tags_source(grid, monkeypatch):
    month, seed = grid
    seed({"old": p(1, 2)})
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({"new-model": p(2, 6)}, {}))
    result = await pricing_scraper.update_current_pricing()
    models = billing.load_pricing(month)["models"]
    assert models["new-model"] == p(2, 6, source="models_dev")
    assert models["old"] == p(1, 2)
    assert result["sources"] == ["models_dev"] and result["added"] == 1


@pytest.mark.asyncio
async def test_refresh_holds_drift_as_suggestion(grid, monkeypatch):
    month, seed = grid
    seed({"m": p(2, 8, source="models_dev")})
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({"m": p(4, 16)}, {}))
    result = await pricing_scraper.update_current_pricing()
    assert result["drifts"] == 1
    assert billing.load_pricing(month)["models"]["m"]["input_price"] == 2
    [item] = pricing_suggestions.list_suggestions()
    assert item["type"] == "drift" and item["proposed"] == p(4, 16) and item["current"] == p(2, 8)
    # Same proposal on the next pass keeps the same id, no duplicate.
    await pricing_scraper.update_current_pricing()
    assert len(pricing_suggestions.list_suggestions()) == 1


@pytest.mark.asyncio
async def test_refresh_reads_openrouter_prices(grid, monkeypatch):
    month, seed = grid
    seed({})
    monkeypatch.setattr(pricing_scraper, "_openrouter_configured", lambda: True)
    payload = {"data": [
        {"id": "vendor/model-a", "pricing": {"prompt": "0.000001", "completion": "0.000004"}},
        {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
    ]}
    with aioresponses() as mock:
        mock.get(pricing_scraper._OPENROUTER_MODELS_URL, payload=payload)
        await pricing_scraper.update_current_pricing()
    grid = billing.load_pricing(month)
    assert "vendor/model-a" not in grid["models"]
    sub = grid["models_by_provider"]["openrouter"]
    assert sub["vendor/model-a"] == p(1.0, 4.0, source="provider_native")
    assert "openrouter/auto" not in sub


@pytest.mark.asyncio
async def test_refresh_never_touches_manual_or_past_month(grid, monkeypatch):
    month, seed = grid
    prev = pricing_scraper._previous_month(month)
    seed({"m": p(1, 1)}, prev)
    seed({"m": p(9, 9, source="manual")})
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({"m": p(2, 6)}, {}))
    before_prev = (billing._PRICING_DIR / f"{prev}.json").read_text()
    await pricing_scraper.update_current_pricing()
    assert billing.load_pricing(month)["models"]["m"] == p(9, 9, source="manual")
    assert (billing._PRICING_DIR / f"{prev}.json").read_text() == before_prev


@pytest.mark.asyncio
async def test_refresh_without_change_does_not_write(grid, monkeypatch):
    month, seed = grid
    seed({"m": p(2, 6, source="models_dev")})
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({"m": p(2, 6)}, {}))
    calls = []
    monkeypatch.setattr(pricing_scraper, "save_pricing", lambda *a: calls.append(a))
    await pricing_scraper.update_current_pricing()
    assert calls == []


@pytest.mark.asyncio
async def test_known_prices_do_not_override_priced_entries(grid):
    month, seed = grid
    seed({"gpt-5-mini": p(7, 7)})
    await pricing_scraper.update_current_pricing(provider_models=["openai:gpt-5-mini"])
    assert billing.load_pricing(month)["models"]["gpt-5-mini"] == p(7, 7)


# -- admin endpoints --------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_marks_only_changed_rows_manual(client, grid):
    month, seed = grid
    seed({"a": p(1, 2, source="litellm"), "b": p(3, 4, source="litellm")})
    resp = await client.patch(
        f"/admin/pricing/{month}", headers=AUTH,
        json={"models": {"a": p(1, 2), "c": p(5, 6)}},
    )
    assert resp.status_code == 200, resp.text
    models = resp.json()["models"]
    assert models["a"] == p(1, 2, source="litellm")
    assert models["b"] == p(3, 4, source="litellm")
    assert models["c"] == p(5, 6, source="manual")


@pytest.mark.asyncio
async def test_patch_validates_prices_and_month(client, grid):
    month, seed = grid
    seed({})
    bad = await client.patch(f"/admin/pricing/{month}", headers=AUTH, json={"models": {"a": p(-1, 2)}})
    assert bad.status_code == 422
    bad_month = await client.patch("/admin/pricing/2026-13", headers=AUTH, json={"models": {}})
    assert bad_month.status_code == 422


@pytest.mark.asyncio
async def test_put_without_change_keeps_sources(client, grid):
    month, seed = grid
    seed({"a": p(1, 2, source="litellm")})
    resp = await client.put(
        f"/admin/pricing/{month}", headers=AUTH,
        json={"models": {"a": p(1.0000000001, 2), "b": p(1, 1)}, "note": "n"},
    )
    assert resp.status_code == 200
    models = resp.json()["models"]
    assert models["a"]["source"] == "litellm"
    assert models["b"]["source"] == "manual"


@pytest.mark.asyncio
async def test_revert_recomputes_from_cached_sources(client, grid, monkeypatch):
    month, seed = grid
    seed({})
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({"m": p(2, 6)}, {}))
    await pricing_scraper.update_current_pricing()
    await client.patch(f"/admin/pricing/{month}", headers=AUTH, json={"models": {"m": p(9, 9)}})
    resp = await client.post(f"/admin/pricing/{month}/revert", headers=AUTH, json={"model": "m"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["entry"] == p(2, 6, source="models_dev")
    assert billing.load_pricing(month)["models"]["m"] == p(2, 6, source="models_dev")


@pytest.mark.asyncio
async def test_revert_refuses_non_manual_unknown_and_past(client, grid):
    month, seed = grid
    seed({"m": p(1, 2, source="litellm")})
    not_manual = await client.post(f"/admin/pricing/{month}/revert", headers=AUTH, json={"model": "m"})
    assert not_manual.status_code == 409
    unknown = await client.post(f"/admin/pricing/{month}/revert", headers=AUTH, json={"model": "zz"})
    assert unknown.status_code == 404
    past = await client.post("/admin/pricing/2020_01/revert", headers=AUTH, json={"model": "m"})
    assert past.status_code == 409


@pytest.mark.asyncio
async def test_suggestion_apply_reject_and_conflict(client, grid, monkeypatch):
    month, seed = grid
    seed({"m": p(2, 8, source="models_dev"), "n": p(1, 4, source="models_dev")})
    monkeypatch.setattr(
        pricing_scraper, "_models_dev_pricing", lambda: ({"m": p(4, 16), "n": p(3, 12)}, {})
    )
    await pricing_scraper.update_current_pricing()
    listing = (await client.get("/admin/pricing/suggestions", headers=AUTH)).json()["items"]
    by_model = {s["model"]: s["id"] for s in listing}
    assert set(by_model) == {"m", "n"}

    applied = await client.post(f"/admin/pricing/suggestions/{by_model['m']}/apply", headers=AUTH)
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    assert billing.load_pricing(month)["models"]["m"] == p(4, 16, source="models_dev")
    again = await client.post(f"/admin/pricing/suggestions/{by_model['m']}/apply", headers=AUTH)
    assert again.status_code == 409

    # The grid entry changed under the suggestion: 409 and the item turns stale.
    await client.patch(f"/admin/pricing/{month}", headers=AUTH, json={"models": {"n": p(1.5, 4)}})
    stale = await client.post(f"/admin/pricing/suggestions/{by_model['n']}/apply", headers=AUTH)
    assert stale.status_code == 409
    assert pricing_suggestions.get(by_model["n"])["status"] == "stale"

    missing = await client.post("/admin/pricing/suggestions/nope/reject", headers=AUTH)
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_bulk_reject_is_remembered(client, grid, monkeypatch):
    month, seed = grid
    seed({"m": p(2, 8, source="models_dev")})
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({"m": p(4, 16)}, {}))
    await pricing_scraper.update_current_pricing()
    [item] = pricing_suggestions.list_suggestions()
    resp = await client.post(
        "/admin/pricing/suggestions/reject", headers=AUTH, json={"ids": [item["id"], "nope"]}
    )
    body = resp.json()
    assert [d["status"] for d in body["done"]] == ["rejected"]
    assert body["errors"][0]["status"] == 404
    await pricing_scraper.update_current_pricing()
    assert pricing_suggestions.list_suggestions() == []


@pytest.mark.asyncio
async def test_revert_without_cached_sources_restores_the_replaced_price(client, grid, monkeypatch):
    month, seed = grid
    seed({"m": p(2, 6, source="litellm")})
    monkeypatch.setattr(pricing_scraper, "_last_sources", {})
    await client.patch(f"/admin/pricing/{month}", headers=AUTH, json={"models": {"m": p(9, 9)}})
    stored = billing.load_pricing(month)["models"]["m"]
    assert stored["source"] == "manual" and stored["previous"] == p(2, 6, source="litellm")
    # A second manual edit keeps the original automatic price.
    await client.patch(f"/admin/pricing/{month}", headers=AUTH, json={"models": {"m": p(8, 8)}})
    resp = await client.post(f"/admin/pricing/{month}/revert", headers=AUTH, json={"model": "m"})
    assert resp.json()["entry"] == p(2, 6, source="litellm")
