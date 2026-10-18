"""
Tests for direct provider integration (Anthropic, OpenAI, Gemini, GitHub Models).

Covers:
- _build_auth_header helper (default vs custom schemes)
- Presets module (list, get, create_from_preset)
- Admin endpoints (/admin/direct-providers/*)
- Backward compatibility (existing providers keep Bearer default)
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses, CallbackResult

from tests.conftest import _setup_providers
from tests.factories import PROVIDER_A, FAKE_COMPLETION


# ══════════════════════════════════════════════════════════════════════════════
# _build_auth_header helper
# ══════════════════════════════════════════════════════════════════════════════


def test_build_auth_header_default_bearer():
    """Default behavior: Authorization: Bearer <token> (OpenClaw compat)."""
    from plugboard.router import _build_auth_header

    provider = {"bearer_token": "abc123"}
    header = _build_auth_header(provider)
    assert header == {"Authorization": "Bearer abc123"}


def test_build_auth_header_anthropic_xapikey():
    """Anthropic: x-api-key header with no prefix."""
    from plugboard.router import _build_auth_header

    provider = {
        "bearer_token": "sk-ant-xyz",
        "auth_header_name": "x-api-key",
        "auth_header_prefix": "",
    }
    header = _build_auth_header(provider)
    assert header == {"x-api-key": "sk-ant-xyz"}


def test_build_auth_header_custom_scheme():
    """Custom header name + prefix."""
    from plugboard.router import _build_auth_header

    provider = {
        "bearer_token": "mykey",
        "auth_header_name": "X-Custom-Auth",
        "auth_header_prefix": "Token",
    }
    header = _build_auth_header(provider)
    assert header == {"X-Custom-Auth": "Token mykey"}


def test_build_auth_header_explicit_bearer():
    """Explicit Bearer prefix (OpenAI, Gemini, GitHub Models)."""
    from plugboard.router import _build_auth_header

    provider = {
        "bearer_token": "sk-xxx",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
    }
    header = _build_auth_header(provider)
    assert header == {"Authorization": "Bearer sk-xxx"}


def test_build_auth_header_backward_compat_no_fields():
    """Existing provider record without auth_header_* fields works as before."""
    from plugboard.router import _build_auth_header

    # PROVIDER_A has no auth_header_name/prefix — should default to Bearer
    header = _build_auth_header(PROVIDER_A)
    assert header == {"Authorization": f"Bearer {PROVIDER_A['bearer_token']}"}


# ══════════════════════════════════════════════════════════════════════════════
# Presets module
# ══════════════════════════════════════════════════════════════════════════════


def test_list_presets():
    """list_presets returns all 5 presets without secrets."""
    from plugboard.direct_providers import list_presets

    presets = list_presets()
    keys = [p["key"] for p in presets]
    assert "anthropic" in keys
    assert "openai" in keys
    assert "gemini" in keys
    assert "github-models" in keys
    assert "copilot" in keys
    # No secrets exposed
    for p in presets:
        assert "api_key" not in p
        assert "bearer_token" not in p


def test_get_preset_anthropic():
    """Anthropic preset uses x-api-key header."""
    from plugboard.direct_providers import get_preset

    preset = get_preset("anthropic")
    assert preset is not None
    assert preset["auth_header_name"] == "x-api-key"
    assert preset["auth_header_prefix"] == ""
    assert "anthropic.com" in preset["base_url"]


def test_get_preset_openai():
    """OpenAI preset uses Bearer auth."""
    from plugboard.direct_providers import get_preset

    preset = get_preset("openai")
    assert preset["auth_header_name"] == "Authorization"
    assert preset["auth_header_prefix"] == "Bearer"
    assert "openai.com" in preset["base_url"]


def test_get_preset_copilot_is_roadmap():
    """Copilot preset is marked as roadmap (OAuth not yet supported)."""
    from plugboard.direct_providers import get_preset

    preset = get_preset("copilot")
    assert preset["status"] == "roadmap"


def test_get_preset_unknown():
    """Unknown preset returns None."""
    from plugboard.direct_providers import get_preset

    assert get_preset("nonexistent") is None


# ══════════════════════════════════════════════════════════════════════════════
# create_from_preset
# ══════════════════════════════════════════════════════════════════════════════


def test_create_from_preset_anthropic(tmp_path, monkeypatch):
    """create_from_preset('anthropic', key) creates a provider with x-api-key."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.direct_providers import create_from_preset

    record = create_from_preset("anthropic", "sk-ant-test")
    assert record["name"] == "Anthropic Direct"
    assert record["auth_header_name"] == "x-api-key"
    assert record["auth_header_prefix"] == ""
    # Token must be masked in returned record
    assert "sk-ant-test" not in record["bearer_token"]


