"""MR-D: prices read from provider pages by a model (off by default)."""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pytest
from aioresponses import aioresponses

from plugboard import billing, llm_pricing, pricing_suggestions, usage
from tests.conftest import _setup_providers

PAGES = Path(__file__).parent / "fixtures" / "pricing_pages"
AUTH = {"Authorization": "Bearer testkey"}


def p(inp, out, **extra):
    return {"input_price": inp, "output_price": out, **extra}


# -- address checks --------------------------------------------------------------------


@pytest.mark.parametrize("address,public", [
    ("8.8.8.8", True),
    ("2606:4700:4700::1111", True),
    ("127.0.0.1", False),
    ("::1", False),
    ("0.0.0.0", False),
    ("10.1.2.3", False),
    ("192.168.1.10", False),
    ("100.64.0.7", False),          # carrier-grade NAT
    ("169.254.169.254", False),     # cloud metadata
    ("fe80::1", False),
    ("::ffff:10.0.0.1", False),     # IPv4-mapped private
    ("::ffff:127.0.0.1", False),
    ("::ffff:8.8.8.8", True),
])
def test_ip_is_public(address, public):
    assert llm_pricing.ip_is_public(address) is public


@pytest.mark.asyncio
async def test_a_name_with_one_private_answer_is_refused(monkeypatch):
    """DNS rebinding: a public and a private answer for one name -> refused."""

    async def fake_getaddrinfo(host, port, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port)),
        ]

    import asyncio

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(llm_pricing.UnsafeURL, match="non-public"):
        await llm_pricing._resolve_public("prices.example", 443)


@pytest.mark.asyncio
async def test_connection_is_pinned_to_the_checked_address():
    pinned = llm_pricing._PinnedResolver("prices.example", [
        {"hostname": "prices.example", "host": "93.184.216.34", "port": 443, "family": socket.AF_INET,
         "proto": 6, "flags": socket.AI_NUMERICHOST},
    ])
    [answer] = await pinned.resolve("prices.example", 443)
    assert answer["host"] == "93.184.216.34"
    with pytest.raises(OSError):
        await pinned.resolve("other.example", 443)


# -- page fetch ------------------------------------------------------------------------


@pytest.fixture
def public_dns(monkeypatch):
    async def fake(host, port):
        return [{"hostname": host, "host": "93.184.216.34", "port": port, "family": socket.AF_INET,
                 "proto": 6, "flags": socket.AI_NUMERICHOST}]

    monkeypatch.setattr(llm_pricing, "_resolve_public", fake)


@pytest.mark.asyncio
async def test_only_https_and_no_credentials():
    with pytest.raises(llm_pricing.UnsafeURL, match="https"):
        await llm_pricing.fetch_page("http://prices.example/pricing")
    with pytest.raises(llm_pricing.UnsafeURL, match="credentials"):
        await llm_pricing.fetch_page("https://user:pw@prices.example/pricing")


@pytest.mark.asyncio
async def test_redirect_to_another_host_is_refused(public_dns):
    with aioresponses() as mock:
        mock.get("https://prices.example/pricing", status=302, headers={"Location": "https://evil.example/x"})
        with pytest.raises(llm_pricing.UnsafeURL, match="not the configured host"):
            await llm_pricing.fetch_page("https://prices.example/pricing")


@pytest.mark.asyncio
async def test_redirect_on_the_same_host_is_followed(public_dns):
    with aioresponses() as mock:
        mock.get("https://prices.example/pricing", status=301, headers={"Location": "/v2/pricing"})
        mock.get("https://prices.example/v2/pricing", body=(PAGES / "table.html").read_text(),
                 content_type="text/html")
        text = await llm_pricing.fetch_page("https://prices.example/pricing")
    assert "alpha-large" in text


@pytest.mark.asyncio
async def test_page_too_large_is_refused(public_dns, monkeypatch):
    monkeypatch.setattr(llm_pricing, "MAX_PAGE_BYTES", 1024)
    with aioresponses() as mock:
        mock.get("https://prices.example/pricing", body=b"x" * 4096)
        with pytest.raises(llm_pricing.UnsafeURL, match="larger"):
            await llm_pricing.fetch_page("https://prices.example/pricing")


