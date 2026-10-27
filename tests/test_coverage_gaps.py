"""
Tests to cover gaps identified by coverage analysis.
Targets: router.py, monitoring.py, pricing_scraper.py, app.py
"""

from __future__ import annotations

import pytest

import aiohttp
from aioresponses import aioresponses

from tests.conftest import _setup_providers
from tests.factories import PROVIDER_A, PROVIDER_B, FAKE_COMPLETION


# ══════════════════════════════════════════════════════════════════════════════
# router.py — discover_models
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_discover_models_via_v1_models(_providers_file):
    """discover_models: strategy 1 — GET /v1/models returns model list."""
    from plugboard.router import discover_models

    provider = {"id": "test", "base_url": "http://fake.test", "bearer_token": "tok"}
    with aioresponses() as m:
        m.get(
            "http://fake.test/v1/models",
            payload={
                "data": [
                    {"id": "model-a", "object": "model"},
                    {"id": "model-b", "object": "model"},
                    {"id": "", "object": "model"},  # empty id — should be filtered
                ]
            },
        )
        result = await discover_models(provider)
    assert len(result) == 2
    assert result[0]["id"] == "model-a"
    assert result[1]["id"] == "model-b"


@pytest.mark.asyncio
async def test_discover_models_v1_fails_fallback_chat(_providers_file):
    """discover_models: strategy 1 fails (404) → strategy 2 via chat."""
    from plugboard.router import discover_models

    provider = {
        "id": "test",
        "base_url": "http://fake.test",
        "bearer_token": "tok",
        "default_model": "main",
    }
    chat_response = {
        "choices": [
            {"message": {"content": "anthropic--claude-sonnet\nopenai--gpt-5\n"}}
        ]
    }
    with aioresponses() as m:
        m.get("http://fake.test/v1/models", status=404, body=b"Not Found")
        m.post("http://fake.test/v1/chat/completions", payload=chat_response)
        result = await discover_models(provider)
    assert len(result) == 2
    assert result[0]["id"] == "anthropic--claude-sonnet"


@pytest.mark.asyncio
async def test_discover_models_v1_exception_fallback(_providers_file):
    """discover_models: strategy 1 throws exception → try strategy 2."""
    from plugboard.router import discover_models

    provider = {
        "id": "test",
        "base_url": "http://fake.test",
        "bearer_token": "tok",
        "default_model": "main",
    }
    chat_response = {"choices": [{"message": {"content": "model--a\nmodel--b\n"}}]}
    with aioresponses() as m:
        m.get(
            "http://fake.test/v1/models",
            exception=aiohttp.ClientConnectionError("fail"),
        )
        m.post("http://fake.test/v1/chat/completions", payload=chat_response)
        result = await discover_models(provider)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_discover_models_no_default_model(_providers_file):
    """discover_models: no default_model → returns empty list."""
    from plugboard.router import discover_models

    provider = {
        "id": "test",
        "base_url": "http://fake.test",
        "bearer_token": "tok",
        "default_model": "",
    }
    with aioresponses() as m:
        m.get("http://fake.test/v1/models", status=404, body=b"nope")
        result = await discover_models(provider)
    assert result == []


@pytest.mark.asyncio
async def test_discover_models_chat_http_error(_providers_file):
    """discover_models: chat strategy returns non-200."""
    from plugboard.router import discover_models

    provider = {
        "id": "test",
        "base_url": "http://fake.test",
        "bearer_token": "tok",
        "default_model": "main",
    }
    with aioresponses() as m:
        m.get("http://fake.test/v1/models", status=404, body=b"nope")
        m.post("http://fake.test/v1/chat/completions", status=500, body=b"error")
        result = await discover_models(provider)
    assert result == []


@pytest.mark.asyncio
async def test_discover_models_chat_exception(_providers_file):
    """discover_models: chat strategy throws exception."""
    from plugboard.router import discover_models

    provider = {
        "id": "test",
        "base_url": "http://fake.test",
        "bearer_token": "tok",
        "default_model": "main",
    }
    with aioresponses() as m:
        m.get("http://fake.test/v1/models", status=404, body=b"nope")
        m.post(
            "http://fake.test/v1/chat/completions",
            exception=aiohttp.ClientConnectionError("down"),
        )
        result = await discover_models(provider)
    assert result == []


