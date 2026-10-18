"""End-to-end coverage audits for pricing (PRICE-1) and benchmarks (BENCH-1)."""

from __future__ import annotations

import pytest


# ── PRICE-1 — candidate keys + fallback to _KNOWN_PRICES ──────────────────


def test_candidate_keys_handles_ollama_two_colon():
    from plugboard.billing import _candidate_keys

    candidates = _candidate_keys("deepseek-v3.1:671b-cloud")
    assert "deepseek-v3.1:671b-cloud" in candidates
    assert "deepseek-v3.1" in candidates


def test_candidate_keys_handles_github_namespaced():
    from plugboard.billing import _candidate_keys

    candidates = _candidate_keys("openai/gpt-4o")
    assert "openai/gpt-4o" in candidates
    assert "gpt-4o" in candidates


def test_candidate_keys_handles_openclaw_doubledash():
    from plugboard.billing import _candidate_keys

    candidates = _candidate_keys("openclaw/anthropic--claude-sonnet-4-6")
    assert "openclaw/anthropic--claude-sonnet-4-6" in candidates
    # After "--" split
    assert "claude-sonnet-4-6" in candidates


def test_candidate_keys_strips_latest_suffix():
    from plugboard.billing import _candidate_keys

    candidates = _candidate_keys("claude-3-5-haiku-latest")
    assert "claude-3-5-haiku" in candidates


def test_resolve_model_price_falls_back_to_known_prices():
    """When pricing.json is stale, resolve must still find _KNOWN_PRICES."""
    from plugboard.billing import resolve_model_price

    # Empty pricing grid on purpose — forces the fallback path.
    empty = {"models": {}}
    # GitHub Models — namespaced IDs live in _KNOWN_PRICES only.
    price = resolve_model_price("openai/gpt-4o", empty)
    assert price is not None
    assert price["input_price"] > 0

    # Ollama `name:tag` — the tag should be ignored, name matched.
    price2 = resolve_model_price("deepseek-v3.1:671b-cloud", empty)
    assert price2 is not None
    assert price2["input_price"] > 0


# ── BENCH-1 — candidate model keys + derived fallback ─────────────────────


def test_bench_candidate_keys_handles_ollama():
    from plugboard.benchmarks import _candidate_model_keys

    keys = _candidate_model_keys("ollama:gemma4:latest")
    assert "gemma4:latest" in keys  # name:tag form
    assert "gemma4" in keys  # bare name
    # The leading "ollama" prefix must not appear as a candidate.
    assert "ollama" not in keys


def test_bench_candidate_keys_handles_github_namespaced():
    from plugboard.benchmarks import _candidate_model_keys

    keys = _candidate_model_keys("github-models-via-gh-cli:openai/gpt-4o")
    assert "openai/gpt-4o" in keys
    assert "gpt-4o" in keys


def test_derived_info_has_provider_and_family():
    """A model with NO curated benchmark still gets a useful info dict."""
    from plugboard.benchmarks import get_model_info

    info = get_model_info("ollama:obscure-model:7b")
    assert info is not None
    assert info["info_source"] == "derived"
    assert info["provider"] == "Ollama"
    assert info["cost_tier"] == "free"
    assert info["family"] == "obscure-model"


def test_curated_info_wins_over_derived():
    """When a curated entry exists, the `info_source` must say benchmark_db."""
    from plugboard.benchmarks import get_model_info

    info = get_model_info("claude-via-claude-cli:claude-sonnet-4-6")
    assert info is not None
    # A curated entry exists for claude-sonnet-4-6 in _MODELS_DB.
    assert info["info_source"] == "benchmark_db"
    assert "benchmarks" in info and info["benchmarks"]


def test_inferred_provider_from_family():
    from plugboard.benchmarks import _infer_provider_from_family

    assert _infer_provider_from_family("claude-opus-4") == "Anthropic"
    assert _infer_provider_from_family("gemini-2.5-pro") == "Google"
    assert _infer_provider_from_family("gpt-4o") == "OpenAI"
    assert _infer_provider_from_family("llama-3.3-70b-instruct") == "Meta"
    assert _infer_provider_from_family("deepseek-v3") == "DeepSeek"
    assert _infer_provider_from_family("qwen3-vl") == "Alibaba"


# ── Coverage endpoints — integration ──────────────────────────────────────


@pytest.mark.asyncio
async def test_pricing_coverage_endpoint_returns_totals(admin_client):
    res = await admin_client.get(
        "/admin/coverage/pricing",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "total" in body
    assert "covered_paid" in body
    assert "covered_free" in body
    assert "missing" in body
    assert "coverage_pct" in body


@pytest.mark.asyncio
async def test_benchmarks_coverage_endpoint_returns_totals(admin_client):
    res = await admin_client.get(
        "/admin/coverage/benchmarks",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "total" in body
    assert "curated" in body
    assert "derived" in body
    assert body["curated"] + body["derived"] <= body["total"]


def test_new_models_pricing_and_benchmarks():
    from plugboard.billing import resolve_model_price
    from plugboard.benchmarks import get_model_info
    from plugboard.circuit_breaker import FALLBACK_MAP

    empty_grid = {"models": {}}

    # Test Kimi K2.6
    kimi_price = resolve_model_price("kimi-k2.6", empty_grid)
    assert kimi_price is not None
    assert kimi_price["input_price"] == 0.60
    assert kimi_price["output_price"] == 2.50

    kimi_info = get_model_info("kimi-k2.6")
    assert kimi_info is not None
    assert kimi_info["info_source"] == "benchmark_db"
    assert kimi_info["provider"] == "Moonshot"
    assert kimi_info["benchmarks"]["Arena_ELO"] == 1335

    # Test DeepSeek R1 and V3
    ds_r1_info = get_model_info("deepseek-r1")
    assert ds_r1_info is not None
    assert ds_r1_info["info_source"] == "benchmark_db"
    assert ds_r1_info["provider"] == "DeepSeek"
    assert ds_r1_info["benchmarks"]["Arena_ELO"] == 1415

    ds_v3_info = get_model_info("deepseek-v3")
    assert ds_v3_info is not None
    assert ds_v3_info["info_source"] == "benchmark_db"
    assert ds_v3_info["provider"] == "DeepSeek"
    assert ds_v3_info["benchmarks"]["Arena_ELO"] == 1375

    # Test Qwen 3 and 3.5
    qwen35_price = resolve_model_price("qwen3.5", empty_grid)
    assert qwen35_price is not None
    assert qwen35_price["input_price"] == 0.80

    qwen35_info = get_model_info("qwen3.5")
    assert qwen35_info is not None
    assert qwen35_info["provider"] == "Alibaba"
    assert qwen35_info["benchmarks"]["Arena_ELO"] == 1405

    # Test Claude 3.7 Sonnet
    c37_info = get_model_info("claude-3-7-sonnet")
    assert c37_info is not None
    assert c37_info["provider"] == "Anthropic"
    assert c37_info["benchmarks"]["Arena_ELO"] == 1368

    # Test Circuit Breaker Mappings
    assert FALLBACK_MAP["claude-opus-4-7"] == "claude-opus-4-6"
    assert FALLBACK_MAP["kimi-k2.6-thinking"] == "kimi-k2.6"
    assert FALLBACK_MAP["deepseek-r1"] == "deepseek-v3"
    assert FALLBACK_MAP["qwen3.5"] == "qwen3"
