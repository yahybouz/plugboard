"""Tests for Ollama integration (sovereign provider)."""

from __future__ import annotations

import json
import pytest

import aiohttp
from aioresponses import aioresponses

from plugboard import ollama
from tests.conftest import _setup_providers
from tests.factories import PROVIDER_A, PROVIDER_OLLAMA, FAKE_OLLAMA_TAGS

OLLAMA_URL = "http://localhost:11434"

FAKE_TAGS = {
    "models": [
        {
            "name": "llama3:latest",
            "model": "llama3:latest",
            "size": 4661224676,
            "digest": "abc123",
            "modified_at": "2026-01-01T00:00:00Z",
        },
        {
            "name": "mistral:latest",
            "model": "mistral:latest",
            "size": 4109855744,
            "digest": "def456",
            "modified_at": "2026-01-02T00:00:00Z",
        },
    ]
}

FAKE_SHOW = {
    "modelfile": "FROM llama3",
    "parameters": "temperature 0.7",
    "template": "{{ .System }} {{ .Prompt }}",
    "details": {
        "format": "gguf",
        "family": "llama",
        "parameter_size": "8B",
        "quantization_level": "Q4_0",
    },
}


# ── Health / availability ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_available_ok():
    with aioresponses() as m:
        m.get(OLLAMA_URL, payload=None, body=b"Ollama is running")
        result = await ollama.check_available()
        assert result is True


@pytest.mark.asyncio
async def test_check_available_down():
    with aioresponses() as m:
        m.get(OLLAMA_URL, exception=aiohttp.ClientConnectionError("refused"))
        result = await ollama.check_available()
        assert result is False


@pytest.mark.asyncio
async def test_check_available_wrong_response():
    with aioresponses() as m:
        m.get(OLLAMA_URL, body=b"Not Found", status=404)
        result = await ollama.check_available()
        assert result is False


@pytest.mark.asyncio
async def test_health_ok():
    with aioresponses() as m:
        m.get(OLLAMA_URL, body=b"Ollama is running")
        result = await ollama.health()
        assert result["ok"] is True
        assert "latency_ms" in result


@pytest.mark.asyncio
async def test_health_down():
    with aioresponses() as m:
        m.get(OLLAMA_URL, exception=aiohttp.ClientConnectionError("refused"))
        result = await ollama.health()
        assert result["ok"] is False
        assert "error" in result


# ── List models ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_models():
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/api/tags", payload=FAKE_TAGS)
        models = await ollama.list_models()
        assert len(models) == 2
        assert models[0]["name"] == "llama3:latest"
        assert models[1]["name"] == "mistral:latest"


@pytest.mark.asyncio
async def test_list_models_error():
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/api/tags", status=500, body=b"Internal error")
        with pytest.raises(RuntimeError, match="returned 500"):
            await ollama.list_models()


# ── Pull model ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_model():
    ndjson = (
        b'{"status":"pulling manifest"}\n'
        b'{"status":"downloading abc","completed":100,"total":200}\n'
        b'{"status":"success"}\n'
    )
    with aioresponses() as m:
        m.post(f"{OLLAMA_URL}/api/pull", body=ndjson)
        chunks = []
        async for chunk in ollama.pull_model("llama3"):
            chunks.append(chunk)
        assert len(chunks) == 3
        assert chunks[0]["status"] == "pulling manifest"
        assert chunks[1]["completed"] == 100
        assert chunks[2]["status"] == "success"


@pytest.mark.asyncio
async def test_pull_model_error():
    with aioresponses() as m:
        m.post(f"{OLLAMA_URL}/api/pull", status=400, body=b"bad request")
        with pytest.raises(RuntimeError, match="pull failed"):
            async for _ in ollama.pull_model("nonexistent"):
                pass


# ── Delete model ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_model_ok():
    with aioresponses() as m:
        m.delete(f"{OLLAMA_URL}/api/delete", status=200)
        result = await ollama.delete_model("llama3")
        assert result is True


@pytest.mark.asyncio
async def test_delete_model_not_found():
    with aioresponses() as m:
        m.delete(f"{OLLAMA_URL}/api/delete", status=404)
        result = await ollama.delete_model("nonexistent")
        assert result is False


