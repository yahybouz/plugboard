"""BUG-10: the operator's Ollama behind a token-protected relay."""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from plugboard import config, ollama, router
from plugboard import model_availability as ma
from tests.conftest import _setup_providers
from tests.factories import PROVIDER_A

RELAY = "http://172.17.0.1:11435"
TOKEN = "relay-test-token"
BEARER = f"Bearer {TOKEN}"


def _sent(m, method: str, url: str) -> list:
    return [(c.kwargs.get("headers") or {}) for c in m.requests[(method, URL(url))]]


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setattr(ollama, "_OLLAMA_URL", RELAY)
    monkeypatch.setattr(ollama, "_OLLAMA_TOKEN", TOKEN)


@pytest.mark.asyncio
async def test_daemon_calls_carry_the_token(relay):
    with aioresponses() as m:
        m.get(RELAY, body=b"Ollama is running")
        m.get(f"{RELAY}/api/tags", payload={"models": []})
        assert await ollama.check_available() is True
        assert await ollama.list_models() == []
    assert _sent(m, "GET", RELAY)[0].get("Authorization") == BEARER
    assert _sent(m, "GET", f"{RELAY}/api/tags")[0].get("Authorization") == BEARER


@pytest.mark.asyncio
async def test_no_token_sends_no_authorization(monkeypatch):
    monkeypatch.setattr(ollama, "_OLLAMA_URL", RELAY)
    monkeypatch.setattr(ollama, "_OLLAMA_TOKEN", "")
    with aioresponses() as m:
        m.get(RELAY, body=b"Ollama is running")
        assert await ollama.check_available() is True
    assert "Authorization" not in _sent(m, "GET", RELAY)[0]


@pytest.mark.asyncio
async def test_public_cloud_catalogue_never_gets_the_token(relay):
    with aioresponses() as m:
        m.get(ollama._CLOUD_CATALOG_URL, payload={"models": []})
        await ollama.fetch_cloud_catalog()
    assert "Authorization" not in _sent(m, "GET", ollama._CLOUD_CATALOG_URL)[0]


@pytest.mark.asyncio
async def test_bootstrap_stores_the_token_on_the_provider(relay, tmp_path, monkeypatch):
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "auto")
    _setup_providers(tmp_path, monkeypatch, [PROVIDER_A])
    with aioresponses() as m:
        m.get(RELAY, body=b"Ollama is running")
        m.get(f"{RELAY}/api/tags", payload={"models": []})
        await ollama.bootstrap_ollama()
    provider = config.get_provider("ollama")
    assert provider["base_url"] == RELAY
    assert provider["bearer_token"] == TOKEN


@pytest.mark.asyncio
async def test_bootstrap_adds_the_token_to_an_existing_provider(
    relay, tmp_path, monkeypatch
):
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "auto")
    existing = {
        "id": "ollama",
        "name": "Ollama",
        "base_url": RELAY,
        "bearer_token": "",
        "provider_type": "sovereign",
        "is_active": True,
        "models": [],
    }
    _setup_providers(tmp_path, monkeypatch, [PROVIDER_A, existing])
    with aioresponses() as m:
        m.get(RELAY, body=b"Ollama is running")
        m.get(f"{RELAY}/api/tags", payload={"models": []})
        await ollama.bootstrap_ollama()
    assert config.get_provider("ollama")["bearer_token"] == TOKEN


@pytest.mark.asyncio
async def test_probe_sends_the_token_only_to_the_configured_ollama(relay):
    other = "http://10.0.0.9:11434"
    with aioresponses() as m:
        m.post(f"{RELAY}/api/generate", payload={"response": "OK"})
        m.post(f"{other}/api/generate", payload={"response": "OK"})
        await ma.probe_ollama_model("llama3:latest", base_url=RELAY, timeout_s=5)
        await ma.probe_ollama_model("llama3:latest", base_url=other, timeout_s=5)
    assert _sent(m, "POST", f"{RELAY}/api/generate")[0].get("Authorization") == BEARER
    assert "Authorization" not in _sent(m, "POST", f"{other}/api/generate")[0]


@pytest.mark.asyncio
async def test_router_discovery_sends_the_provider_token():
    provider = {
        "id": "ollama",
        "name": "Ollama",
        "base_url": RELAY,
        "bearer_token": TOKEN,
        "provider_type": "sovereign",
        "is_active": True,
    }
    with aioresponses() as m:
        m.get(f"{RELAY}/api/tags", payload={"models": [{"name": "llama3:latest"}]})
        async with aiohttp.ClientSession() as session:
            rows = await router._discover_ollama_models(provider, session)
    assert [r["id"] for r in rows] == ["llama3:latest"]
    assert _sent(m, "GET", f"{RELAY}/api/tags")[0].get("Authorization") == BEARER