@pytest.mark.asyncio
async def test_discover_models_filters_prose(_providers_file):
    """discover_models: filters out prose lines from chat response."""
    from plugboard.router import discover_models

    provider = {
        "id": "test",
        "base_url": "http://fake.test",
        "bearer_token": "tok",
        "default_model": "main",
    }
    chat_response = {
        "choices": [
            {
                "message": {
                    "content": (
                        "# Models\n"  # filtered: starts with #
                        "ab\n"  # filtered: len < 3
                        "This is a sentence\n"  # filtered: space without --
                        "anthropic--claude\n"  # kept
                        "  - openai--gpt-5  \n"  # kept after strip
                        "```model-x```\n"  # kept after strip backticks
                    )
                }
            }
        ]
    }
    with aioresponses() as m:
        m.get("http://fake.test/v1/models", status=404)
        m.post("http://fake.test/v1/chat/completions", payload=chat_response)
        result = await discover_models(provider)
    ids = [r["id"] for r in result]
    assert "anthropic--claude" in ids
    assert "openai--gpt-5" in ids
    assert "model-x" in ids
    assert len(ids) == 3


# ══════════════════════════════════════════════════════════════════════════════
# router.py — _resolve_all_providers, _track_in_flight
# ══════════════════════════════════════════════════════════════════════════════


def test_resolve_all_no_active_providers(tmp_path, monkeypatch):
    """_resolve_all_providers raises ValueError when no active providers."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.router import _resolve_all_providers

    with pytest.raises(ValueError, match="No active providers"):
        _resolve_all_providers("some-model")


def test_resolve_all_no_prefix_match(tmp_path, monkeypatch):
    """_resolve_all_providers with no prefix match uses all providers."""
    _setup_providers(tmp_path, monkeypatch, [PROVIDER_A, PROVIDER_B])
    from plugboard.router import _resolve_all_providers

    results = _resolve_all_providers("unknown-model")
    assert len(results) >= 2
    # First entry should use the model as-is
    assert results[0][1] == "unknown-model"


def test_track_in_flight_cleanup():
    """_track_in_flight cleans up counter on zero."""
    from plugboard.router import _track_in_flight, _IN_FLIGHT_BY_PROVIDER

    pid = "test-track"
    _IN_FLIGHT_BY_PROVIDER.pop(pid, None)
    with _track_in_flight(pid):
        assert _IN_FLIGHT_BY_PROVIDER.get(pid) == 1
    # After exit, should be cleaned up (popped, not 0)
    assert pid not in _IN_FLIGHT_BY_PROVIDER


def test_track_in_flight_nested():
    """_track_in_flight handles nested requests."""
    from plugboard.router import _track_in_flight, _IN_FLIGHT_BY_PROVIDER

    pid = "test-nested"
    _IN_FLIGHT_BY_PROVIDER.pop(pid, None)
    with _track_in_flight(pid):
        assert _IN_FLIGHT_BY_PROVIDER[pid] == 1
        with _track_in_flight(pid):
            assert _IN_FLIGHT_BY_PROVIDER[pid] == 2
        assert _IN_FLIGHT_BY_PROVIDER[pid] == 1
    assert pid not in _IN_FLIGHT_BY_PROVIDER


# ══════════════════════════════════════════════════════════════════════════════
# monitoring.py
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_check_model_availability_ok():
    """check_model_availability returns ok when model responds 200."""
    from plugboard.monitoring import check_model_availability

    with aioresponses() as m:
        m.post("http://localhost:8765/v1/chat/completions", payload=FAKE_COMPLETION)
        result = await check_model_availability("test:model", "tok")
    assert result["ok"] is True
    assert result["model"] == "test:model"
    assert "latency_ms" in result


@pytest.mark.asyncio
async def test_check_model_availability_error():
    """check_model_availability returns ok=False on HTTP error."""
    from plugboard.monitoring import check_model_availability

    with aioresponses() as m:
        m.post("http://localhost:8765/v1/chat/completions", status=500)
        result = await check_model_availability("test:model", "tok")
    assert result["ok"] is False
    assert result["status"] == 500


@pytest.mark.asyncio
async def test_check_model_availability_exception():
    """check_model_availability returns ok=False on connection error."""
    from plugboard.monitoring import check_model_availability

    with aioresponses() as m:
        m.post(
            "http://localhost:8765/v1/chat/completions",
            exception=aiohttp.ClientConnectionError("refused"),
        )
        result = await check_model_availability("test:model", "tok")
    assert result["ok"] is False
    assert "error" in result


@pytest.mark.asyncio
async def test_check_provider_health_not_found(tmp_path, monkeypatch):
    """check_provider_health returns error when provider not found."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.monitoring import check_provider_health

    result = await check_provider_health("nonexistent")
    assert result["ok"] is False
    assert "not found" in result.get("error", "").lower() or not result["ok"]


