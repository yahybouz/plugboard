"""Tests for benchmarks module — model info, matching, enrichment."""

from __future__ import annotations

import pytest

AUTH = {"Authorization": "Bearer testkey"}


# ── Unit tests ───────────────────────────────────────────────────────────────


def test_get_model_info_exact():
    from plugboard.benchmarks import get_model_info

    info = get_model_info("claude-sonnet-4-6")
    assert info is not None
    assert info["provider"] == "Anthropic"
    assert info["family"] == "Claude 4.6"
    assert info["tier"] == "mid"
    assert info["context_window"] == 200000
    assert "vision" in info["capabilities"]
    assert info["benchmarks"]["MMLU"] > 85


def test_get_model_info_full_id():
    """Match from a full model ID with provider prefix."""
    from plugboard.benchmarks import get_model_info

    info = get_model_info("openclaw-llm-proxy:openclaw/anthropic--claude-opus-4-6")
    assert info is not None
    assert info["provider"] == "Anthropic"
    assert info["model_key"] == "claude-opus-4-6"


def test_get_model_info_with_date_suffix():
    """Match models with date suffixes like -20251001."""
    from plugboard.benchmarks import get_model_info

    info = get_model_info("claude-haiku-4-5-20251001")
    assert info is not None
    assert info["model_key"] == "claude-haiku-4-5"


def test_get_model_info_openai():
    from plugboard.benchmarks import get_model_info

    info = get_model_info("gpt-5-4-pro")
    assert info is not None
    assert info["provider"] == "OpenAI"
    assert "HumanEval" in info["benchmarks"]


def test_get_model_info_unknown_returns_derived(monkeypatch):
    """BENCH-1 change — unknown models now get a derived info dict with
    provider/family/context guesses rather than None. Clients always
    render SOMETHING, so the picker never shows an empty row."""
    from plugboard.benchmarks import get_model_info

    info = get_model_info("unknown-model-xyz")
    assert info is not None
    assert info["info_source"] == "derived"
    assert info["matched_from"] == "unknown-model-xyz"


def test_get_all_benchmarks():
    from plugboard.benchmarks import get_all_benchmarks

    all_b = get_all_benchmarks()
    assert len(all_b) >= 15
    assert "claude-sonnet-4-6" in all_b
    assert "gpt-5-4-pro" in all_b


def test_enrich_models_list():
    """Every model gets an `info` block — curated from _MODELS_DB when
    available, derived otherwise (BENCH-1)."""
    from plugboard.benchmarks import enrich_models_list

    models = [
        {
            "id": "openclaw-llm-proxy:openclaw/anthropic--claude-sonnet-4-6",
            "object": "model",
        },
        {"id": "openclaw-llm-proxy:openclaw/openai--gpt-5-mini", "object": "model"},
        {"id": "openclaw-llm-proxy:openclaw/unknown-model", "object": "model"},
    ]
    enriched = enrich_models_list(models)
    assert len(enriched) == 3
    assert enriched[0]["info"]["provider"] == "Anthropic"
    assert enriched[1]["info"]["provider"] == "OpenAI"
    # Unknown: info still present (derived), provider inferred or "unknown".
    assert "info" in enriched[2]


# ── API endpoint tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_benchmarks_endpoint(client):
    """GET /v1/models/benchmarks returns enriched models list."""
    resp = await client.get("/v1/models/benchmarks", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "list"
    assert isinstance(data["data"], list)


@pytest.mark.asyncio
async def test_model_info_endpoint(client):
    """GET /v1/models/{id}/info returns detailed model info."""
    resp = await client.get("/v1/models/claude-sonnet-4-6/info", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "Anthropic"
    assert "benchmarks" in body
    assert "capabilities" in body
    assert "best_for" in body


@pytest.mark.asyncio
async def test_model_info_full_path(client):
    """GET /v1/models/{full_id}/info works with full model IDs."""
    resp = await client.get(
        "/v1/models/openclaw-llm-proxy:openclaw/anthropic--claude-haiku-4-5/info",
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["provider"] == "Anthropic"


@pytest.mark.asyncio
async def test_model_info_unknown_returns_derived(client):
    """GET /v1/models/{unknown}/info — BENCH-1 now returns a derived
    dict (info_source=derived) instead of 404 so clients always have
    something to render."""
    resp = await client.get("/v1/models/nonexistent-model/info", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["info_source"] == "derived"
    assert body["matched_from"] == "nonexistent-model"


@pytest.mark.asyncio
async def test_benchmarks_requires_auth(client):
    """GET /v1/models/benchmarks without auth returns 401."""
    resp = await client.get("/v1/models/benchmarks")
    assert resp.status_code == 401
