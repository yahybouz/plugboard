"""Unit tests for plugboard.router."""

from __future__ import annotations

import json
import pytest
from aioresponses import aioresponses

from tests.factories import PROVIDER_A, FAKE_COMPLETION


# ── resolve_provider_and_model ────────────────────────────────────────────────


def test_resolve_known_prefix(_providers_file):
    from plugboard.router import resolve_provider_and_model

    provider, model = resolve_provider_and_model("test-provider:fake:model-v1")
    assert provider["id"] == "test-provider"
    assert model == "fake:model-v1"


def test_resolve_longest_prefix_wins(_providers_file):
    """If two providers share a prefix, the longest match wins."""
    from plugboard.router import resolve_provider_and_model

    provider, model = resolve_provider_and_model("other-provider:other:model-v2")
    assert provider["id"] == "other-provider"
    assert model == "other:model-v2"


def test_resolve_no_prefix_falls_back_to_first_active(_providers_file):
    from plugboard.router import resolve_provider_and_model

    provider, model = resolve_provider_and_model("some-random-model")
    assert provider["id"] == "test-provider"  # first active
    assert model == "some-random-model"


def test_resolve_empty_model_uses_default(_providers_file):
    from plugboard.router import resolve_provider_and_model

    provider, model = resolve_provider_and_model("")
    assert provider["id"] == "test-provider"
    assert model == PROVIDER_A["default_model"]


def test_resolve_legacy_gemini_prefix_routes_to_subprocess_provider(
    _providers_file, monkeypatch
):
    from plugboard import router

    providers = [
        {"id": "ollama", "is_active": True, "default_model": "llama3.2:3b"},
        {
            "id": "gemini-via-gemini-cli",
            "is_active": True,
            "auth_source": "subprocess:gemini",
            "default_model": "gemini-2.5-flash",
        },
    ]
    monkeypatch.setattr(router, "load_providers", lambda: providers)

    provider, model = router.resolve_provider_and_model("gemini-cli:gemini-2.5-flash")
    assert provider["id"] == "gemini-via-gemini-cli"
    assert model == "gemini-2.5-flash"


def test_resolve_no_active_providers_raises(_empty_providers_file):
    from plugboard.router import resolve_provider_and_model

    with pytest.raises(ValueError, match="No active providers"):
        resolve_provider_and_model("any-model")


def test_resolve_rewrites_aliased_model_to_canonical_id(_providers_file):
    """When the catalog has promoted a dated-only family to a clean display
    name, API callers may send either the dated ID or the clean alias. The
    router must rewrite the alias to the canonical dated ID before matching
    a provider, so the subprocess / HTTP backend receives the exact model
    name it knows about.
    """
    from plugboard import router

    # Populate the alias cache as `list_models` would.
    router._models_alias_cache = {
        "test-provider:claude-3-opus": "test-provider:claude-3-opus-20240229",
    }
    try:
        provider, model = router.resolve_provider_and_model(
            "test-provider:claude-3-opus"
        )
        assert provider["id"] == "test-provider"
        assert model == "claude-3-opus-20240229"
    finally:
        router.invalidate_models_cache()


def test_resolve_leaves_unknown_model_unchanged_when_alias_cache_empty(
    _providers_file,
):
    """Empty alias cache must degrade to a no-op — no crashes, no rewrites."""
    from plugboard import router

    router.invalidate_models_cache()
    provider, model = router.resolve_provider_and_model("test-provider:whatever-model")
    assert model == "whatever-model"


def test_read_env_float_valid_value(monkeypatch):
    import plugboard.router as router

    monkeypatch.setenv("LB_TEST_FLOAT", "1.75")
    assert router._read_env_float("LB_TEST_FLOAT", 0.3) == pytest.approx(1.75)


def test_read_env_float_invalid_or_negative_falls_back(monkeypatch):
    import plugboard.router as router

    monkeypatch.setenv("LB_TEST_FLOAT", "not-a-number")
    assert router._read_env_float("LB_TEST_FLOAT", 0.3) == pytest.approx(0.3)

    monkeypatch.setenv("LB_TEST_FLOAT", "-2")
    assert router._read_env_float("LB_TEST_FLOAT", 0.3, min_value=0.0) == pytest.approx(
        0.3
    )