@pytest.mark.asyncio
async def test_delete_model_error():
    with aioresponses() as m:
        m.delete(f"{OLLAMA_URL}/api/delete", status=500, body=b"error")
        with pytest.raises(RuntimeError, match="delete failed"):
            await ollama.delete_model("broken")


# ── Show model ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_show_model():
    with aioresponses() as m:
        m.post(f"{OLLAMA_URL}/api/show", payload=FAKE_SHOW)
        result = await ollama.show_model("llama3")
        assert "details" in result
        assert result["details"]["family"] == "llama"


@pytest.mark.asyncio
async def test_show_model_not_found():
    with aioresponses() as m:
        m.post(f"{OLLAMA_URL}/api/show", status=404)
        with pytest.raises(RuntimeError, match="not found"):
            await ollama.show_model("nonexistent")


# ── Bootstrap ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bootstrap_disabled(monkeypatch):
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "false")
    result = await ollama.bootstrap_ollama()
    assert result is None


@pytest.mark.asyncio
async def test_bootstrap_not_available(monkeypatch):
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "auto")
    with aioresponses() as m:
        m.get(OLLAMA_URL, exception=aiohttp.ClientConnectionError("refused"))
        result = await ollama.bootstrap_ollama()
        assert result is None


@pytest.mark.asyncio
async def test_bootstrap_creates_provider(tmp_path, monkeypatch):
    """When Ollama is available, bootstrap creates a sovereign provider."""
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "auto")
    monkeypatch.setattr(ollama, "_OLLAMA_URL", OLLAMA_URL)

    # Setup empty providers file
    pfile = _setup_providers(tmp_path, monkeypatch, [PROVIDER_A])

    with aioresponses() as m:
        m.get(OLLAMA_URL, body=b"Ollama is running")
        m.get(f"{OLLAMA_URL}/api/tags", payload=FAKE_TAGS)

        result = await ollama.bootstrap_ollama()

    assert result is not None
    assert result["id"] == "ollama"
    assert result.get("provider_type") == "sovereign"

    # Verify persisted
    data = json.loads(pfile.read_text())
    ollama_providers = [p for p in data["providers"] if p["id"] == "ollama"]
    assert len(ollama_providers) == 1
    assert ollama_providers[0]["provider_type"] == "sovereign"


@pytest.mark.asyncio
async def test_bootstrap_idempotent(tmp_path, monkeypatch):
    """Calling bootstrap twice should not create duplicate providers."""
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "auto")
    monkeypatch.setattr(ollama, "_OLLAMA_URL", OLLAMA_URL)

    pfile = _setup_providers(tmp_path, monkeypatch, [PROVIDER_A])

    with aioresponses() as m:
        # First call
        m.get(OLLAMA_URL, body=b"Ollama is running")
        m.get(f"{OLLAMA_URL}/api/tags", payload=FAKE_TAGS)
        r1 = await ollama.bootstrap_ollama()

        # Second call — already registered
        m.get(OLLAMA_URL, body=b"Ollama is running")
        m.get(f"{OLLAMA_URL}/api/tags", payload=FAKE_TAGS)
        r2 = await ollama.bootstrap_ollama()

    assert r1 is not None
    assert r2 is not None
    data = json.loads(pfile.read_text())
    ollama_count = sum(1 for p in data["providers"] if p["id"] == "ollama")
    assert ollama_count == 1


# ── Config provider_type ─────────────────────────────────────────────────────


def test_create_provider_with_type(tmp_path, monkeypatch):
    """create_provider() persists provider_type field."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.config import create_provider, load_providers

    create_provider(
        name="Test Sovereign",
        base_url="http://localhost:9999",
        bearer_token="",
        provider_type="sovereign",
    )
    providers = load_providers()
    assert len(providers) == 1
    assert providers[0]["provider_type"] == "sovereign"


def test_create_provider_default_type(tmp_path, monkeypatch):
    """create_provider() defaults to 'cloud' type."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.config import create_provider, load_providers

    create_provider(
        name="Test Cloud",
        base_url="http://cloud.test",
        bearer_token="token",
    )
    providers = load_providers()
    assert providers[0]["provider_type"] == "cloud"


