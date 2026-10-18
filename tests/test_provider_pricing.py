"""MR-A3: prices per provider, billing by the provider that served the call."""

from __future__ import annotations

import json
import os
import time

import pytest
from aioresponses import aioresponses

from plugboard import billing, pricing_scraper, pricing_suggestions
from tests.factories import FAKE_COMPLETION

AUTH = {"Authorization": "Bearer testkey"}


def p(inp, out, **extra):
    return {"input_price": inp, "output_price": out, **extra}


@pytest.fixture
def pricing_dir(tmp_path, monkeypatch):
    d = tmp_path / "pricing"
    d.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", d)
    return d


def write_grid(d, month, grid):
    path = d / f"{month}.json"
    path.write_text(json.dumps({"month": month, **grid}))
    return path


GRID = {
    "models": {"gpt-4o": p(2.5, 10), "claude-sonnet-4-6": p(3, 15)},
    "models_by_provider": {
        "openrouter": {"openai/gpt-4o": p(3.0, 12)},
        "my-provider": {"gpt-4o": p(1, 4)},
    },
}


# -- resolution ------------------------------------------------------------------


def test_two_prices_for_one_model():
    vendor = billing.resolve_model_price("openai/gpt-4o", GRID)
    via_openrouter = billing.resolve_model_price(
        "openai/gpt-4o",
        GRID,
        provider={"id": "or-1", "preset_key": "catalog-openrouter"},
    )
    assert vendor == p(2.5, 10)
    assert via_openrouter == p(3.0, 12)


def test_provider_id_space_and_preset_order():
    assert billing.resolve_model_price("gpt-4o", GRID, provider="my-provider") == p(
        1, 4
    )
    spaces = billing.provider_spaces({"id": "x", "preset_key": "catalog-openrouter"})
    assert spaces == ["catalog-openrouter", "openrouter", "x"]


def test_suffix_match_never_crosses_spaces():
    # `vendor/claude-sonnet-4-6` would suffix-match nothing in the openrouter
    # space; it must fall through to the vendor price, not to another space.
    price = billing.resolve_model_price(
        "anthropic/claude-sonnet-4-6",
        GRID,
        provider={"preset_key": "catalog-openrouter"},
    )
    assert price == p(3, 15)


def test_model_id_with_at_sign():
    grid = {
        "models": {},
        "models_by_provider": {"deepinfra": {"org/model@v2": p(0.5, 1)}},
    }
    assert billing.resolve_model_price("org/model@v2", grid, provider="deepinfra") == p(
        0.5, 1
    )
    assert billing.resolve_model_price("org/model@v2", grid) != p(0.5, 1)


def test_resolution_matches_previous_semantics():
    grid = {"models": {"claude-sonnet-4-6": p(3, 15), "claude-opus-4-1": p(15, 75)}}
    assert billing.resolve_model_price(
        "openclaw/anthropic--claude-sonnet-4-6", grid
    ) == p(3, 15)
    assert billing.resolve_model_price("claude-opus-4-1-20250805", grid) == p(15, 75)
    assert billing.resolve_model_price("claude-sonnet-4.6", grid) == p(3, 15)


def test_ten_thousand_keys_lookup_stays_fast(pricing_dir):
    month = billing.current_pricing_month()
    models = {f"vendor-{i}/model-{i}-large": p(1, 2) for i in range(10_000)}
    models["claude-sonnet-4-6"] = p(3, 15)
    write_grid(pricing_dir, month, {"models": models})
    billing.resolve_price(
        "openclaw/anthropic--claude-sonnet-4-6", month
    )  # warm the cache
    t0 = time.perf_counter()
    for _ in range(100):
        match = billing.resolve_price("openclaw/anthropic--claude-sonnet-4-6", month)
    per_call = (time.perf_counter() - t0) / 100
    assert match.entry == p(3, 15)
    assert per_call < 0.005, f"{per_call * 1000:.3f} ms per lookup"


def test_cache_is_invalidated_by_mtime(pricing_dir):
    month = billing.current_pricing_month()
    path = write_grid(pricing_dir, month, {"models": {"m": p(1, 2)}})
    assert billing.resolve_price("m", month).entry == p(1, 2)
    write_grid(pricing_dir, month, {"models": {"m": p(5, 6)}})
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert billing.resolve_price("m", month).entry == p(5, 6)