def test_resolve_all_providers_prefers_lower_inflight_load(
    _providers_file, monkeypatch
):
    import plugboard.router as router

    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.2, "requests": 100, "errors": 0},
            "other-provider": {"latency_avg": 0.2, "requests": 100, "errors": 0},
        },
    )
    monkeypatch.setattr(
        router,
        "_IN_FLIGHT_BY_PROVIDER",
        {"test-provider": 4, "other-provider": 0},
    )

    candidates = router._resolve_all_providers("fake:model-v1")
    assert candidates[0][0]["id"] == "other-provider"


def test_resolve_all_providers_keeps_prefixed_provider_first(
    _providers_file, monkeypatch
):
    import plugboard.router as router

    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 2.0, "requests": 100, "errors": 40},
            "other-provider": {"latency_avg": 0.1, "requests": 100, "errors": 0},
        },
    )
    monkeypatch.setattr(
        router,
        "_IN_FLIGHT_BY_PROVIDER",
        {"test-provider": 6, "other-provider": 0},
    )

    candidates = router._resolve_all_providers("test-provider:fake:model-v1")
    assert candidates[0][0]["id"] == "test-provider"


# ── forward ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forward_success(_providers_file):
    from plugboard.router import forward

    body = {
        "model": "test-provider:fake:model-v1",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 10,
    }
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        result = await forward(body)

    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert result["model"] == "fake:model-v1"


@pytest.mark.asyncio
async def test_forward_passes_real_model_to_backend(_providers_file):
    """The 'model' sent to backend must be the real model (without provider prefix)."""
    from plugboard.router import forward

    captured = {}

    with aioresponses() as mock:

        def callback(_url, **kwargs):
            captured["body"] = kwargs.get("json", {})
            from aioresponses import CallbackResult

            return CallbackResult(status=200, payload=FAKE_COMPLETION)

        mock.post("http://fake-llm.test/v1/chat/completions", callback=callback)
        await forward(
            {"model": "test-provider:fake:model-v1", "messages": [], "max_tokens": 5}
        )

    assert captured["body"]["model"] == "fake:model-v1"


@pytest.mark.asyncio
async def test_forward_passes_bearer_token(_providers_file):
    """Bearer token from provider config must be used."""
    from plugboard.router import forward

    captured = {}

    with aioresponses() as mock:

        def callback(_url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            from aioresponses import CallbackResult

            return CallbackResult(status=200, payload=FAKE_COMPLETION)

        mock.post("http://fake-llm.test/v1/chat/completions", callback=callback)
        await forward({"model": "test-provider:fake:model-v1", "messages": []})

    assert (
        captured["headers"]["Authorization"] == f"Bearer {PROVIDER_A['bearer_token']}"
    )


@pytest.mark.asyncio
async def test_forward_retry_on_429(_providers_file):
    """Should retry on 429 and succeed on second attempt."""
    from plugboard.router import forward

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            status=429,
            body="rate limited",
        )
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        result = await forward(
            {"model": "test-provider:fake:model-v1", "messages": []},
            timeout=0.1,
        )
    assert result["choices"][0]["message"]["content"] == "Hello!"


@pytest.mark.asyncio
async def test_forward_non_retryable_error_raises_immediately(_providers_file):
    """HTTP 400 should try next provider via fallback; all fail → RuntimeError."""
    from plugboard.router import forward

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            status=400,
            body="bad request",
        )
        # Fallback provider also fails
        mock.post(
            "http://other-llm.test/v1/chat/completions",
            status=400,
            body="bad request",
        )
        with pytest.raises(RuntimeError, match="HTTP 400"):
            await forward({"model": "test-provider:fake:model-v1", "messages": []})


@pytest.mark.asyncio
async def test_forward_all_retries_exhausted(_providers_file):
    """If all retries and all providers fail, RuntimeError is raised."""
    from plugboard.router import forward

    with aioresponses() as mock:
        for _ in range(4):  # initial + 3 retries for first provider
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                status=500,
                body="server error",
            )
        # Fallback provider also fails (initial + 3 retries)
        for _ in range(4):
            mock.post(
                "http://other-llm.test/v1/chat/completions",
                status=500,
                body="server error",
            )
        with pytest.raises(RuntimeError):
            await forward(
                {"model": "test-provider:fake:model-v1", "messages": []},
                timeout=0.1,
            )