@pytest.mark.asyncio
async def test_environment_proxy_is_ignored(public_dns, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    seen = {}
    real = aiohttp.ClientSession

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(llm_pricing.aiohttp, "ClientSession", spy)
    with aioresponses() as mock:
        mock.get("https://prices.example/pricing", body="<p>ok</p>")
        await llm_pricing.fetch_page("https://prices.example/pricing")
    assert seen["trust_env"] is False


# -- page text (eval on fixture pages) ----------------------------------------------


@pytest.mark.parametrize("page,expected", [
    ("table.html", ["alpha-large", "$2.00", "$8.00", "alpha-small", "$0.20"]),
    ("cards.html", ["bravo-pro", "$1.25", "$5.00", "bravo-mini", "$0.10"]),
    ("per_thousand.html", ["per 1K tokens", "charlie-7b", "$0.0002", "charlie-70b"]),
    ("injection.html", ["delta-chat", "$3", "$15"]),
    ("long_head.html", ["echo-1", "$0.50", "$1.50"]),
])
def test_page_text_keeps_models_and_prices(page, expected):
    text = llm_pricing.html_to_text((PAGES / page).read_text())
    for fragment in expected:
        assert fragment in text
    assert "tracking" not in text and "color:red" not in text


def test_long_page_is_truncated_to_the_context_share():
    page = (PAGES / "long_head.html").read_text() + "<p>" + "Lorem ipsum dolor. " * 50_000 + "</p>"
    text = llm_pricing.truncate_for(llm_pricing.html_to_text(page), 8_000)
    assert len(text) == int(8_000 * 0.6 * 3.5)
    assert text.startswith("echo-1")
    assert llm_pricing.estimate_tokens("x" * 35) == 10


# -- answers ---------------------------------------------------------------------------


def test_parse_answer_keeps_requested_valid_prices():
    answer = '```json\n{"a": {"input_price": 1, "output_price": 4}, "b": {"input_price": 0, "output_price": 1},' \
             ' "c": {"input_price": 2000, "output_price": 1}, "zz": {"input_price": 1, "output_price": 1}}\n```'
    prices, rejected = llm_pricing.parse_answer(answer, ["a", "b", "c"])
    assert prices == {"a": p(1.0, 4.0)}
    assert sorted(rejected) == ["b", "c"]


@pytest.mark.parametrize("answer", ["I cannot browse pages.", "", "[1, 2]", "{'a': 1}"])
def test_refusals_and_invalid_json_are_rejected(answer):
    with pytest.raises(llm_pricing.LLMPriceParseError):
        llm_pricing.parse_answer(answer, ["a"])


def test_pages_setting(monkeypatch, caplog):
    monkeypatch.setenv("PLUGBOARD_LLM_PRICING_URLS", "acme=https://acme.example/pricing; bad=http://x ;junk")
    pages = llm_pricing.configured_pages()
    assert pages["acme"] == "https://acme.example/pricing" and pages["groq"].startswith("https://")
    assert "bad" not in pages and "ignoring" in caplog.text


# -- a pass --------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    pricing_dir = tmp_path / "pricing"
    pricing_dir.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing_dir)
    monkeypatch.setattr(usage, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    base = tmp_path / "llm"
    base.mkdir()
    _setup_providers(base, monkeypatch, [
        {"id": "pricer", "is_active": True, "base_url": "https://llm.example/v1", "default_model": "reader"},
        {"id": "acme", "is_active": True, "base_url": "https://api.acme.example/v1", "preset_key": "catalog-acme"},
    ])
    month = billing.current_pricing_month()
    (pricing_dir / f"{month}.json").write_text(json.dumps({"models": {"reader": p(1.0, 2.0)}}))
    monkeypatch.setenv("PLUGBOARD_LLM_PRICING", "1")
    monkeypatch.setenv("PLUGBOARD_LLM_PRICING_URLS", "acme=https://acme.example/pricing")
    for name in ("PLUGBOARD_LLM_PRICING_MAX_USD", "PLUGBOARD_LLM_PRICING_MAX_MODELS",
                 "PLUGBOARD_LLM_PRICING_PROVIDER", "PLUGBOARD_LLM_PRICING_MODEL"):
        monkeypatch.delenv(name, raising=False)

    pages = {}

    async def fake_fetch(url):
        pages["url"] = url
        return llm_pricing.html_to_text((PAGES / "table.html").read_text())

    monkeypatch.setattr(llm_pricing, "fetch_page", fake_fetch)
    calls = []

    def answer_with(content, usage_=None):
        async def fake_forward(body):
            calls.append(body)
            return {"choices": [{"message": {"content": content}}],
                    "usage": usage_ or {"prompt_tokens": 1000, "completion_tokens": 50}}

        monkeypatch.setattr("plugboard.router.forward", fake_forward)

    return {"month": month, "calls": calls, "answer_with": answer_with, "pages": pages, "dir": pricing_dir}


MISSING = ["acme:alpha-large", "acme:alpha-small", "acme:unlisted"]
ANSWER = json.dumps({"alpha-large": p(2, 8), "alpha-small": p(0.2, 0.8)})


@pytest.mark.asyncio
async def test_off_by_default(env, monkeypatch):
    monkeypatch.delenv("PLUGBOARD_LLM_PRICING")
    report = await llm_pricing.run(MISSING)
    assert not report.ran and "PLUGBOARD_LLM_PRICING=1" in report.skipped


@pytest.mark.asyncio
async def test_pass_queues_suggestions_and_bills_internal_client(env):
    env["answer_with"](ANSWER)
    report = await llm_pricing.run(MISSING)
    assert report.ran and report.calls == 1
    assert env["pages"]["url"] == "https://acme.example/pricing"
    [call] = env["calls"]
    assert call["model"] == "pricer:reader" and call["max_tokens"] == 1024
    assert call["messages"][0]["role"] == "system" and "untrusted" in call["messages"][1]["content"]
    assert report.reasons["acme:unlisted"] == "not found on the pricing page"
    items = {s["model"]: s for s in pricing_suggestions.list_suggestions()}
    assert set(items) == {"alpha-large", "alpha-small"}
    assert items["alpha-large"]["type"] == "llm" and items["alpha-large"]["space"] == "acme"
    # Nothing reaches the grid on its own.
    assert "models_by_provider" not in billing.load_pricing(env["month"])
    [row] = usage.get_usage(env["month"])
    assert row["client_id"] == "plugboard-internal"
    assert row["cost_usd"] == pytest.approx(1000 * 1.0 / 1e6 + 50 * 2.0 / 1e6)
    # Applying writes the provider price with source llm.
    pricing_suggestions.apply(items["alpha-large"]["id"])
    entry = billing.load_pricing(env["month"])["models_by_provider"]["acme"]["alpha-large"]
    assert entry["source"] == "llm" and entry["input_price"] == 2


@pytest.mark.asyncio
async def test_once_a_day_and_budget_survive_a_restart(env, monkeypatch):
    env["answer_with"](ANSWER)
    await llm_pricing.run(MISSING)
    second = await llm_pricing.run(MISSING)
    assert not second.ran and "24 hours" in second.skipped
    # The spend is on disk: a new process with a tiny budget stops before calling.
    monkeypatch.setenv("PLUGBOARD_LLM_PRICING_MAX_USD", "0.0011")
    third = await llm_pricing.run(["acme:alpha-medium"], force=True)
    assert "daily budget" in third.reasons["acme:alpha-medium"]
    assert len(env["calls"]) == 1


def test_spend_resets_the_next_day(env):
    llm_pricing.write_state({"day": (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d"),
                             "spent_usd": 9.0})
    assert llm_pricing.read_state()["spent_usd"] == 0.0


@pytest.mark.asyncio
async def test_unknown_price_of_the_pricing_model_means_no_call(env):
    (env["dir"] / f"{env['month']}.json").write_text(json.dumps({"models": {}}))
    env["answer_with"](ANSWER)
    report = await llm_pricing.run(MISSING)
    assert "unknown" in report.skipped and env["calls"] == []


@pytest.mark.asyncio
async def test_refusal_and_out_of_bounds_create_nothing(env):
    env["answer_with"]("Sorry, I can't help with that.")
    report = await llm_pricing.run(MISSING)
    assert "answer rejected" in report.reasons["acme:alpha-large"]
    env["answer_with"](json.dumps({"alpha-large": p(5000, 1), "alpha-small": p(0, 0)}))
    report = await llm_pricing.run(MISSING, force=True)
    assert "price rejected" in report.reasons["acme:alpha-large"]
    assert pricing_suggestions.list_suggestions() == []


@pytest.mark.asyncio
async def test_rejected_price_is_not_asked_again(env):
    env["answer_with"](ANSWER)
    await llm_pricing.run(MISSING)
    for item in pricing_suggestions.list_suggestions():
        pricing_suggestions.reject(item["id"])
    report = await llm_pricing.run(MISSING, force=True)
    assert "rejected earlier" in report.reasons["acme:alpha-large"]
    assert len(env["calls"]) == 2  # only alpha-unlisted was asked again
    assert env["calls"][1]["messages"][0]["content"].count("alpha-large") == 0


@pytest.mark.asyncio
async def test_models_per_pass_cap(env, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_LLM_PRICING_MAX_MODELS", "1")
    env["answer_with"](ANSWER)
    report = await llm_pricing.run(MISSING)
    assert "MAX_MODELS" in report.reasons["acme:alpha-small"]


@pytest.mark.asyncio
async def test_no_page_known(env, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_LLM_PRICING_URLS", "")
    monkeypatch.setattr(llm_pricing, "DEFAULT_PAGES", {})
    env["answer_with"](ANSWER)
    report = await llm_pricing.run(MISSING)
    assert "PLUGBOARD_LLM_PRICING_URLS" in report.reasons["acme:alpha-large"]
    assert env["calls"] == []


@pytest.mark.asyncio
async def test_suggestion_of_a_closed_month_expires(env):
    [sid] = pricing_suggestions.record_llm("2020_01", [{"model": "old", "space": "acme", "new": p(1, 2)}])
    assert pricing_suggestions.list_suggestions() == []  # expired on read
    assert pricing_suggestions.get(sid)["status"] == "expired"
    with pytest.raises(pricing_suggestions.SuggestionConflict):
        pricing_suggestions.apply(sid)
    # Proposed again this month: the same id comes back as pending.
    pricing_suggestions.record_llm(env["month"], [{"model": "old", "space": "acme", "new": p(1, 2)}])
    assert pricing_suggestions.get(sid)["status"] == "pending"
    assert pricing_suggestions.get(sid)["month"] == env["month"]


def test_rejection_survives_the_month_rollover(env):
    [sid] = pricing_suggestions.record_drifts(env["month"], [
        {"model": "m", "new": p(4, 16), "old": p(2, 8), "pct": 100.0, "source": "models_dev"},
    ])
    pricing_suggestions.reject(sid)
    assert pricing_suggestions.record_drifts("2099_01", [
        {"model": "m", "new": p(4, 16), "old": p(2, 8), "pct": 100.0, "source": "models_dev"},
    ]) == []


@pytest.mark.asyncio
async def test_status_and_run_endpoints(client, env):
    status = await client.get("/admin/pricing/llm/status", headers=AUTH)
    assert status.status_code == 200 and status.json()["enabled"] is True
    env["answer_with"](ANSWER)

    async def no_missing():
        return MISSING

    import plugboard.llm_pricing as mod

    mod_missing = mod.missing_models
    mod.missing_models = no_missing
    try:
        resp = await client.post("/admin/pricing/llm/run", headers=AUTH)
    finally:
        mod.missing_models = mod_missing
    assert resp.status_code == 200 and resp.json()["calls"] == 1