@pytest.mark.asyncio
async def test_full_health_check_inactive_provider(tmp_path, monkeypatch):
    """full_health_check marks inactive providers as unhealthy."""
    inactive = {**PROVIDER_A, "is_active": False}
    _setup_providers(tmp_path, monkeypatch, [inactive])
    from plugboard.monitoring import full_health_check

    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path / "data"))
    report = await full_health_check()
    assert report["summary"]["unhealthy"] == 1
    p = report["providers"][0]
    assert p["active"] is False


# ══════════════════════════════════════════════════════════════════════════════
# pricing_scraper.py
# ══════════════════════════════════════════════════════════════════════════════


def test_match_known_price_exact():
    """_match_known_price: exact match."""
    from plugboard.pricing_scraper import _match_known_price

    result = _match_known_price("claude-sonnet-4-6")
    assert result is not None
    assert result["input_price"] == 3.0


def test_match_known_price_normalized():
    """_match_known_price: normalized match (dots→dashes)."""
    from plugboard.pricing_scraper import _match_known_price

    result = _match_known_price("gemini-2.5-pro")
    assert result is not None
    assert result["input_price"] == 1.25


def test_match_known_price_ollama_tag():
    """_match_known_price: Ollama format model:tag extracts base name (Turbo cloud pricing)."""
    from plugboard.pricing_scraper import _match_known_price

    result = _match_known_price("deepseek-v3.1:671b-cloud")
    assert result is not None
    # Ollama Turbo cloud model — priced like source provider (DeepSeek)
    assert result["input_price"] == 0.27
    assert result["output_price"] == 1.10


def test_match_known_price_ollama_local_free():
    """_match_known_price: local Ollama models stay free."""
    from plugboard.pricing_scraper import _match_known_price

    result = _match_known_price("llama3")
    assert result is not None
    assert result["input_price"] == 0.0
    result = _match_known_price("mistral")
    assert result is not None
    assert result["input_price"] == 0.0


def test_match_known_price_suffix():
    """_match_known_price: suffix match (longest wins)."""
    from plugboard.pricing_scraper import _match_known_price

    # "claude-sonnet-4-6" should match via suffix
    result = _match_known_price("anthropic-claude-sonnet-4-6")
    assert result is not None
    assert result["input_price"] == 3.0


def test_match_known_price_no_match():
    """_match_known_price: returns None for unknown model."""
    from plugboard.pricing_scraper import _match_known_price

    result = _match_known_price("totally-unknown-model-xyz")
    assert result is None


def test_discover_new_models_skips_known():
    """_discover_new_models skips models already in pricing grid."""
    from plugboard.pricing_scraper import _discover_new_models

    existing = {"claude-sonnet-4-6": {"input_price": 3.0, "output_price": 15.0}}
    provider_models = [
        "provider:namespace--claude-sonnet-4-6",  # already priced
        "provider:namespace--totally-new-model",  # new
    ]
    missing = _discover_new_models(existing, provider_models)
    assert "claude-sonnet-4-6" not in missing
    assert "totally-new-model" in missing


def test_discover_new_models_ollama_base_name():
    """_discover_new_models skips Ollama models whose base name is known."""
    from plugboard.pricing_scraper import _discover_new_models

    existing = {"deepseek-v3.1": {"input_price": 0.0, "output_price": 0.0}}
    provider_models = ["deepseek-v3.1:671b-cloud"]
    missing = _discover_new_models(existing, provider_models)
    assert len(missing) == 0