# ── discover_models ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_via_standard_endpoint(_providers_file):
    """If /v1/models returns JSON, use it."""
    from plugboard.router import discover_models
    from tests.factories import PROVIDER_A

    fake_models = {"data": [{"id": "model-a"}, {"id": "model-b"}]}
    with aioresponses() as mock:
        mock.get(
            "http://fake-llm.test/v1/models",
            payload=fake_models,
            status=200,
            content_type="application/json",
        )
        result = await discover_models(PROVIDER_A)
    assert len(result) == 2
    assert result[0]["id"] == "model-a"
    assert result[1]["id"] == "model-b"


@pytest.mark.asyncio
async def test_discover_anthropic_uses_native_models_api(_providers_file):
    from plugboard.router import discover_models

    provider = {
        "id": "anthropic-direct",
        "base_url": "https://api.anthropic.com/v1",
        "bearer_token": "sk-ant-test",
        "auth_header_name": "x-api-key",
        "auth_header_prefix": "",
    }
    with aioresponses() as mock:
        mock.get(
            "https://api.anthropic.com/v1/models?limit=1000",
            payload={
                "data": [
                    {"id": "claude-opus-4-7", "type": "model"},
                    {"id": "claude-sonnet-4-6", "type": "model"},
                ]
            },
            status=200,
        )
        result = await discover_models(provider)
    assert [m["id"] for m in result] == ["claude-opus-4-7", "claude-sonnet-4-6"]


