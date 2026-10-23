"""Regression tests for the pre-landing review of the auto-update stack."""

from __future__ import annotations

import asyncio
import json

import pytest
from aioresponses import aioresponses

from plugboard import billing, config, llm_pricing, pricing_suggestions, usage
from plugboard import model_availability as ma
from plugboard.endpoints import openai_url
from tests.conftest import _setup_providers

AUTH = {"Authorization": "Bearer testkey"}


def p(inp, out, **extra):
    return {"input_price": inp, "output_price": out, **extra}


# -- admin endpoints: error paths -----------------------------------------------------


@pytest.mark.asyncio
async def test_direct_provider_test_refuses_a_disallowed_domain(client, monkeypatch):
    from plugboard import app as app_mod

    def deny(url):
        raise config.ProviderDomainNotAllowed("evil.example is not allowed")

    called = []

    async def spy(*args):
        called.append(args)
        return {}

    monkeypatch.setattr(config, "check_provider_domain_allowed", deny)
    monkeypatch.setattr(app_mod, "_test_openai_endpoint", spy)
    resp = await client.post(
        "/admin/direct-providers/test",
        headers=AUTH,
        json={"base_url": "https://evil.example/v1", "api_key": "k"},
    )
    assert resp.status_code == 422 and called == []
    unknown = await client.post(
        "/admin/direct-providers/test", headers=AUTH, json={"preset_key": "nope"}
    )
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_discover_errors_keep_the_model_list(
    client, _providers_file, monkeypatch
):
    missing = await client.post("/providers/nope/discover", headers=AUTH)
    assert missing.status_code == 404
    config.update_provider("test-provider", {"models": [{"id": "keep-me"}]})

    async def nothing(provider):
        return []

    monkeypatch.setattr("plugboard.app.discover_models", nothing)
    resp = await client.post("/providers/test-provider/discover", headers=AUTH)
    assert resp.status_code == 502
    assert [m["id"] for m in config.get_provider("test-provider")["models"]] == [
        "keep-me"
    ]


@pytest.mark.asyncio
async def test_llm_run_refused_when_off(client, monkeypatch):
    monkeypatch.delenv("PLUGBOARD_LLM_PRICING", raising=False)
    called = []

    async def spy(*a, **k):
        called.append(a)

    monkeypatch.setattr(llm_pricing, "run", spy)
    resp = await client.post("/admin/pricing/llm/run", headers=AUTH)
    assert resp.status_code == 409 and called == []


@pytest.mark.asyncio
async def test_auto_update_run_starts_both_and_survives_a_failure(client):
    from plugboard.app import _background_tasks, app

    calls = []

    class Fake:
        def __init__(self, name, fail=False):
            self.name, self.fail, self.running_since = name, fail, None
            self.status_dict = {}

        async def run_now(self, kind):
            calls.append((self.name, kind))
            if self.fail:
                raise RuntimeError("busy")

    saved = (
        getattr(app.state, "pricing_scheduler", None),
        getattr(app.state, "model_availability_scheduler", None),
    )
    app.state.pricing_scheduler, app.state.model_availability_scheduler = (
        Fake("pricing", fail=True),
        Fake("models"),
    )
    try:
        resp = await client.post("/admin/auto-update/run", headers=AUTH)
        assert resp.status_code == 202 and resp.json() == {"started": True}
        await asyncio.gather(*list(_background_tasks), return_exceptions=True)
    finally:
        app.state.pricing_scheduler, app.state.model_availability_scheduler = saved
    assert calls == [("pricing", "manual"), ("models", "manual")]