@pytest.mark.asyncio
async def test_litellm_pricing_parses_and_converts_per_token_to_per_million(
    monkeypatch,
):
    """The LiteLLM fetcher must parse `input_cost_per_token` (float
    per token) and emit our `input_price` (float per 1M tokens)."""
    from plugboard import pricing_scraper as ps

    sample = {
        "sample_spec": {"mode": "chat"},  # no cost → skipped
        "image-model": {
            "mode": "image_generation",
            "input_cost_per_token": 0.01,
            "output_cost_per_token": 0.02,
        },  # wrong mode → skipped
        "claude-opus-4-1": {
            "mode": "chat",
            "input_cost_per_token": 1.5e-05,
            "output_cost_per_token": 7.5e-05,
        },
        "gpt-4o": {
            "mode": "chat",
            "input_cost_per_token": 2.5e-06,
            "output_cost_per_token": 1e-05,
        },
    }

    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self, content_type=None):
            return sample

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **kw):
            return _FakeResp()

    monkeypatch.setattr(ps.aiohttp, "ClientSession", lambda: _FakeSession())
    prices = await ps._fetch_litellm_pricing()
    # Per-token → per-million conversion
    assert prices["claude-opus-4-1"] == {"input_price": 15.0, "output_price": 75.0}
    assert prices["gpt-4o"] == {"input_price": 2.5, "output_price": 10.0}
    # GitHub-Models alias emitted for bare OpenAI keys
    assert prices["openai/gpt-4o"] == {"input_price": 2.5, "output_price": 10.0}
    # Non-chat entries dropped
    assert "sample_spec" not in prices
    assert "image-model" not in prices


@pytest.mark.asyncio
async def test_litellm_pricing_handles_network_failure_gracefully(monkeypatch):
    """A network error must not crash the scraper — just return {}."""
    from plugboard import pricing_scraper as ps

    class _Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **kw):
            raise RuntimeError("DNS down")

    monkeypatch.setattr(ps.aiohttp, "ClientSession", lambda: _Boom())
    result = await ps._fetch_litellm_pricing()
    assert result == {}


# ══════════════════════════════════════════════════════════════════════════════
# app.py — spending limits, model access
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_model_access_denied(admin_client, _providers_file, monkeypatch):
    """Completion with disallowed model returns 403."""
    from plugboard.apikeys import create_key

    # Create a key with restricted models
    key_data = create_key(
        "restricted-client", label="test", allowed_models=["fake:model-v1"]
    )
    raw_key = key_data["key"]

    with aioresponses() as m:
        m.post(PROVIDER_A["base_url"] + "/v1/chat/completions", payload=FAKE_COMPLETION)
        resp = await admin_client.post(
            "/v1/chat/completions",
            json={
                "model": "test-provider:forbidden-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": f"Bearer {raw_key}"},
        )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_model_access_allowed(admin_client, _providers_file, monkeypatch):
    """Completion with allowed model goes through."""
    from plugboard.apikeys import create_key

    monkeypatch.setattr("plugboard.ratelimit._LIMIT", 0)
    key_data = create_key("allowed-client", label="test", allowed_models=["fake:model-v1"])
    raw_key = key_data["key"]

    with aioresponses() as m:
        m.post(PROVIDER_A["base_url"] + "/v1/chat/completions", payload=FAKE_COMPLETION)
        resp = await admin_client.post(
            "/v1/chat/completions",
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": f"Bearer {raw_key}"},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_spending_limit_exceeded(admin_client, _providers_file, monkeypatch):
    """Completion exceeding spending limit returns 429."""
    from plugboard.apikeys import create_key
    from plugboard import usage as _usage, billing as _billing

    monkeypatch.setattr("plugboard.ratelimit._LIMIT", 0)
    key_data = create_key("limited-client", label="test", monthly_limit_usd=0.001)
    raw_key = key_data["key"]

    # Pre-fill usage to exceed limit
    month = _billing.current_pricing_month()
    _usage.append_usage(
        client_id="limited-client",
        model="test",
        provider_id="test",
        prompt_tokens=1000000,
        completion_tokens=1000000,
        pricing_month=month,
        cost_usd=1.0,
        key_id=key_data["key_id"],
    )

    resp = await admin_client.post(
        "/v1/chat/completions",
        json={
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 429
    assert "spending limit" in resp.json()["detail"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# app.py — /v1/me, /usage/me endpoints
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_v1_me_returns_client_info(admin_client, _providers_file, monkeypatch):
    """GET /v1/me returns client info."""
    from plugboard.apikeys import create_key

    monkeypatch.setattr("plugboard.ratelimit._LIMIT", 0)
    key_data = create_key("me-client", label="me-key")
    raw_key = key_data["key"]
    resp = await admin_client.get(
        "/v1/me",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["client_id"] == "me-client"


@pytest.mark.asyncio
async def test_usage_me_returns_usage(admin_client, _providers_file, monkeypatch):
    """GET /usage/me returns usage for calling client."""
    from plugboard.apikeys import create_key

    monkeypatch.setattr("plugboard.ratelimit._LIMIT", 0)
    key_data = create_key("usage-client", label="usage-key")
    raw_key = key_data["key"]
    resp = await admin_client.get(
        "/usage/me",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code == 200