@pytest.mark.asyncio
async def test_discover_gemini_uses_native_models_api(_providers_file):
    from plugboard.router import discover_models

    provider = {
        "id": "gemini-direct",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "bearer_token": "AIza-test",
    }
    with aioresponses() as mock:
        mock.get(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000&key=AIza-test",
            payload={
                "models": [
                    {
                        "name": "models/gemini-2.5-flash",
                        "baseModelId": "gemini-2.5-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/text-embedding-004",
                        "baseModelId": "text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
            status=200,
        )
        result = await discover_models(provider)
    assert [m["id"] for m in result] == ["gemini-2.5-flash"]


@pytest.mark.asyncio
async def test_discover_ollama_uses_api_tags(_providers_file):
    from plugboard.router import discover_models

    provider = {
        "id": "ollama",
        "name": "Ollama",
        "base_url": "http://localhost:11434",
        "provider_type": "sovereign",
    }
    with aioresponses() as mock:
        mock.get(
            "http://localhost:11434/api/tags",
            payload={
                "models": [
                    {"name": "llama3.2:latest"},
                    {"model": "mistral:latest"},
                ]
            },
            status=200,
        )
        result = await discover_models(provider)
    assert [m["id"] for m in result] == ["llama3.2:latest", "mistral:latest"]


@pytest.mark.asyncio
async def test_discover_fallback_to_chat(_providers_file):
    """If /v1/models returns HTML, fallback to chat completion."""
    from plugboard.router import discover_models
    from tests.factories import PROVIDER_A

    chat_response = {
        "choices": [
            {
                "message": {
                    "content": "anthropic--claude-sonnet-4-6\nopenai--gpt-4o\ngoogle--gemini-2.0-flash"
                }
            }
        ],
    }
    with aioresponses() as mock:
        mock.get(
            "http://fake-llm.test/v1/models",
            status=200,
            body="<html>SPA</html>",
            content_type="text/html",
        )
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=chat_response,
            status=200,
        )
        result = await discover_models(PROVIDER_A)
    ids = [m["id"] for m in result]
    assert "anthropic--claude-sonnet-4-6" in ids
    assert "openai--gpt-4o" in ids
    assert "google--gemini-2.0-flash" in ids


@pytest.mark.asyncio
async def test_discover_filters_prose_lines(_providers_file):
    """Lines with spaces but no -- separator should be filtered out."""
    from plugboard.router import discover_models
    from tests.factories import PROVIDER_A

    chat_response = {
        "choices": [
            {
                "message": {
                    "content": "Here are the models:\nanthropic--claude-sonnet-4-6\nThis is a description\nopenai--gpt-4o"
                }
            }
        ],
    }
    with aioresponses() as mock:
        mock.get("http://fake-llm.test/v1/models", status=404)
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=chat_response,
            status=200,
        )
        result = await discover_models(PROVIDER_A)
    ids = [m["id"] for m in result]
    assert "anthropic--claude-sonnet-4-6" in ids
    assert "openai--gpt-4o" in ids
    assert not any("description" in m["id"] for m in result)


# ── list_models ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_models_with_model_list(_providers_file):
    from plugboard.router import list_models

    models = await list_models()
    ids = [m["id"] for m in models]
    assert "other-provider:other:model-v2" in ids
    assert "other-provider:other:model-v3" in ids


@pytest.mark.asyncio
async def test_list_models_fallback_to_default_model(_providers_file):
    """Provider without models list exposes its default_model."""
    from plugboard.router import list_models

    models = await list_models()
    ids = [m["id"] for m in models]
    assert "test-provider:fake:model-v1" in ids


@pytest.mark.asyncio
async def test_list_models_inactive_provider_excluded(_providers_file):
    """Inactive providers should not appear in models list."""
    import plugboard.config as cfg

    data = json.loads(cfg.PROVIDERS_FILE.read_text())
    data["providers"][0]["is_active"] = False
    cfg.PROVIDERS_FILE.write_text(json.dumps(data))

    from plugboard.router import invalidate_models_cache, list_models

    invalidate_models_cache()
    models = await list_models()
    ids = [m["id"] for m in models]
    assert not any("test-provider" in i for i in ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filter_mode, expected",
    [("", {"gpt-4o", "gpt-5"}), ("curated", {"gpt-5"})],
    ids=["default-lists-all", "curated-opt-in"],
)
async def test_list_models_curated_filter_is_opt_in(
    tmp_path, monkeypatch, filter_mode, expected
):
    # Added by the /ship coverage audit.
    # Value: protects=/v1/models lists every model of a built-in provider by
    # default and trims it only with PLUGBOARD_MODEL_FILTER=curated;
    # fails_when=list_models filters unconditionally or ignores the mode;
    # why_new=_model_filter_mode is unit-tested, the list_models gate is not and
    # the other list_models tests use providers the filter never touches;
    # seam=none
    from tests.conftest import _setup_providers

    _setup_providers(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "openai",
                "name": "OpenAI",
                "base_url": "http://fake-openai.test",
                "bearer_token": "t",
                "models": [{"id": "gpt-4o"}, {"id": "gpt-5"}],
                "is_active": True,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    )
    if filter_mode:
        monkeypatch.setenv("PLUGBOARD_MODEL_FILTER", filter_mode)
    else:
        monkeypatch.delenv("PLUGBOARD_MODEL_FILTER", raising=False)
    from plugboard.router import list_models

    models = await list_models()

    listed = {m["id"].split(":", 1)[1] for m in models if m["provider"] == "openai"}
    assert listed == expected


# ── BENCH-006 — o-series payload normalization ─────────────────────────────


def test_normalize_payload_o_series_translates_max_tokens():
    """TNR — BENCH-006: o-series models reject `max_tokens` (HTTP 400)
    in favour of `max_completion_tokens`. The router must auto-translate
    so callers written against the legacy spec keep working."""
    from plugboard.router import _normalize_payload_for_o_series

    out = _normalize_payload_for_o_series(
        "openai/o4-mini",
        {"model": "openai/o4-mini", "max_tokens": 100, "messages": []},
    )
    assert "max_tokens" not in out
    assert out["max_completion_tokens"] == 100
    # Other keys preserved
    assert out["model"] == "openai/o4-mini"
    assert out["messages"] == []


def test_normalize_payload_o_series_no_op_for_other_models():
    """Non-o-series payloads must be returned unchanged."""
    from plugboard.router import _normalize_payload_for_o_series

    payload = {"model": "openai/gpt-4o", "max_tokens": 50, "messages": []}
    out = _normalize_payload_for_o_series("openai/gpt-4o", payload)
    assert out == payload
    # Implementation may short-circuit (identity) or return a copy — both
    # are acceptable as long as the input is never mutated.
    assert payload["max_tokens"] == 50


def test_normalize_payload_o_series_no_op_when_max_tokens_absent():
    """If `max_tokens` is absent, the helper short-circuits."""
    from plugboard.router import _normalize_payload_for_o_series

    payload = {"model": "openai/o3", "messages": []}
    out = _normalize_payload_for_o_series("openai/o3", payload)
    assert out is payload  # short-circuit, identity return is acceptable


def test_normalize_payload_o_series_max_completion_tokens_wins():
    """If both keys are present, max_completion_tokens is canonical."""
    from plugboard.router import _normalize_payload_for_o_series

    out = _normalize_payload_for_o_series(
        "openai/o1",
        {"max_tokens": 100, "max_completion_tokens": 200, "messages": []},
    )
    assert "max_tokens" not in out
    assert out["max_completion_tokens"] == 200  # not overwritten by 100