@pytest.mark.asyncio
async def test_put_and_patch_refuse_an_unreadable_grid(client, tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "_PRICING_DIR", tmp_path)
    (tmp_path / "2026_05.json").write_text("{not json")
    for method in ("PUT", "PATCH"):
        resp = await client.request(
            method,
            "/admin/pricing/2026_05",
            headers=AUTH,
            json={"models": {"a": p(1, 2)}},
        )
        assert resp.status_code == 503, method
    assert (tmp_path / "2026_05.json").read_text() == "{not json"
    bad = await client.put("/admin/pricing/2026-5", headers=AUTH, json={"models": {}})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_client_usage_hides_the_price_key(
    client, _api_key, monkeypatch, tmp_path
):
    monkeypatch.setattr(usage, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    from plugboard import app as app_mod

    async def as_client():
        return "acme"

    app_mod.app.dependency_overrides[app_mod._resolve_client] = as_client
    try:
        usage.append_usage(
            "acme",
            "m",
            "p",
            1,
            1,
            billing.current_pricing_month(),
            0.1,
            price_key="p|m",
            price_source="manual",
        )
        resp = await client.get(
            "/usage/me", headers={"Authorization": "Bearer testkey"}
        )
    finally:
        app_mod.app.dependency_overrides.clear()
    [entry] = resp.json()["entries"]
    assert "price_key" not in entry and "price_source" not in entry


# -- pricing internals ---------------------------------------------------------------


def test_index_rebuilds_after_in_place_change():
    table = {"a-model": p(1, 1)}
    assert billing.resolve_model_price("a-model", {"models": table}) == p(1, 1)
    del table["a-model"]
    table["b-model"] = p(2, 2)  # same length, different keys
    assert billing.resolve_model_price("b-model", {"models": table}) == p(2, 2)


def test_reseller_added_by_hand_reads_its_space():
    grid = {
        "models": {"gpt-4o": p(2.5, 10)},
        "models_by_provider": {"openrouter": {"openai/gpt-4o": p(3, 12)}},
    }
    provider = {"id": "or-main", "base_url": "https://openrouter.ai/api/v1"}
    assert billing.resolve_model_price("openai/gpt-4o", grid, provider=provider) == p(
        3, 12
    )
    assert (
        billing.price_space(provider) == "or-main"
    )  # its own prices are written under its id


@pytest.mark.parametrize(
    "address", ["64:ff9b::a00:5", "2002:c0a8:0101::1", "64:ff9b:1::1"]
)
def test_ipv6_with_an_embedded_private_ipv4_is_refused(address):
    assert llm_pricing.ip_is_public(address) is False


def test_azure_style_base_keeps_the_v1_prefix():
    assert (
        openai_url("https://x.openai.azure.com/openai", "models")
        == "https://x.openai.azure.com/openai/v1/models"
    )


def test_apply_and_reject_cannot_both_win(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "_PRICING_DIR", tmp_path)
    month = billing.current_pricing_month()
    (tmp_path / f"{month}.json").write_text(
        json.dumps({"models": {"m": p(2, 8, source="models_dev")}})
    )
    [sid] = pricing_suggestions.record_drifts(
        month,
        [
            {
                "model": "m",
                "new": p(4, 16),
                "old": p(2, 8),
                "pct": 100.0,
                "source": "models_dev",
            },
        ],
    )
    pricing_suggestions.reject(sid)
    with pytest.raises(pricing_suggestions.SuggestionConflict):
        pricing_suggestions.apply(sid)
    assert billing.load_pricing(month)["models"]["m"]["input_price"] == 2


# -- llm pricing: failures cost nothing and are recorded -------------------------------


@pytest.fixture
def llm_env(tmp_path, monkeypatch):
    pricing_dir = tmp_path / "pricing"
    pricing_dir.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing_dir)
    monkeypatch.setattr(usage, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    base = tmp_path / "llm"
    base.mkdir()
    _setup_providers(
        base,
        monkeypatch,
        [
            {
                "id": "pricer",
                "is_active": True,
                "base_url": "https://llm.example/v1",
                "default_model": "reader",
            },
            {
                "id": "acme",
                "is_active": True,
                "base_url": "https://api.acme.example/v1",
                "preset_key": "catalog-acme",
            },
        ],
    )
    month = billing.current_pricing_month()
    (pricing_dir / f"{month}.json").write_text(
        json.dumps({"models": {"reader": p(1.0, 2.0)}})
    )
    monkeypatch.setenv("PLUGBOARD_LLM_PRICING", "1")
    monkeypatch.setenv(
        "PLUGBOARD_LLM_PRICING_URLS", "acme=https://acme.example/pricing"
    )
    return month


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fetch", "call"])
async def test_llm_pass_failures_spend_nothing(llm_env, monkeypatch, failure):
    async def fetch(url):
        if failure == "fetch":
            raise llm_pricing.UnsafeURL("acme.example resolves to a non-public address")
        return "alpha-large $2 / $8"

    async def forward(body):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(llm_pricing, "fetch_page", fetch)
    monkeypatch.setattr("plugboard.router.forward", forward)
    report = await llm_pricing.run(["acme:alpha-large"])
    reason = report.reasons["acme:alpha-large"]
    assert ("not read" in reason) if failure == "fetch" else ("call failed" in reason)
    assert llm_pricing.read_state()["spent_usd"] == 0.0
    assert usage.get_usage(llm_env) == []
    assert pricing_suggestions.list_suggestions() == []


@pytest.mark.asyncio
async def test_llm_last_run_is_recorded_before_the_first_call(llm_env, monkeypatch):
    async def fetch(url):
        return "alpha-large $2 / $8"

    async def slow_forward(body):
        state = llm_pricing.read_state()
        assert state.get("last_run")  # already on disk if the pass is cut short now
        raise asyncio.CancelledError

    monkeypatch.setattr(llm_pricing, "fetch_page", fetch)
    monkeypatch.setattr("plugboard.router.forward", slow_forward)
    with pytest.raises(asyncio.CancelledError):
        await llm_pricing.run(["acme:alpha-large"])
    again = await llm_pricing.run(["acme:alpha-large"])
    assert "24 hours" in again.skipped


@pytest.mark.asyncio
async def test_only_one_llm_pass_at_a_time(llm_env):
    async with llm_pricing._pass_lock:
        report = await llm_pricing.run(["acme:alpha-large"])
    assert report.skipped == "another pass is running"


# -- model lists ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reseller_lists_are_not_family_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr("plugboard.usage.get_all_used_models", lambda: set())
    base = "https://api.reseller.test/v1"
    old = [{"id": "vendor/model-2"}, {"id": "vendor/model-3"}]
    _setup_providers(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "reseller",
                "is_active": True,
                "base_url": base,
                "auto_discover": True,
                "models": old,
            },
        ],
    )
    with aioresponses() as mock:
        mock.get(
            f"{base}/models",
            payload={"data": [{"id": "vendor/model-2"}, {"id": "vendor/model-3"}]},
        )
        await ma.refresh_all_model_catalogs()
    assert [m["id"] for m in config.get_provider("reseller")["models"]] == [
        "vendor/model-2",
        "vendor/model-3",
    ]


