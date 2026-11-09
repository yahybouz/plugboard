"""Provider health: /health/detailed and the dashboard Ping agree."""

from __future__ import annotations

import pytest

from plugboard.health_probe import health_url_for

ADMIN = {"Authorization": "Bearer testkey"}


@pytest.mark.parametrize(
    "provider, expected",
    [
        # OpenAI-compatible relay in front of Ollama: /v1/health does not exist.
        (
            {"base_url": "http://172.17.0.1:11435/v1", "provider_type": "sovereign"},
            "http://172.17.0.1:11435/v1/models",
        ),
        # Bare Ollama base: "/" answers "Ollama is running".
        (
            {"base_url": "http://ollama:11434", "provider_type": "sovereign"},
            "http://ollama:11434/",
        ),
        ({"base_url": "http://localhost:11434"}, "http://localhost:11434/"),
        (
            {"base_url": "https://llm.example/v1", "provider_type": "cloud"},
            "https://llm.example/v1/health",
        ),
    ],
)
def test_health_url_for(provider, expected):
    assert health_url_for(provider) == expected


@pytest.mark.asyncio
async def test_health_detailed_uses_the_shared_probe(client, monkeypatch):
    """CLI providers were probed at subprocess://<kind>/health (always down)
    and a 401 counted as healthy: the endpoint now reports the same result
    as the dashboard Ping."""
    import plugboard.health_probe as hp

    seen = []

    async def fake_probe(provider):
        seen.append(provider["id"])
        return {"ok": True, "status": "running", "url": "x", "latency_ms": 1.0}

    monkeypatch.setattr(hp, "probe_provider", fake_probe)
    resp = await client.get("/health/detailed", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert seen, "no provider probed"
    assert body["ok"] is True
    assert {r["provider"] for r in body["providers"]} == set(seen)
    assert all(r["ok"] for r in body["providers"])


@pytest.mark.asyncio
async def test_health_detailed_reports_a_failing_probe(client, monkeypatch):
    import plugboard.health_probe as hp

    async def fake_probe(provider):
        return {"ok": False, "status": 401, "url": "x", "latency_ms": 1.0}

    monkeypatch.setattr(hp, "probe_provider", fake_probe)
    body = (await client.get("/health/detailed", headers=ADMIN)).json()
    assert body["ok"] is False
    assert all(r["ok"] is False and r["status"] == 401 for r in body["providers"])