def test_negative_cache_clears_when_grid_changes(pricing_dir):
    month = billing.current_pricing_month()
    path = write_grid(pricing_dir, month, {"models": {}})
    assert billing.resolve_price("zz-unknown-model", month, max_months_back=1) is None
    write_grid(pricing_dir, month, {"models": {"zz-unknown-model": p(1, 1)}})
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert billing.resolve_price(
        "zz-unknown-model", month, max_months_back=1
    ).entry == p(1, 1)


def test_price_match_reports_key_and_source(pricing_dir):
    month = billing.current_pricing_month()
    write_grid(
        pricing_dir,
        month,
        {
            "models": {"gpt-4o": p(2.5, 10, source="models_dev")},
            "models_by_provider": {
                "openrouter": {"openai/gpt-4o": p(3, 12, source="provider_native")}
            },
        },
    )
    match = billing.resolve_price(
        "openai/gpt-4o", month, provider={"preset_key": "catalog-openrouter"}
    )
    assert match.price_key == "openrouter|openai/gpt-4o"
    assert match.source == "provider_native"
    assert billing.resolve_price("openai/gpt-4o", month).price_key == "gpt-4o"


# -- billing uses the provider that served the call ---------------------------------


@pytest.mark.asyncio
async def test_forward_records_the_fallback_provider(_providers_file):
    from plugboard.router import forward, served_by

    with aioresponses() as mock:
        mock.post("http://fake-llm.test/v1/chat/completions", status=400, body="bad")
        mock.post("http://other-llm.test/v1/chat/completions", payload=FAKE_COMPLETION)
        # No provider prefix: forward() may fall back to the next provider.
        await forward({"model": "some-model", "messages": []})
    provider, _model = served_by()
    assert provider["id"] == "other-provider"


@pytest.mark.asyncio
async def test_billing_target_prefers_served_provider(_providers_file, monkeypatch):
    from plugboard import app as app_mod

    served = ({"id": "other-provider"}, "other:model-v2")
    monkeypatch.setattr(app_mod, "_served_by", lambda: served)
    assert app_mod._billing_target({"model": "test-provider:fake:model-v1"}) == served
    monkeypatch.setattr(app_mod, "_served_by", lambda: None)
    provider, model = app_mod._billing_target({"model": "test-provider:fake:model-v1"})
    assert provider["id"] == "test-provider" and model == "fake:model-v1"


def test_usage_stores_price_key_and_migrates_old_db(tmp_path, monkeypatch):
    import sqlite3

    from plugboard import usage

    db = tmp_path / "usage.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE usage_entries (id TEXT PRIMARY KEY, ts TEXT NOT NULL, month TEXT NOT NULL,
        client_id TEXT NOT NULL, key_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
        provider_id TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
        total_tokens INTEGER NOT NULL, pricing_month TEXT NOT NULL, cost_usd REAL NOT NULL)"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(usage, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "_DB_PATH", db)
    usage.append_usage(
        "c",
        "openai/gpt-4o",
        "or-1",
        10,
        5,
        "2026_10",
        0.1,
        price_key="openrouter|openai/gpt-4o",
        price_source="provider_native",
    )
    [row] = usage.get_usage(billing.current_pricing_month())
    assert row["price_key"] == "openrouter|openai/gpt-4o"
    assert row["price_source"] == "provider_native"


# -- grid writer ------------------------------------------------------------------


@pytest.fixture
def refresh(pricing_dir, monkeypatch):
    monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", "")
    monkeypatch.setattr(pricing_scraper, "_openrouter_configured", lambda: False)

    async def _none():
        return {}

    monkeypatch.setattr(pricing_scraper, "_fetch_litellm_pricing", _none)
    for name in (
        "PLUGBOARD_PRICING_SOURCES",
        "PLUGBOARD_PRICE_DRIFT_ACTION",
        "PLUGBOARD_PRICE_DRIFT_PCT",
    ):
        monkeypatch.delenv(name, raising=False)
    return billing.current_pricing_month()


@pytest.mark.asyncio
async def test_reseller_price_kept_only_when_it_differs(refresh, monkeypatch):
    month = refresh
    vendor = {"gpt-4o": p(2.5, 10)}
    resellers = {
        "openrouter": {"openai/gpt-4o": p(2.5, 10), "openai/gpt-4o-mini": p(0.2, 0.8)},
        "deepinfra": {"gpt-4o": p(2.0, 8)},
    }
    monkeypatch.setattr(
        pricing_scraper, "_models_dev_pricing", lambda: (vendor, resellers)
    )
    result = await pricing_scraper.update_current_pricing()
    grid = billing.load_pricing(month)
    assert grid["models"]["gpt-4o"] == p(2.5, 10, source="models_dev")
    assert grid["models_by_provider"] == {
        "openrouter": {"openai/gpt-4o-mini": p(0.2, 0.8, source="models_dev")},
        "deepinfra": {"gpt-4o": p(2.0, 8, source="models_dev")},
    }
    assert result["provider_prices"] == 2