@pytest.mark.asyncio
async def test_failed_cli_probe_is_retried_next_pass(tmp_path, monkeypatch):
    monkeypatch.setattr("plugboard.usage.get_all_used_models", lambda: set())
    _setup_providers(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "cli",
                "is_active": True,
                "auth_source": "subprocess:gemini",
                "models": [{"id": "x"}],
            },
        ],
    )
    calls = []

    async def failing(provider, **kwargs):
        calls.append(1)
        return {"provider": "cli", "models": [], "error": "not logged in"}

    monkeypatch.setattr(ma, "refresh_subprocess_provider_models", failing)
    await ma.refresh_all_model_catalogs()
    await ma.refresh_all_model_catalogs()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_scheduler_skips_a_tick_while_a_run_is_in_progress():
    from plugboard.pricing_scheduler import PricingScheduler

    started = asyncio.Event()
    release = asyncio.Event()
    runs = []

    async def update():
        runs.append(1)
        started.set()
        await release.wait()
        return {"sources": ["litellm"]}

    sched = PricingScheduler(update_fn=update, providers_fn=lambda: [{"id": "p"}])
    first = asyncio.create_task(sched.run_now())
    await started.wait()
    await sched._invoke_update("daily")  # skipped, not run alongside
    with pytest.raises(RuntimeError):
        await sched.run_now()
    release.set()
    await first
    assert runs == [1] and sched.running_since is None


@pytest.mark.asyncio
async def test_clear_text_external_source_is_ignored(monkeypatch, caplog):
    from plugboard import pricing_scraper

    monkeypatch.setattr(
        pricing_scraper, "_PRICING_SOURCE_URL", "http://prices.example/grid.json"
    )
    with aioresponses():  # any request would fail: none is registered
        assert await pricing_scraper._fetch_external_source() == {}
    assert "must use https" in caplog.text
    assert pricing_scraper._secure_source("http://localhost:9000/grid.json")


@pytest.mark.asyncio
async def test_fallback_discovery_never_removes(client, _providers_file, monkeypatch):
    from plugboard import router

    config.update_provider(
        "test-provider", {"models": [{"id": "real-1"}, {"id": "real-2"}]}
    )

    async def guessed(provider):
        router._discovery_trusted.set(False)  # e.g. a model listed its own guesses
        return [{"id": "real-1"}, {"id": "guess"}]

    monkeypatch.setattr("plugboard.app.discover_models", guessed)
    resp = await client.post("/providers/test-provider/discover", headers=AUTH)
    body = resp.json()
    assert body["removed"] == [] and body["removals_allowed"] is False
    assert sorted(m["id"] for m in config.get_provider("test-provider")["models"]) == [
        "guess",
        "real-1",
        "real-2",
    ]


def test_superseded_or_applied_proposal_comes_back(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "_PRICING_DIR", tmp_path)
    month = billing.current_pricing_month()

    def drift(inp):
        return {
            "model": "m",
            "new": p(inp, inp * 4),
            "old": p(2, 8),
            "pct": 50.0,
            "source": "models_dev",
        }

    [a] = pricing_suggestions.record_drifts(month, [drift(3)])
    [b] = pricing_suggestions.record_drifts(month, [drift(4)])
    assert pricing_suggestions.get(a)["status"] == "superseded"
    assert pricing_suggestions.record_drifts(month, [drift(3)]) == [
        a
    ]  # back for review
    assert pricing_suggestions.get(a)["status"] == "pending"
    assert pricing_suggestions.get(b)["status"] == "superseded"


