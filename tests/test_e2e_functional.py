"""
End-to-end functional tests — full user journeys through the API.

Each journey simulates a realistic sequence of admin and client actions.
All external LLM provider calls are mocked with aioresponses.
All persistence (usage, audit, cache, api_keys) is redirected to tmp_path
so tests are fully isolated.
"""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses
from httpx import ASGITransport, AsyncClient

from tests.factories import FAKE_COMPLETION, PROVIDER_A

AUTH_ADMIN = {"Authorization": "Bearer adminkey"}
AUTH_CLIENT_LEGACY = {"Authorization": "Bearer testkey"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolate_data(tmp_path, monkeypatch):
    """Redirect ALL data modules to a temporary directory so tests are isolated."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Providers
    import plugboard.config as cfg

    pfile = data_dir / "providers.json"
    pfile.write_text(json.dumps({"providers": [PROVIDER_A]}))
    monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
    monkeypatch.setattr(cfg, "PROVIDERS_FILE", pfile)
    monkeypatch.setattr(cfg, "PROVIDERS_LOCK", data_dir / "providers.json.lock")

    # API keys
    import plugboard.apikeys as ak

    keys_file = data_dir / "api_keys.json"
    keys_file.write_text(json.dumps({"keys": []}))
    monkeypatch.setattr(ak, "_DATA_DIR", data_dir)
    monkeypatch.setattr(ak, "KEYS_FILE", keys_file)
    monkeypatch.setattr(ak, "KEYS_LOCK", data_dir / "api_keys.json.lock")

    # Usage (SQLite)
    import plugboard.usage as usage_mod

    monkeypatch.setattr(usage_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(usage_mod, "_DB_PATH", data_dir / "usage.db")

    # Audit (SQLite)
    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(audit_mod, "_DB_PATH", data_dir / "audit.db")

    # Cache (SQLite)
    import plugboard.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(cache_mod, "_DB_PATH", data_dir / "cache.db")

    # Monitoring (SQLite)
    import plugboard.monitoring as mon_mod

    monkeypatch.setattr(mon_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(mon_mod, "_DB_PATH", data_dir / "monitoring.db")

    # Billing / pricing
    import plugboard.billing as bill_mod

    monkeypatch.setattr(bill_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(bill_mod, "_PRICING_DIR", data_dir / "pricing")

    # Rate limit off
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 0)

    # Auth: admin key vs client key
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    monkeypatch.setattr("plugboard.app._API_KEY", "testkey")

    # Invalidate models cache so tests see fresh provider data
    from plugboard.router import invalidate_models_cache

    invalidate_models_cache()

    return data_dir


@pytest.fixture()
async def api(_isolate_data):
    """Async HTTPX client bound to the FastAPI app."""
    from plugboard.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _current_month():
    from plugboard.billing import current_pricing_month

    return current_pricing_month()


# ===========================================================================
# Journey 1: Admin setup
# ===========================================================================


async def test_e2e_admin_creates_provider(api):
    """POST /providers creates a provider and GET /providers returns it."""
    resp = await api.post(
        "/providers",
        headers=AUTH_ADMIN,
        json={
            "name": "New Provider",
            "base_url": "http://new-llm.test",
            "bearer_token": "tok-new-secret",
            "default_model": "new:model-v1",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "New Provider"
    assert body["base_url"] == "http://new-llm.test"
    # Token is masked
    assert "tok-new-secret" not in body["bearer_token"]
    provider_id = body["id"]

    # Verify it appears in list
    resp2 = await api.get("/providers", headers=AUTH_ADMIN)
    assert resp2.status_code == 200
    ids = [p["id"] for p in resp2.json()]
    assert provider_id in ids


async def test_e2e_admin_discovers_models(api):
    """POST /providers/{id}/discover fetches models from the provider."""
    with aioresponses() as mock:
        mock.get(
            "http://fake-llm.test/v1/models",
            payload={
                "data": [
                    {"id": "fake:model-v1", "object": "model"},
                    {"id": "fake:model-v2", "object": "model"},
                ],
            },
        )
        resp = await api.post(
            "/providers/test-provider/discover",
            headers=AUTH_ADMIN,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "test-provider"
    assert body["discovered"] == 2
    assert "fake:model-v1" in body["models"]
    assert "fake:model-v2" in body["models"]


async def test_e2e_admin_sets_pricing(api):
    """PUT /admin/pricing/{month} upserts a pricing grid."""
    month = _current_month()
    resp = await api.put(
        f"/admin/pricing/{month}",
        headers=AUTH_ADMIN,
        json={
            "models": {
                "fake:model-v1": {"input_price": 3.0, "output_price": 15.0},
            },
            "note": "E2E test pricing",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "fake:model-v1" in body["models"]
    assert body["models"]["fake:model-v1"]["input_price"] == 3.0

    # Verify GET returns it
    resp2 = await api.get(f"/admin/pricing/{month}", headers=AUTH_ADMIN)
    assert resp2.status_code == 200
    assert resp2.json()["models"]["fake:model-v1"]["output_price"] == 15.0


async def test_e2e_admin_creates_client_key(api):
    """POST /admin/keys creates a client key with limits."""
    resp = await api.post(
        "/admin/keys",
        headers=AUTH_ADMIN,
        json={
            "client_id": "acme-corp",
            "label": "ACME production key",
            "monthly_limit_usd": 50.0,
            "rpm_limit": 100,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"] == "acme-corp"
    assert body["monthly_limit_usd"] == 50.0
    assert "key" in body  # plaintext key shown only once
    assert body["key"].startswith("pb_")
    assert body["is_active"] is True


# ===========================================================================
# Journey 2: Client usage
# ===========================================================================


async def _create_client_key(api, client_id="test-client", **kwargs):
    """Helper: create a key and return (plaintext_key, key_id)."""
    payload = {"client_id": client_id, "label": "e2e", **kwargs}
    resp = await api.post("/admin/keys", headers=AUTH_ADMIN, json=payload)
    assert resp.status_code == 201
    body = resp.json()
    return body["key"], body["key_id"]


async def test_e2e_client_lists_models(api):
    """GET /v1/models with a valid client key returns model list."""
    key, _ = await _create_client_key(api)
    resp = await api.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert len(body["data"]) >= 1
    model_ids = [m["id"] for m in body["data"]]
    # Models are prefixed with provider id (e.g. "test-provider:fake:model-v1")
    assert any("fake:model-v1" in mid for mid in model_ids)


async def test_e2e_client_sends_completion(api):
    """POST /v1/chat/completions with a client key proxies to the provider."""
    # Setup: pricing so billing works
    month = _current_month()
    await api.put(
        f"/admin/pricing/{month}",
        headers=AUTH_ADMIN,
        json={
            "models": {"fake:model-v1": {"input_price": 3.0, "output_price": 15.0}},
            "note": "",
        },
    )
    key, _ = await _create_client_key(api, client_id="e2e-user")

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        resp = await api.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == "Hello!"
    # Billing info injected
    assert "billing" in body
    assert body["billing"]["model"] == "fake:model-v1"
    assert body["billing"]["cost_usd"] >= 0


async def test_e2e_client_checks_usage(api):
    """GET /usage/me returns logged usage after a completion."""
    month = _current_month()
    await api.put(
        f"/admin/pricing/{month}",
        headers=AUTH_ADMIN,
        json={
            "models": {"fake:model-v1": {"input_price": 3.0, "output_price": 15.0}},
            "note": "",
        },
    )
    key, _ = await _create_client_key(api, client_id="usage-checker")

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        await api.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

    resp = await api.get("/usage/me", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == "usage-checker"
    assert len(body["entries"]) >= 1
    assert body["entries"][0]["model"] == "fake:model-v1"


async def test_e2e_client_checks_info(api):
    """GET /v1/me returns client info with limit and remaining."""
    month = _current_month()
    await api.put(
        f"/admin/pricing/{month}",
        headers=AUTH_ADMIN,
        json={
            "models": {"fake:model-v1": {"input_price": 3.0, "output_price": 15.0}},
            "note": "",
        },
    )
    key, _ = await _create_client_key(
        api, client_id="info-client", monthly_limit_usd=100.0
    )

    resp = await api.get("/v1/me", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == "info-client"
    assert body["monthly_limit_usd"] == 100.0
    assert body["remaining_usd"] is not None
    assert body["remaining_usd"] <= 100.0
    assert "usage" in body


# ===========================================================================
# Journey 3: Admin monitoring
# ===========================================================================


async def test_e2e_admin_checks_usage(api):
    """GET /admin/usage/{month} shows client usage after completions."""
    month = _current_month()
    await api.put(
        f"/admin/pricing/{month}",
        headers=AUTH_ADMIN,
        json={
            "models": {"fake:model-v1": {"input_price": 3.0, "output_price": 15.0}},
            "note": "",
        },
    )
    key, _ = await _create_client_key(api, client_id="monitored-client")

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        await api.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    resp = await api.get(f"/admin/usage/{month}", headers=AUTH_ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    client_ids = [e["client_id"] for e in body["entries"]]
    assert "monitored-client" in client_ids


async def test_e2e_admin_exports_csv(api):
    """GET /admin/usage/{month}/export?format=csv returns CSV data."""
    month = _current_month()
    await api.put(
        f"/admin/pricing/{month}",
        headers=AUTH_ADMIN,
        json={
            "models": {"fake:model-v1": {"input_price": 3.0, "output_price": 15.0}},
            "note": "",
        },
    )
    key, _ = await _create_client_key(api, client_id="csv-client")

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        await api.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    resp = await api.get(
        f"/admin/usage/{month}/export",
        headers=AUTH_ADMIN,
        params={"format": "csv"},
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")
    text = resp.text
    # CSV should have a header and at least one data row
    lines = [line for line in text.strip().split("\n") if line]
    assert len(lines) >= 2
    assert "client_id" in lines[0]
    assert "csv-client" in text


async def test_e2e_admin_checks_audit(api):
    """GET /admin/audit shows recent admin actions."""
    # Trigger an auditable action (create a key)
    await api.post(
        "/admin/keys",
        headers=AUTH_ADMIN,
        json={"client_id": "audit-test", "label": "audit"},
    )

    resp = await api.get("/admin/audit", headers=AUTH_ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    actions = [e["action"] for e in body["entries"]]
    assert "key.create" in actions


async def test_e2e_admin_checks_monitoring(api):
    """GET /admin/monitoring returns health report."""
    with aioresponses() as mock:
        # Mock the provider health endpoint
        mock.get("http://fake-llm.test/health", payload={"ok": True}, status=200)
        resp = await api.get("/admin/monitoring", headers=AUTH_ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert "providers" in body
    assert "summary" in body
    assert body["summary"]["total_providers"] >= 1


# ===========================================================================
# Journey 4: Key lifecycle
# ===========================================================================


async def test_e2e_key_rotate(api):
    """Create key, rotate it: old key fails, new key works."""
    key_old, key_id_old = await _create_client_key(api, client_id="rotate-client")

    # Verify old key works
    resp = await api.get("/v1/models", headers={"Authorization": f"Bearer {key_old}"})
    assert resp.status_code == 200

    # Rotate
    resp = await api.post(
        f"/admin/keys/{key_id_old}/rotate",
        headers=AUTH_ADMIN,
    )
    assert resp.status_code == 201
    body = resp.json()
    key_new = body["key"]
    assert key_new != key_old
    assert key_new.startswith("pb_")

    # Old key should now fail (revoked)
    resp_old = await api.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {key_old}"},
    )
    assert resp_old.status_code == 401

    # New key should work
    resp_new = await api.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {key_new}"},
    )
    assert resp_new.status_code == 200


async def test_e2e_key_revoke(api):
    """Create key, revoke it, verify 401."""
    key, key_id = await _create_client_key(api, client_id="revoke-client")

    # Key works
    resp = await api.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200

    # Revoke
    resp_revoke = await api.post(
        f"/admin/keys/{key_id}/revoke",
        headers=AUTH_ADMIN,
    )
    assert resp_revoke.status_code == 200
    assert resp_revoke.json()["is_active"] is False

    # Key should now fail
    resp_after = await api.get(
        "/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp_after.status_code == 401


# ===========================================================================
# Journey 5: Multi-org
# ===========================================================================


async def test_e2e_org_keys(api):
    """Create keys with org_id, GET /admin/orgs lists them."""
    await _create_client_key(api, client_id="org-user-1", org_id="acme")
    await _create_client_key(api, client_id="org-user-2", org_id="acme")
    await _create_client_key(api, client_id="solo-user", org_id="solo-org")

    resp = await api.get("/admin/orgs", headers=AUTH_ADMIN)
    assert resp.status_code == 200
    orgs = resp.json()
    org_ids = [o["org_id"] for o in orgs]
    assert "acme" in org_ids
    assert "solo-org" in org_ids

    acme = next(o for o in orgs if o["org_id"] == "acme")
    assert acme["keys"] == 2
    assert acme["client_count"] == 2
    assert "org-user-1" in acme["clients"]
    assert "org-user-2" in acme["clients"]


async def test_e2e_org_usage(api):
    """GET /admin/orgs/{org_id}/usage/{month} shows aggregated usage."""
    month = _current_month()
    await api.put(
        f"/admin/pricing/{month}",
        headers=AUTH_ADMIN,
        json={
            "models": {"fake:model-v1": {"input_price": 3.0, "output_price": 15.0}},
            "note": "",
        },
    )
    key, _ = await _create_client_key(api, client_id="org-member", org_id="org-x")

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        await api.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    resp = await api.get(f"/admin/orgs/org-x/usage/{month}", headers=AUTH_ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["org_id"] == "org-x"
    assert body["month"] == month
    assert "org-member" in body["clients"]
    assert len(body["entries"]) >= 1
    assert body["summary"]["total_cost_usd"] >= 0


# ===========================================================================
# Journey 6: v2 API
# ===========================================================================


async def test_e2e_v2_models(api):
    """GET /v2/models returns api_version field."""
    key, _ = await _create_client_key(api)
    resp = await api.get("/v2/models", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_version"] == "2"
    assert body["object"] == "list"
    assert len(body["data"]) >= 1


async def test_e2e_v2_benchmarks(api):
    """GET /v2/models/benchmarks returns enriched data + api_version."""
    key, _ = await _create_client_key(api)
    resp = await api.get(
        "/v2/models/benchmarks",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_version"] == "2"
    assert body["object"] == "list"
    # Data should exist (may or may not have benchmark info depending on model)
    assert isinstance(body["data"], list)