def test_create_from_preset_openai(tmp_path, monkeypatch):
    """OpenAI preset creates provider with Bearer auth."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.direct_providers import create_from_preset

    record = create_from_preset("openai", "sk-test-key")
    assert record["auth_header_name"] == "Authorization"
    assert record["auth_header_prefix"] == "Bearer"


def test_create_from_preset_gemini(tmp_path, monkeypatch):
    """Gemini preset uses OpenAI-compat endpoint."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.direct_providers import create_from_preset

    record = create_from_preset("gemini", "AIza-xxx")
    assert "generativelanguage.googleapis.com" in record["base_url"]


def test_create_from_preset_github_models(tmp_path, monkeypatch):
    """GitHub Models preset uses Azure endpoint + Bearer."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.direct_providers import create_from_preset

    record = create_from_preset("github-models", "ghp_token")
    assert "models.github.ai/inference" in record["base_url"]


def test_create_from_preset_unknown_raises():
    """Unknown preset raises ValueError."""
    from plugboard.direct_providers import create_from_preset

    with pytest.raises(ValueError, match="Unknown preset"):
        create_from_preset("nonexistent", "key")


def test_create_from_preset_copilot_raises():
    """Copilot preset is roadmap — raises ValueError."""
    from plugboard.direct_providers import create_from_preset

    with pytest.raises(ValueError, match="roadmap"):
        create_from_preset("copilot", "key")


def test_create_from_preset_empty_key_raises():
    """Empty api_key raises ValueError."""
    from plugboard.direct_providers import create_from_preset

    with pytest.raises(ValueError, match="api_key is required"):
        create_from_preset("openai", "")


# ══════════════════════════════════════════════════════════════════════════════
# Admin endpoints
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_admin_list_presets_endpoint(admin_client):
    """GET /admin/direct-providers/presets returns API-key + OAuth presets."""
    resp = await admin_client.get(
        "/admin/direct-providers/presets",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "presets" in data
    keys = {p["key"] for p in data["presets"]}
    # API-key presets
    assert "anthropic" in keys
    assert "openai" in keys
    assert "gemini" in keys
    assert "github-models" in keys
    # OAuth presets (delegated to CLIs)
    assert "chatgpt-oauth" in keys
    assert "gemini-oauth" in keys
    assert "github-copilot-oauth" in keys
    assert "github-models-oauth" in keys


@pytest.mark.asyncio
async def test_admin_create_anthropic_endpoint(admin_client, tmp_path, monkeypatch):
    """POST /admin/direct-providers/anthropic with api_key creates provider."""
    resp = await admin_client.post(
        "/admin/direct-providers/anthropic",
        json={"api_key": "sk-ant-real"},
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Anthropic Direct"
    assert data["auth_header_name"] == "x-api-key"


@pytest.mark.asyncio
async def test_admin_create_unknown_preset_returns_404(admin_client):
    """POST /admin/direct-providers/bogus returns 404."""
    resp = await admin_client.post(
        "/admin/direct-providers/bogus",
        json={"api_key": "key"},
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_create_roadmap_returns_422(admin_client):
    """POST /admin/direct-providers/copilot returns 422 (roadmap)."""
    resp = await admin_client.post(
        "/admin/direct-providers/copilot",
        json={"api_key": "key"},
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_admin_create_missing_key_returns_422(admin_client):
    """POST without api_key returns 422 (Pydantic validation)."""
    resp = await admin_client.post(
        "/admin/direct-providers/openai",
        json={},
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end: forward uses correct auth header
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_forward_uses_xapikey_for_anthropic(tmp_path, monkeypatch):
    """forward() sends x-api-key header for Anthropic-style providers."""
    anthropic_provider = {
        "id": "anthropic-direct",
        "name": "Anthropic Direct",
        "base_url": "http://anthropic.test",
        "bearer_token": "sk-ant-xxx",
        "default_model": "claude-sonnet-4-20250514",
        "is_active": True,
        "provider_type": "cloud",
        "auth_header_name": "x-api-key",
        "auth_header_prefix": "",
        "timeout": 60.0,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    _setup_providers(tmp_path, monkeypatch, [anthropic_provider])

    from plugboard.router import forward

    captured = {}

    def _capture(url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return CallbackResult(status=200, payload=FAKE_COMPLETION)

    with aioresponses() as m:
        m.post(
            "http://anthropic.test/v1/chat/completions",
            callback=_capture,
            payload=FAKE_COMPLETION,
        )
        await forward(
            {
                "model": "anthropic-direct:claude-sonnet-4-20250514",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )

    assert captured["headers"].get("x-api-key") == "sk-ant-xxx"
    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_forward_uses_bearer_for_existing_openclaw(tmp_path, monkeypatch):
    """Existing OpenClaw provider (no auth_header_* fields) still uses Bearer."""
    _setup_providers(tmp_path, monkeypatch, [PROVIDER_A])

    from plugboard.router import forward

    captured = {}

    def _capture(url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return CallbackResult(status=200, payload=FAKE_COMPLETION)

    with aioresponses() as m:
        m.post(
            PROVIDER_A["base_url"] + "/v1/chat/completions",
            callback=_capture,
            payload=FAKE_COMPLETION,
        )
        await forward(
            {
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "hi"}],
            }
        )

    assert (
        captured["headers"].get("Authorization")
        == f"Bearer {PROVIDER_A['bearer_token']}"
    )


# ── BENCH-005 — OpenAI direct preset enriched (closes BENCH-005) ────


def test_bench_005_openai_preset_has_gpt5_family():
    """ARCH BENCH-005: the OpenAI direct preset must expose the GPT-5 family
    so the bench cloud has an alternative to github-models-via-gh-cli (which
    rate-limits at 46x HTTP 429 / 21min)."""
    from plugboard.direct_providers import PRESETS

    openai = PRESETS["openai"]
    models = set(openai["known_models"])
    # GPT-5 family
    assert "gpt-5" in models
    assert "gpt-5-chat" in models
    assert "gpt-5-mini" in models
    assert "gpt-5-nano" in models


def test_bench_005_openai_preset_has_gpt41_family():
    """GPT-4.1 family must be provisioned."""
    from plugboard.direct_providers import PRESETS

    models = set(PRESETS["openai"]["known_models"])
    assert "gpt-4.1" in models
    assert "gpt-4.1-mini" in models
    assert "gpt-4.1-nano" in models


def test_bench_005_openai_preset_has_o3_pro():
    """o3-pro is the top-tier reasoning model — was missing pre-BENCH-005."""
    from plugboard.direct_providers import PRESETS

    assert "o3-pro" in set(PRESETS["openai"]["known_models"])


def test_bench_005_openai_preset_status_available():
    """Preset must be 'available' (not 'roadmap') so create_from_preset works."""
    from plugboard.direct_providers import PRESETS

    assert PRESETS["openai"]["status"] == "available"