def test_own_price_wins_over_the_shared_reseller_space():
    grid = {
        "models": {},
        "models_by_provider": {"openrouter": {"m": p(3, 3)}, "my-or": {"m": p(1, 1)}},
    }
    provider = {"id": "my-or", "base_url": "https://openrouter.ai/api/v1"}
    assert billing.resolve_model_price("m", grid, provider=provider) == p(1, 1)


@pytest.mark.asyncio
async def test_scheduler_rebuilds_a_corrupt_current_grid(tmp_path, monkeypatch):
    from plugboard import pricing_scraper

    monkeypatch.setattr(billing, "_PRICING_DIR", tmp_path)
    monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", "")
    monkeypatch.setattr(pricing_scraper, "_openrouter_configured", lambda: False)
    monkeypatch.setattr(
        pricing_scraper, "_models_dev_pricing", lambda: ({"m": p(1, 2)}, {})
    )

    async def none():
        return {}

    monkeypatch.setattr(pricing_scraper, "_fetch_litellm_pricing", none)
    month = billing.current_pricing_month()
    (tmp_path / f"{month}.json").write_text("{broken")
    await pricing_scraper.update_current_pricing()
    assert billing.load_pricing(month)["models"]["m"]["input_price"] == 1
    assert any(f.name.startswith(f"{month}.corrupt-") for f in tmp_path.iterdir())


def test_small_steps_cannot_add_up_past_the_threshold():
    from plugboard.pricing_merge import merge_sources

    order = ["models_dev"]
    reviewed = {}
    grid = {"m": p(10, 40, source="models_dev")}
    res = merge_sources(
        grid, {"models_dev": {"m": p(11.0, 44.0)}}, order, reviewed=reviewed
    )
    assert not res.drifts  # +10%: applied silently
    grid = res.models
    res = merge_sources(
        grid, {"models_dev": {"m": p(12.5, 50.0)}}, order, reviewed=reviewed
    )
    # +13.6% from the current price, but +25% from the last reviewed 10/40: held.
    assert res.drifts and res.drifts[0]["pct"] == 25.0
    assert res.models["m"]["input_price"] == 11.0


@pytest.mark.asyncio
async def test_catalog_test_connects_only_to_the_checked_address(monkeypatch):
    from plugboard import app as app_mod

    seen = {}

    class Resolver:
        def __init__(self, host, results):
            seen["host"], seen["results"] = host, results

    monkeypatch.setattr("plugboard.llm_pricing._PinnedResolver", Resolver)

    class Boom(Exception):
        pass

    def connector(**kw):
        seen["connector"] = kw
        raise Boom

    monkeypatch.setattr("aiohttp.TCPConnector", connector)
    pinned = [
        {
            "hostname": "api.cat.example",
            "host": "93.184.216.34",
            "port": 443,
            "family": 2,
            "proto": 6,
            "flags": 4,
        }
    ]
    with pytest.raises(Boom):
        await app_mod._test_openai_endpoint(
            "https://api.cat.example/v1", "k", False, pinned=pinned
        )
    assert seen["host"] == "api.cat.example" and seen["results"] == pinned
    assert seen["connector"]["use_dns_cache"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("caller, sees_price_key", [("acme", False), ("admin", True)])
async def test_completion_billing_shows_the_price_key_to_admins_only(
    client, caller, sees_price_key
):
    """`billing.price_key` reveals provider routing, like X-Price-Key and the
    entries of /usage/me: admins see it, client keys do not."""
    from aioresponses import aioresponses

    from plugboard import app as app_mod
    from tests.factories import FAKE_COMPLETION

    async def as_caller():
        return caller

    app_mod.app.dependency_overrides[app_mod._resolve_client] = as_caller
    try:
        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions", payload=FAKE_COMPLETION
            )
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer testkey"},
                json={
                    "model": "test-provider:fake:model-v1",
                    "messages": [{"role": "user", "content": "hi"}],
                    "cache": False,
                },
            )
    finally:
        app_mod.app.dependency_overrides.clear()
    assert resp.status_code == 200
    billing_body = resp.json()["billing"]
    assert billing_body["cost_usd"] >= 0
    assert ("price_key" in billing_body) is sees_price_key
    assert ("x-price-key" in resp.headers) is (
        sees_price_key and bool(billing_body.get("price_key"))
    )