@pytest.mark.asyncio
async def test_reseller_drift_is_queued_with_its_space(refresh, monkeypatch):
    month = refresh
    write_grid(
        billing._PRICING_DIR,
        month,
        {
            "models": {"gpt-4o": p(2.5, 10, source="models_dev")},
            "models_by_provider": {
                "deepinfra": {"gpt-4o": p(2.0, 8, source="models_dev")}
            },
        },
    )
    monkeypatch.setattr(
        pricing_scraper,
        "_models_dev_pricing",
        lambda: ({"gpt-4o": p(2.5, 10)}, {"deepinfra": {"gpt-4o": p(4.0, 16)}}),
    )
    await pricing_scraper.update_current_pricing()
    [item] = pricing_suggestions.list_suggestions()
    assert item["space"] == "deepinfra" and item["price_key"] == "deepinfra|gpt-4o"
    pricing_suggestions.apply(item["id"])
    assert (
        billing.load_pricing(month)["models_by_provider"]["deepinfra"]["gpt-4o"][
            "input_price"
        ]
        == 4.0
    )


@pytest.mark.asyncio
async def test_patch_and_revert_a_provider_price(client, refresh, monkeypatch):
    month = refresh
    monkeypatch.setattr(
        pricing_scraper,
        "_models_dev_pricing",
        lambda: ({"gpt-4o": p(2.5, 10)}, {"deepinfra": {"gpt-4o": p(2.0, 8)}}),
    )
    await pricing_scraper.update_current_pricing()
    resp = await client.patch(
        f"/admin/pricing/{month}",
        headers=AUTH,
        json={"models_by_provider": {"deepinfra": {"gpt-4o": p(9, 9)}}},
    )
    assert resp.status_code == 200, resp.text
    assert (
        resp.json()["models_by_provider"]["deepinfra"]["gpt-4o"]["source"] == "manual"
    )
    reverted = await client.post(
        f"/admin/pricing/{month}/revert",
        headers=AUTH,
        json={"model": "gpt-4o", "provider": "deepinfra"},
    )
    assert reverted.status_code == 200, reverted.text
    assert reverted.json()["entry"] == p(2.0, 8, source="models_dev")


@pytest.mark.asyncio
async def test_put_without_provider_prices_keeps_them(client, pricing_dir):
    month = billing.current_pricing_month()
    write_grid(
        pricing_dir,
        month,
        {"models": {"a": p(1, 2)}, "models_by_provider": {"x": {"a": p(3, 4)}}},
    )
    resp = await client.put(
        f"/admin/pricing/{month}", headers=AUTH, json={"models": {"a": p(1, 2)}}
    )
    assert resp.json()["models_by_provider"] == {"x": {"a": p(3, 4)}}


# -- reconciliation -----------------------------------------------------------------


def test_reconcile_reports_differences_per_client_and_provider(pricing_dir):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "billing_reconcile",
        Path(__file__).resolve().parent.parent / "scripts" / "billing_reconcile.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    month = "2026_09"
    write_grid(
        pricing_dir,
        month,
        {
            "models": {"gpt-4o": p(2.0, 8.0)},
            "models_by_provider": {"openrouter": {"openai/gpt-4o": p(3.0, 12.0)}},
        },
    )
    entries = [
        {
            "client_id": "acme",
            "provider_id": "or-1",
            "model": "openai/gpt-4o",
            "month": month,
            "pricing_month": month,
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
            "cost_usd": 2.0,
        },
        {
            "client_id": "acme",
            "provider_id": "oa-1",
            "model": "gpt-4o",
            "month": month,
            "pricing_month": month,
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
            "cost_usd": 2.0,
        },
    ]
    providers = {
        "or-1": {"id": "or-1", "preset_key": "catalog-openrouter"},
        "oa-1": {"id": "oa-1"},
    }
    rows = {r["provider_id"]: r for r in mod.reconcile(entries, providers)}
    assert rows["or-1"]["before_usd"] == 2.0 and rows["or-1"]["after_usd"] == 3.0
    assert rows["or-1"]["diff_pct"] == 50.0
    assert rows["oa-1"]["diff_pct"] == 0.0