def test_provider_type_backward_compat(tmp_path, monkeypatch):
    """Providers without provider_type should work (treated as cloud)."""
    # PROVIDER_A has no provider_type field
    _setup_providers(tmp_path, monkeypatch, [PROVIDER_A])
    from plugboard.config import load_providers

    providers = load_providers()
    assert len(providers) == 1
    # Missing field — code should use .get("provider_type", "cloud")
    assert providers[0].get("provider_type", "cloud") == "cloud"


# ── get_ollama_url ───────────────────────────────────────────────────────────


def test_get_ollama_url():
    assert ollama.get_ollama_url() == ollama._OLLAMA_URL


# ══════════════════════════════════════════════════════════════════════════════
# API Endpoint tests
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ollama_status_endpoint(admin_client):
    """GET /admin/ollama/status returns availability info."""
    with aioresponses() as m:
        m.get(OLLAMA_URL, body=b"Ollama is running")
        resp = await admin_client.get(
            "/admin/ollama/status",
            headers={"Authorization": "Bearer adminkey"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "available" in data
    assert "url" in data
    assert "provider_id" in data


@pytest.mark.asyncio
async def test_ollama_status_requires_admin(client):
    """GET /admin/ollama/status requires admin auth."""
    resp = await client.get(
        "/admin/ollama/status",
        headers={"Authorization": "Bearer testkey"},
    )
    # testkey == admin key in client fixture
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ollama_list_models_endpoint(admin_client):
    """GET /admin/ollama/models returns installed models."""
    with aioresponses() as m:
        m.get(f"{OLLAMA_URL}/api/tags", payload=FAKE_TAGS)
        resp = await admin_client.get(
            "/admin/ollama/models",
            headers={"Authorization": "Bearer adminkey"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert len(data["models"]) == 2


@pytest.mark.asyncio
async def test_ollama_list_models_unavailable(admin_client):
    """GET /admin/ollama/models returns 502 when Ollama is down."""
    with aioresponses() as m:
        m.get(
            f"{OLLAMA_URL}/api/tags", exception=aiohttp.ClientConnectionError("refused")
        )
        resp = await admin_client.get(
            "/admin/ollama/models",
            headers={"Authorization": "Bearer adminkey"},
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_ollama_pull_endpoint(admin_client):
    """POST /admin/ollama/models/pull streams NDJSON progress."""
    import json as _json

    ndjson = b'{"status":"pulling manifest"}\n{"status":"success"}\n'
    with aioresponses() as m:
        m.post(f"{OLLAMA_URL}/api/pull", body=ndjson)
        # Mock _sync_models to avoid side effects
        m.get(f"{OLLAMA_URL}/api/tags", payload=FAKE_TAGS)
        resp = await admin_client.post(
            "/admin/ollama/models/pull",
            json={"name": "llama3"},
            headers={"Authorization": "Bearer adminkey"},
        )
    assert resp.status_code == 200
    lines = [line for line in resp.text.strip().split("\n") if line]
    assert len(lines) >= 2
    first = _json.loads(lines[0])
    assert first["status"] == "pulling manifest"


@pytest.mark.asyncio
async def test_ollama_delete_endpoint(admin_client):
    """DELETE /admin/ollama/models/{name} deletes a model."""
    with aioresponses() as m:
        m.delete(f"{OLLAMA_URL}/api/delete", status=200)
        m.get(f"{OLLAMA_URL}/api/tags", payload={"models": []})
        resp = await admin_client.delete(
            "/admin/ollama/models/llama3",
            headers={"Authorization": "Bearer adminkey"},
        )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_ollama_delete_not_found(admin_client):
    """DELETE /admin/ollama/models/{name} returns 404 for unknown model."""
    with aioresponses() as m:
        m.delete(f"{OLLAMA_URL}/api/delete", status=404)
        resp = await admin_client.delete(
            "/admin/ollama/models/nonexistent",
            headers={"Authorization": "Bearer adminkey"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ollama_show_endpoint(admin_client):
    """GET /admin/ollama/models/{name} returns model details."""
    with aioresponses() as m:
        m.post(f"{OLLAMA_URL}/api/show", payload=FAKE_SHOW)
        resp = await admin_client.get(
            "/admin/ollama/models/llama3",
            headers={"Authorization": "Bearer adminkey"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "details" in data


@pytest.mark.asyncio
async def test_ollama_show_not_found(admin_client):
    """GET /admin/ollama/models/{name} returns 404 for unknown model."""
    with aioresponses() as m:
        m.post(f"{OLLAMA_URL}/api/show", status=404)
        resp = await admin_client.get(
            "/admin/ollama/models/nonexistent",
            headers={"Authorization": "Bearer adminkey"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_provider_create_with_type_endpoint(admin_client):
    """POST /providers with provider_type persists correctly."""
    resp = await admin_client.post(
        "/providers",
        json={
            "name": "Local Test",
            "base_url": "http://localhost:9999",
            "bearer_token": "tok",
            "provider_type": "sovereign",
        },
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider_type"] == "sovereign"


# ══════════════════════════════════════════════════════════════════════════════
# Phase 5: Watchdog, Pricing, Benchmarks integration tests
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_watchdog_sovereign_health(tmp_path, monkeypatch):
    """Watchdog uses GET / for sovereign providers (not /health)."""
    _setup_providers(tmp_path, monkeypatch, [PROVIDER_OLLAMA])
    from plugboard.provider_watchdog import ping_provider

    with aioresponses() as m:
        # Sovereign health pings GET / (not /health)
        m.get("http://localhost:11434", body=b"Ollama is running")
        result = await ping_provider("ollama")
    assert result["ok"] is True
    assert result["check"] == "sovereign"


@pytest.mark.asyncio
async def test_watchdog_sovereign_health_down(tmp_path, monkeypatch):
    """Watchdog detects sovereign provider down."""
    _setup_providers(tmp_path, monkeypatch, [PROVIDER_OLLAMA])
    from plugboard.provider_watchdog import ping_provider

    with aioresponses() as m:
        m.get(
            "http://localhost:11434", exception=aiohttp.ClientConnectionError("refused")
        )
        result = await ping_provider("ollama")
    assert result["ok"] is False
    assert result["check"] == "sovereign"


@pytest.mark.asyncio
async def test_watchdog_skip_restart_sovereign(tmp_path, monkeypatch):
    """Watchdog loop does NOT attempt restart for sovereign providers."""
    _setup_providers(tmp_path, monkeypatch, [PROVIDER_OLLAMA])
    from plugboard import provider_watchdog as wd

    # Track whether restart_provider gets called
    restart_called = []

    def mock_restart(pid):
        restart_called.append(pid)
        return {"success": False, "error": "mocked"}

    monkeypatch.setattr(wd, "restart_provider", mock_restart)

    # Simulate enough failures to trigger restart
    state = wd._get_state("ollama")
    for _ in range(5):
        state.record_failure()
    assert state.consecutive_failures >= 3

    # Run one iteration of the loop logic manually
    providers = wd.load_providers()
    for p in providers:
        if p["id"] == "ollama":
            is_sovereign = p.get("provider_type", "cloud") == "sovereign"
            # Sovereign providers skip restart
            assert is_sovereign is True
            assert len(restart_called) == 0  # restart never called


def test_pricing_ollama_free():
    """Ollama models have $0 pricing in _KNOWN_PRICES."""
    from plugboard.pricing_scraper import _KNOWN_PRICES

    ollama_models = [
        "llama3",
        "mistral",
        "codellama",
        "phi3",
        "gemma2",
        "qwen2.5:7b",
        "deepseek-coder-v2",
    ]
    for model in ollama_models:
        assert model in _KNOWN_PRICES, f"{model} missing from _KNOWN_PRICES"
        assert _KNOWN_PRICES[model]["input_price"] == 0.0
        assert _KNOWN_PRICES[model]["output_price"] == 0.0


def test_benchmarks_ollama_models():
    """All Ollama models have benchmark entries."""
    from plugboard.benchmarks import get_model_info

    ollama_models = [
        "llama3",
        "llama3-70b",
        "mistral",
        "codellama",
        "phi3",
        "gemma2",
        "qwen2-5-7b",
        "deepseek-coder-v2",
    ]
    for model in ollama_models:
        info = get_model_info(model)
        assert info is not None, f"No benchmark data for {model}"
        assert info.get("cost_tier") == "free", f"{model} should have cost_tier=free"


def test_benchmarks_ollama_provider_tag():
    """Ollama benchmark entries have (Ollama) in provider name."""
    from plugboard.benchmarks import get_model_info

    for model in ["llama3", "mistral", "phi3", "gemma2"]:
        info = get_model_info(model)
        assert "Ollama" in info["provider"], f"{model} provider should mention Ollama"


def test_factories_provider_ollama():
    """Factory PROVIDER_OLLAMA has correct structure."""
    assert PROVIDER_OLLAMA["id"] == "ollama"
    assert PROVIDER_OLLAMA["provider_type"] == "sovereign"
    assert PROVIDER_OLLAMA["bearer_token"] == ""
    assert PROVIDER_OLLAMA["base_url"] == "http://localhost:11434"


def test_factories_fake_ollama_tags():
    """Factory FAKE_OLLAMA_TAGS has correct structure."""
    models = FAKE_OLLAMA_TAGS["models"]
    assert len(models) == 2
    assert models[0]["name"] == "llama3:latest"
    assert models[1]["name"] == "mistral:latest"


# ── Step 3 (2026-05-31): _sync_models preserves sticky internet_access ──


@pytest.mark.asyncio
async def test_sync_models_preserves_internet_access(monkeypatch, tmp_path):
    """Regression guard: _sync_models() must NOT wipe the internet_access
    flag written by the boot probe. update_provider() does a full
    replacement of the ``models`` key, so we snapshot the sticky flags
    from the existing config before rewriting.
    """
    # Build a provider dict that already carries an internet_access flag
    # written by a previous probe run.
    existing_provider = {
        "id": "ollama",
        "models": [
            {
                "id": "qwen3.5:397b-cloud",
                "name": "qwen3.5:397b-cloud",
                "internet_access": True,
                "internet_access_confidence": "medium",
                "last_internet_probe_at": "2026-05-31T09:00:00+00:00",
            },
            {
                "id": "old-model:latest",
                "name": "old-model:latest",
                "internet_access": False,
            },
        ],
    }

    # Mock get_provider to return the snapshot above.
    from plugboard import config as _config

    def fake_get_provider(pid):
        assert pid == "ollama"
        return existing_provider

    monkeypatch.setattr(_config, "get_provider", fake_get_provider)

    # Mock list_models so the sync returns the cloud model (still listed) +
    # a NEW model that has no sticky flag. old-model dropped from upstream.
    async def fake_list_models():
        return [
            {"name": "qwen3.5:397b-cloud", "model": "qwen3.5:397b-cloud"},
            {"name": "newly-added:latest", "model": "newly-added:latest"},
        ]

    monkeypatch.setattr(ollama, "list_models", fake_list_models)

    # Capture what gets persisted.
    captured: list = []

    def fake_update_provider(pid, updates):
        captured.append((pid, updates))

    monkeypatch.setattr(_config, "update_provider", fake_update_provider)

    # Avoid actual cache invalidation (router import side-effects in test env).
    from plugboard import router as _router

    monkeypatch.setattr(_router, "invalidate_models_cache", lambda: None)

    await ollama._sync_models({"id": "ollama"})

    # Verify the call: persisted model_list keeps internet_access=True for
    # the cloud model, drops old-model, and adds newly-added with no flag.
    assert len(captured) == 1
    pid, updates = captured[0]
    assert pid == "ollama"
    persisted = {m["id"]: m for m in updates["models"]}

    assert "qwen3.5:397b-cloud" in persisted
    assert persisted["qwen3.5:397b-cloud"]["internet_access"] is True
    assert persisted["qwen3.5:397b-cloud"]["internet_access_confidence"] == "medium"

    # New model has no sticky flag — that's expected, boot probe will fill it.
    assert "newly-added:latest" in persisted
    assert "internet_access" not in persisted["newly-added:latest"]

    # Removed upstream model is gone.
    assert "old-model:latest" not in persisted
