"""Integration tests for all FastAPI endpoints."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from tests.factories import FAKE_COMPLETION, PROVIDER_A


AUTH = {"Authorization": "Bearer testkey"}


# ── /health ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_no_auth_required(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["providers"] == 2
    assert "test-provider" in body["active"]


@pytest.mark.asyncio
async def test_health_returns_version(client):
    from plugboard import __version__

    resp = await client.get("/health")
    assert resp.json()["version"] == __version__


# ── /v1/chat/completions ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_completions_success(client):
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hi"}],
                "max_tokens": 10,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"


@pytest.mark.asyncio
async def test_completions_unauthorized(client):
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong-key"},
        json={"model": "x", "messages": []},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_completions_no_auth_header(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": []},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_completions_backend_error_returns_502(client):
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            status=400,
            body="bad request",
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": "test-provider:fake:model-v1", "messages": []},
        )
    assert resp.status_code == 502


# ── /v1/models ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_models_list(client):
    resp = await client.get("/v1/models", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert len(body["data"]) >= 2


@pytest.mark.asyncio
async def test_models_unauthorized(client):
    resp = await client.get("/v1/models")
    assert resp.status_code == 401


# ── GET /providers ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_providers(client):
    resp = await client.get("/providers", headers=AUTH)
    assert resp.status_code == 200
    providers = resp.json()
    assert len(providers) == 2
    assert providers[0]["id"] == "test-provider"


@pytest.mark.asyncio
async def test_get_providers_token_masked(client):
    resp = await client.get("/providers", headers=AUTH)
    for p in resp.json():
        token = p["bearer_token"]
        assert PROVIDER_A["bearer_token"] not in token  # real token not exposed
        assert "***" in token


@pytest.mark.asyncio
async def test_get_providers_unauthorized(client):
    resp = await client.get("/providers")
    assert resp.status_code == 401


# ── POST /providers ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_provider(client):
    resp = await client.post(
        "/providers",
        headers=AUTH,
        json={
            "name": "New Provider",
            "base_url": "http://new-llm.test",
            "bearer_token": "new-token",
            "default_model": "new:model",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "new-provider"
    assert body["base_url"] == "http://new-llm.test"
    assert "***" in body["bearer_token"]


@pytest.mark.asyncio
async def test_create_provider_slug_collision(client):
    """Creating two providers with the same name → second gets a suffix."""
    for _ in range(2):
        await client.post(
            "/providers",
            headers=AUTH,
            json={"name": "Dupe", "base_url": "http://x.test", "bearer_token": "t"},
        )
    resp = await client.get("/providers", headers=AUTH)
    ids = [p["id"] for p in resp.json()]
    assert "dupe" in ids
    assert "dupe-1" in ids


@pytest.mark.asyncio
async def test_create_provider_unauthorized(client):
    resp = await client.post(
        "/providers", json={"name": "x", "base_url": "y", "bearer_token": "z"}
    )
    assert resp.status_code == 401


# ── PUT /providers/{id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_provider(client):
    resp = await client.put(
        "/providers/test-provider",
        headers=AUTH,
        json={"default_model": "fake:model-v2"},
    )
    assert resp.status_code == 200
    assert resp.json()["default_model"] == "fake:model-v2"


@pytest.mark.asyncio
async def test_update_provider_not_found(client):
    resp = await client.put(
        "/providers/nonexistent",
        headers=AUTH,
        json={"default_model": "x"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_provider_id_immutable(client):
    """Updating the 'id' field should be silently ignored."""
    await client.put(
        "/providers/test-provider",
        headers=AUTH,
        json={"id": "hacked-id"},
    )
    resp = await client.get("/providers", headers=AUTH)
    ids = [p["id"] for p in resp.json()]
    assert "test-provider" in ids
    assert "hacked-id" not in ids


@pytest.mark.asyncio
async def test_update_provider_base_url_change_is_audited(
    client, tmp_path, monkeypatch
):
    """TM-004: a base_url change must emit a dedicated audit event with old/new."""
    import json as _json

    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")

    resp = await client.put(
        "/providers/test-provider",
        headers=AUTH,
        json={"base_url": "https://new.example/v1"},
    )
    assert resp.status_code == 200

    entries = audit_mod.get_audit_log(action="provider.base_url.change")
    assert len(entries) == 1
    payload = _json.loads(entries[0]["details"])
    assert payload["new"] == "https://new.example/v1"
    assert payload["old"] != payload["new"]


@pytest.mark.asyncio
async def test_update_provider_same_base_url_not_audited(client, tmp_path, monkeypatch):
    """No base_url audit event when the submitted value equals the existing one."""
    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")

    current = await client.get("/providers", headers=AUTH)
    base_url = next(p["base_url"] for p in current.json() if p["id"] == "test-provider")

    resp = await client.put(
        "/providers/test-provider",
        headers=AUTH,
        json={"base_url": base_url},
    )
    assert resp.status_code == 200
    assert audit_mod.get_audit_log(action="provider.base_url.change") == []


# ── DELETE /providers/{id} ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_provider(client):
    resp = await client.delete("/providers/test-provider", headers=AUTH)
    assert resp.status_code == 204

    resp2 = await client.get("/providers", headers=AUTH)
    ids = [p["id"] for p in resp2.json()]
    assert "test-provider" not in ids


@pytest.mark.asyncio
async def test_delete_provider_not_found(client):
    resp = await client.delete("/providers/ghost", headers=AUTH)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_provider_unauthorized(client):
    resp = await client.delete("/providers/test-provider")
    assert resp.status_code == 401


# ── Auth disabled ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_disabled_when_api_key_empty(_providers_file, monkeypatch):
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")
    from plugboard.app import app
    from httpx import AsyncClient, ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        # /v1 remains accessible when client auth is disabled.
        resp_v1 = await c.get("/v1/models")
        # Admin endpoints are open when BOTH keys are empty (dev mode).
        resp_admin = await c.get("/providers")
    assert resp_v1.status_code == 200
    assert resp_admin.status_code == 200


@pytest.mark.asyncio
async def test_auth_disabled_when_api_key_empty_even_with_admin(
    _providers_file, monkeypatch
):
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    from plugboard.app import app
    from httpx import AsyncClient, ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        # /v1 endpoints should remain accessible when client auth is disabled.
        resp = await c.get("/v1/models")
        resp_admin = await c.get("/providers")

    assert resp.status_code == 200
    assert resp_admin.status_code == 401
