"""Tests for billing and usage modules."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from tests.factories import (
    FAKE_COMPLETION,
    FAKE_PRICING,
    FAKE_STREAM_CHUNK_1,
    FAKE_STREAM_CHUNK_2,
    FAKE_STREAM_DONE,
)

AUTH = {"Authorization": "Bearer testkey"}
ADMIN = {"Authorization": "Bearer testkey"}
_MODEL = "test-provider:fake:model-v1"


# ── billing.py unit tests ─────────────────────────────────────────────────────


def test_current_pricing_month():
    from plugboard.billing import current_pricing_month

    m = current_pricing_month()
    assert len(m) == 7
    assert "_" in m  # "YYYY_MM"


def test_resolve_model_price_exact():
    from plugboard.billing import resolve_model_price

    pricing = {"models": {"gpt-4o": {"input_price": 2.5, "output_price": 10.0}}}
    entry = resolve_model_price("gpt-4o", pricing)
    assert entry["input_price"] == 2.5


def test_resolve_model_price_suffix():
    from plugboard.billing import resolve_model_price

    pricing = {
        "models": {"claude-sonnet-4-6": {"input_price": 3.0, "output_price": 15.0}}
    }
    entry = resolve_model_price("openclaw:anthropic--claude-sonnet-4-6", pricing)
    assert entry is not None
    assert entry["output_price"] == 15.0


def test_resolve_model_price_no_match():
    from plugboard.billing import resolve_model_price

    pricing = {"models": {"gpt-4o": {"input_price": 2.5, "output_price": 10.0}}}
    assert resolve_model_price("unknown-model", pricing) is None


def test_calculate_cost_known_price():
    from plugboard.billing import calculate_cost

    price = {"input_price": 3.0, "output_price": 15.0}
    cost = calculate_cost(1_000_000, 500_000, price)
    assert cost == pytest.approx(3.0 + 7.5)  # $10.50


def test_calculate_cost_no_price():
    from plugboard.billing import calculate_cost

    assert calculate_cost(1000, 500, None) == 0.0


def test_save_and_load_pricing(tmp_path, monkeypatch):
    import plugboard.billing as b

    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    b.save_pricing("2026_03", dict(FAKE_PRICING))
    loaded = b.load_pricing("2026_03")
    assert loaded["month"] == "2026_03"
    assert "fake:model-v1" in loaded["models"]


def test_load_pricing_missing_month(tmp_path, monkeypatch):
    import plugboard.billing as b

    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    assert b.load_pricing("1999_01") == {}


# ── usage.py unit tests ───────────────────────────────────────────────────────


def test_append_and_get_usage(tmp_path, monkeypatch):
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")
    entry = u.append_usage(
        client_id="test-client",
        model="fake:model-v1",
        provider_id="test-provider",
        prompt_tokens=100,
        completion_tokens=50,
        pricing_month="2026_03",
        cost_usd=0.005,
    )
    assert entry["client_id"] == "test-client"
    assert entry["total_tokens"] == 150

    entries = u.get_usage("2026_03")
    assert len(entries) == 1
    assert entries[0]["cost_usd"] == 0.005


def test_get_usage_by_client(tmp_path, monkeypatch):
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")
    u.append_usage("client-a", "m", "p", 10, 5, "2026_03", 0.0)
    u.append_usage("client-b", "m", "p", 10, 5, "2026_03", 0.0)
    u.append_usage("client-a", "m", "p", 20, 10, "2026_03", 0.0)
    assert len(u.get_usage_by_client("2026_03", "client-a")) == 2
    assert len(u.get_usage_by_client("2026_03", "client-b")) == 1


def test_summarize_usage():
    from plugboard.usage import summarize_usage

    entries = [
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost_usd": 0.01,
            "model": "m1",
        },
        {
            "prompt_tokens": 200,
            "completion_tokens": 100,
            "total_tokens": 300,
            "cost_usd": 0.02,
            "model": "m1",
        },
    ]
    s = summarize_usage(entries)
    assert s["total_entries"] == 2
    assert s["total_tokens"] == 450
    assert s["total_cost_usd"] == pytest.approx(0.03)
    assert s["by_model"]["m1"]["entries"] == 2


# ── apikeys.py unit tests ─────────────────────────────────────────────────────


def test_create_and_get_key(tmp_path, monkeypatch):
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    record = ak.create_key("acme", "test key")
    assert record["client_id"] == "acme"
    assert record["key"].startswith("pb_")
    found = ak.get_key_record(record["key"])
    assert found is not None
    assert found["client_id"] == "acme"


def test_revoke_key(tmp_path, monkeypatch):
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    rec = ak.create_key("client", "")
    assert ak.revoke_key(rec["key_id"]) is True
    assert ak.get_key_record(rec["key"]) is None


def test_delete_key(tmp_path, monkeypatch):
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    rec = ak.create_key("client", "")
    assert ak.delete_key(rec["key_id"]) is True
    assert ak.delete_key(rec["key_id"]) is False


def test_legacy_api_key_fallback_removed_in_v3(tmp_path, monkeypatch):
    """v3.0.0 BREAKING (ADR-034): the synthetic record built from the
    `API_KEY` env var when `api_keys.json` is empty/absent has been
    removed. Empty file = zero keys; `get_key_record(env_key)` → None.
    """
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    ak._invalidate_cache()
    monkeypatch.setenv("API_KEY", "legacykey")
    assert ak.get_key_record("legacykey") is None


# ── API integration: admin keys endpoints ─────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_list_keys(client):
    resp = await client.post(
        "/admin/keys",
        headers=AUTH,
        json={"client_id": "new-client", "label": "Test"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"] == "new-client"
    assert body["key"].startswith("pb_")

    resp2 = await client.get("/admin/keys", headers=AUTH)
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_revoke_key_endpoint(client):
    resp = await client.post(
        "/admin/keys",
        headers=AUTH,
        json={"client_id": "revoke-test", "label": ""},
    )
    key_id = resp.json()["key_id"]
    resp2 = await client.post(f"/admin/keys/{key_id}/revoke", headers=AUTH)
    assert resp2.status_code == 200
    assert resp2.json()["is_active"] is False


@pytest.mark.asyncio
async def test_delete_key_endpoint(client):
    resp = await client.post(
        "/admin/keys",
        headers=AUTH,
        json={"client_id": "del-test", "label": ""},
    )
    key_id = resp.json()["key_id"]
    resp2 = await client.delete(f"/admin/keys/{key_id}", headers=AUTH)
    assert resp2.status_code == 204


# ── API integration: discover models endpoint ─────────────────────────────────


@pytest.mark.asyncio
async def test_discover_models_endpoint(client):
    """POST /providers/{id}/discover should find models and save them."""
    fake_models = {"data": [{"id": "discovered-model-a"}, {"id": "discovered-model-b"}]}
    with aioresponses() as mock:
        mock.get(
            "http://fake-llm.test/v1/models",
            payload=fake_models,
            status=200,
            content_type="application/json",
        )
        resp = await client.post("/providers/test-provider/discover", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["discovered"] == 2
    assert "discovered-model-a" in body["models"]

    # Verify models now appear in /v1/models
    resp2 = await client.get("/v1/models", headers=AUTH)
    ids = [m["id"] for m in resp2.json()["data"]]
    assert "test-provider:discovered-model-a" in ids


@pytest.mark.asyncio
async def test_discover_models_not_found(client):
    resp = await client.post("/providers/nonexistent/discover", headers=AUTH)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_discover_models_chat_fallback(client):
    """If /v1/models returns HTML, fall back to chat discovery."""
    chat_response = {
        "choices": [
            {"message": {"content": "anthropic--claude-sonnet-4-6\nopenai--gpt-4o"}}
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
        resp = await client.post("/providers/test-provider/discover", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["discovered"] == 2


# ── API integration: pricing endpoints ───────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_and_get_pricing(client, tmp_path, monkeypatch):
    import plugboard.billing as b

    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    resp = await client.put(
        "/admin/pricing/2026_03",
        headers=AUTH,
        json={
            "models": {"gpt-4o": {"input_price": 2.5, "output_price": 10.0}},
            "note": "test",
        },
    )
    assert resp.status_code == 200
    assert "gpt-4o" in resp.json()["models"]

    resp2 = await client.get("/admin/pricing/2026_03", headers=AUTH)
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_pricing_not_found(client, tmp_path, monkeypatch):
    import plugboard.billing as b

    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    resp = await client.get("/admin/pricing/1999_01", headers=AUTH)
    assert resp.status_code == 404


# ── API integration: usage endpoints ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_usage_me_empty(client):
    resp = await client.get("/usage/me", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "entries" in body
    assert "summary" in body


@pytest.mark.asyncio
async def test_admin_usage_endpoint(client):
    resp = await client.get("/admin/usage/2026_03", headers=AUTH)
    assert resp.status_code == 200
    assert "entries" in resp.json()


# ── API integration: billing on completions ───────────────────────────────────


@pytest.mark.asyncio
async def test_completions_logs_usage(client, tmp_path, monkeypatch):
    """A successful completion should log a usage entry."""
    import plugboard.usage as u
    import plugboard.billing as b
    from aioresponses import aioresponses

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": _MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        )
    assert resp.status_code == 200

    entries = u.get_usage("2026_03")
    assert len(entries) == 1
    assert entries[0]["prompt_tokens"] == 5
    assert entries[0]["completion_tokens"] == 2


# ── Phase 5: streaming billing ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_streaming_bills_tokens(client, tmp_path, monkeypatch):
    """Streaming completion should log usage with non-zero token counts."""
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")

    # Build SSE byte stream from factory chunks
    chunks = [FAKE_STREAM_CHUNK_1, FAKE_STREAM_CHUNK_2, FAKE_STREAM_DONE]

    async def fake_stream(body):
        for c in chunks:
            yield c

    # Patch on the app module where forward_stream is imported
    monkeypatch.setattr("plugboard.app.forward_stream", fake_stream)

    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": _MODEL,
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    # Consume streaming body
    _ = resp.content

    entries = u.get_usage("2026_03")
    assert len(entries) == 1
    assert entries[0]["prompt_tokens"] > 0
    assert entries[0]["completion_tokens"] > 0


# ── Phase 5: model access control ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_access_allowed(client, tmp_path, monkeypatch):
    """Client with allowed_models=['fake'] can access fake:model-v1."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    rec = ak.create_key("test-client", "test", allowed_models=["fake"])

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {rec['key']}"},
            json={"model": _MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_model_access_denied(client, tmp_path, monkeypatch):
    """Client with allowed_models=['gpt'] gets 403 on fake:model-v1."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    rec = ak.create_key("test-client", "test", allowed_models=["gpt"])

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {rec['key']}"},
        json={"model": _MODEL, "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_model_access_empty_allows_all(client, tmp_path, monkeypatch):
    """Client with allowed_models=[] can access anything."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    rec = ak.create_key("test-client", "test", allowed_models=[])

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {rec['key']}"},
            json={"model": _MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_model_access_enforced_per_calling_key(client, tmp_path, monkeypatch):
    """When a client has multiple keys, policies are enforced on the calling key."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")

    # Same client, two different policies.
    _unrestricted = ak.create_key("shared-client", "unrestricted", allowed_models=[])
    restricted = ak.create_key("shared-client", "restricted", allowed_models=["gpt"])

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {restricted['key']}"},
        json={"model": _MODEL, "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rpm_limit_enforced_per_calling_key(client, tmp_path, monkeypatch):
    """rpm_limit must be applied per key, not by the first key for the same client."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")

    limited = ak.create_key("shared-rpm", "limited", rpm_limit=1)
    _unlimited = ak.create_key("shared-rpm", "unlimited", rpm_limit=0)

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        first = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {limited['key']}"},
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": "Hi"}],
                "cache": False,
            },
        )
        second = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {limited['key']}"},
            json={
                "model": _MODEL,
                "messages": [{"role": "user", "content": "Hi again"}],
                "cache": False,
            },
        )

    assert first.status_code == 200
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_spending_limit_enforced_per_calling_key(client, tmp_path, monkeypatch):
    """monthly_limit_usd must be evaluated against usage of the calling key."""
    import plugboard.apikeys as ak
    import plugboard.billing as b
    import plugboard.usage as u

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")

    limited = ak.create_key("shared-limit", "limited", monthly_limit_usd=1.0)
    unlimited = ak.create_key("shared-limit", "unlimited", monthly_limit_usd=0.0)

    u.append_usage(
        client_id="shared-limit",
        model="fake:model-v1",
        provider_id="test-provider",
        prompt_tokens=100,
        completion_tokens=50,
        pricing_month="2026_03",
        cost_usd=1.5,
        key_id=limited["key_id"],
    )

    blocked = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {limited['key']}"},
        json={"model": _MODEL, "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert blocked.status_code == 429

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        allowed = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {unlimited['key']}"},
            json={"model": _MODEL, "messages": [{"role": "user", "content": "Hi"}]},
        )
    assert allowed.status_code == 200


# ── Phase 5: usage pagination ────────────────────────────────────────────────


def test_usage_pagination(tmp_path, monkeypatch):
    """Add 5 entries, request page=1 limit=2, verify 2 returned with total=5."""
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")

    for i in range(5):
        u.append_usage(f"client-{i}", "m", "p", 10, 5, "2026_03", 0.001)

    entries, total = u.get_usage_paginated("2026_03", page=1, limit=2)
    assert len(entries) == 2
    assert total == 5

    entries2, total2 = u.get_usage_paginated("2026_03", page=3, limit=2)
    assert len(entries2) == 1  # 5th entry
    assert total2 == 5


@pytest.mark.asyncio
async def test_admin_usage_pagination_endpoint(client, tmp_path, monkeypatch):
    """Admin usage endpoint returns pagination metadata."""
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")

    for i in range(3):
        u.append_usage(f"client-{i}", "m", "p", 10, 5, "2026_03", 0.001)

    resp = await client.get("/admin/usage/2026_03?page=1&limit=2", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"] == 1
    assert body["limit"] == 2
    assert body["total"] == 3
    assert len(body["entries"]) == 2
    # Summary should reflect ALL entries, not just the page
    assert body["summary"]["total_entries"] == 3


# ── Phase 5: delete usage ────────────────────────────────────────────────────


def test_delete_usage(tmp_path, monkeypatch):
    """Create usage, delete it, verify gone."""
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")

    u.append_usage("c", "m", "p", 10, 5, "2026_03", 0.0)
    assert len(u.get_usage("2026_03")) == 1

    assert u.delete_usage("2026_03") is True
    assert len(u.get_usage("2026_03")) == 0
    # Second delete returns False
    assert u.delete_usage("2026_03") is False


@pytest.mark.asyncio
async def test_delete_usage_endpoint(client, tmp_path, monkeypatch):
    """DELETE /admin/usage/{month} returns 204 on success, 404 when missing."""
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")

    u.append_usage("c", "m", "p", 10, 5, "2026_03", 0.0)

    resp = await client.delete("/admin/usage/2026_03", headers=AUTH)
    assert resp.status_code == 204

    resp2 = await client.delete("/admin/usage/2026_03", headers=AUTH)
    assert resp2.status_code == 404


# ── Phase 5: models cache ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_models_cache_returns_same(_providers_file):
    """Call list_models twice quickly, verify same result (no errors)."""
    from plugboard.router import invalidate_models_cache, list_models

    invalidate_models_cache()
    first = await list_models()
    second = await list_models()
    assert first == second
    assert len(first) >= 2


# ── Phase 6: client portal — /v1/me endpoint ─────────────────────────────────


@pytest.mark.asyncio
async def test_client_info_endpoint(client, tmp_path, monkeypatch):
    """GET /v1/me with a client key returns client_id, usage, remaining_usd."""
    import plugboard.apikeys as ak
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")

    rec = ak.create_key("portal-test", "Portal test key", monthly_limit_usd=10.0)

    # Add some usage
    u.append_usage(
        "portal-test", "m1", "p", 100, 50, "2026_03", 2.5, key_id=rec["key_id"]
    )

    resp = await client.get("/v1/me", headers={"Authorization": f"Bearer {rec['key']}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == "portal-test"
    assert body["monthly_limit_usd"] == 10.0
    assert "usage" in body
    assert body["usage"]["total_entries"] == 1
    assert body["remaining_usd"] == pytest.approx(7.5)
    assert body["current_month"] == "2026_03"


@pytest.mark.asyncio
async def test_client_info_uses_calling_key_scope(client, tmp_path, monkeypatch):
    """/v1/me should report limits and usage for the calling key when key_id is known."""
    import plugboard.apikeys as ak
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")

    k1 = ak.create_key("portal-shared", "k1", monthly_limit_usd=10.0, rpm_limit=10)
    k2 = ak.create_key("portal-shared", "k2", monthly_limit_usd=3.0, rpm_limit=1)
    u.append_usage(
        "portal-shared", "m1", "p", 10, 5, "2026_03", 2.5, key_id=k1["key_id"]
    )

    resp = await client.get("/v1/me", headers={"Authorization": f"Bearer {k2['key']}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == "portal-shared"
    assert body["key_id"] == k2["key_id"]
    assert body["monthly_limit_usd"] == 3.0
    assert body["rpm_limit"] == 1
    # Usage should be scoped to key2, so key1 usage must not leak here.
    assert body["usage"]["total_entries"] == 0
    assert body["remaining_usd"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_client_info_unauthorized(client, monkeypatch):
    """GET /v1/me without a key returns 401."""
    monkeypatch.setattr("plugboard.app._API_KEY", "testkey")
    resp = await client.get("/v1/me")
    assert resp.status_code == 401


# ── Phase 5: purge old usage ────────────────────────────────────────────────


def test_purge_old_usage(tmp_path, monkeypatch):
    """purge_old_usage deletes entries older than keep_months from SQLite."""
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")

    # Insert entries for old months and a recent month
    for month_val in ["2024_01", "2024_06", "2026_04"]:
        monkeypatch.setattr(b, "current_pricing_month", lambda m=month_val: m)
        u.append_usage("c", "m", "p", 10, 5, month_val, 0.0)

    deleted = u.purge_old_usage(keep_months=6)
    assert "2024_01" in deleted
    assert "2024_06" in deleted
    assert "2026_04" not in deleted
    # Verify old entries are gone, recent remain
    assert len(u.get_usage("2024_01")) == 0
    assert len(u.get_usage("2026_04")) == 1


def test_migrate_json_to_sqlite(tmp_path, monkeypatch):
    """migrate_json_to_sqlite imports existing JSON files into SQLite."""
    import json
    import plugboard.usage as u

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")

    # Create a fake JSON usage file
    data = {
        "month": "2026_03",
        "entries": [
            {
                "id": "abc123",
                "ts": "2026-03-15T10:00:00.000Z",
                "client_id": "test-mig",
                "key_id": "k1",
                "model": "m1",
                "provider_id": "p1",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "pricing_month": "2026_03",
                "cost_usd": 0.01,
            },
            {
                "id": "def456",
                "ts": "2026-03-16T10:00:00.000Z",
                "client_id": "test-mig",
                "key_id": "k2",
                "model": "m2",
                "provider_id": "p1",
                "prompt_tokens": 200,
                "completion_tokens": 100,
                "total_tokens": 300,
                "pricing_month": "2026_03",
                "cost_usd": 0.02,
            },
        ],
    }
    (tmp_path / "usage_2026_03.json").write_text(json.dumps(data))

    count = u.migrate_json_to_sqlite()
    assert count == 2
    entries = u.get_usage("2026_03")
    assert len(entries) == 2
    assert entries[0]["client_id"] == "test-mig"

    # Idempotent — running again should not duplicate
    count2 = u.migrate_json_to_sqlite()
    assert count2 == 2  # INSERT OR IGNORE
    assert len(u.get_usage("2026_03")) == 2


def test_purge_old_usage_nothing_to_delete(tmp_path, monkeypatch):
    """purge_old_usage with no old files returns empty list."""
    import plugboard.usage as u

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    deleted = u.purge_old_usage(keep_months=12)
    assert deleted == []


@pytest.mark.asyncio
async def test_purge_endpoint(client, tmp_path, monkeypatch):
    """POST /admin/usage/purge returns purged list."""
    import plugboard.usage as u
    import plugboard.billing as b

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")

    # Insert an old entry directly into SQLite
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2020_01")
    u.append_usage("c", "m", "p", 10, 5, "2020_01", 0.0)

    resp = await client.post("/admin/usage/purge?keep_months=6", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "2020_01" in body["purged"]


# ── Phase 6: key rotation ──────────────────────────────────────────────────


def test_rotate_key(tmp_path, monkeypatch):
    """rotate_key revokes old key and creates new one with same settings."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")

    old = ak.create_key(
        "rotate-client",
        "my label",
        monthly_limit_usd=50.0,
        allowed_models=["gpt"],
        rpm_limit=123,
        org_id="org-abc",
    )
    new = ak.rotate_key(old["key_id"])

    assert new is not None
    assert new["client_id"] == "rotate-client"
    assert new["monthly_limit_usd"] == 50.0
    assert new["allowed_models"] == ["gpt"]
    assert new["rpm_limit"] == 123
    assert new["org_id"] == "org-abc"
    assert new["key"] != old["key"]  # new plaintext key
    assert new["key_id"] != old["key_id"]

    # Old key should be revoked
    assert ak.get_key_record(old["key"]) is None
    # New key should work
    assert ak.get_key_record(new["key"]) is not None


def test_rotate_key_not_found(tmp_path, monkeypatch):
    """rotate_key with unknown key_id returns None."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    assert ak.rotate_key("nonexistent") is None


@pytest.mark.asyncio
async def test_rotate_key_endpoint(client, tmp_path, monkeypatch):
    """POST /admin/keys/{id}/rotate returns 201 with new key."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")

    # Create a key first
    resp = await client.post(
        "/admin/keys", headers=AUTH, json={"client_id": "rot-test", "label": "test"}
    )
    old_id = resp.json()["key_id"]

    # Rotate
    resp2 = await client.post(f"/admin/keys/{old_id}/rotate", headers=AUTH)
    assert resp2.status_code == 201
    body = resp2.json()
    assert body["client_id"] == "rot-test"
    assert body["key"].startswith("pb_")
    assert body["key_id"] != old_id


@pytest.mark.asyncio
async def test_rotate_key_endpoint_not_found(client):
    """POST /admin/keys/nonexistent/rotate returns 404."""
    resp = await client.post("/admin/keys/nonexistent/rotate", headers=AUTH)
    assert resp.status_code == 404


# ── Phase 7: health detailed ───────────────────────────────────────────────

# ── Phase 6: multi-keys per client ──────────────────────────────────────────


def test_get_keys_by_client(tmp_path, monkeypatch):
    """get_keys_by_client returns only keys for the given client_id."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")

    ak.create_key("client-a", "prod")
    ak.create_key("client-a", "staging")
    ak.create_key("client-b", "prod")

    keys_a = ak.get_keys_by_client("client-a")
    assert len(keys_a) == 2
    assert all(k["client_id"] == "client-a" for k in keys_a)

    keys_b = ak.get_keys_by_client("client-b")
    assert len(keys_b) == 1


@pytest.mark.asyncio
async def test_admin_keys_by_client_endpoint(client, tmp_path, monkeypatch):
    """GET /admin/keys/client/{id} returns only that client's keys."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")

    await client.post(
        "/admin/keys", headers=AUTH, json={"client_id": "multi-a", "label": "k1"}
    )
    await client.post(
        "/admin/keys", headers=AUTH, json={"client_id": "multi-a", "label": "k2"}
    )
    await client.post(
        "/admin/keys", headers=AUTH, json={"client_id": "multi-b", "label": "k3"}
    )

    resp = await client.get("/admin/keys/client/multi-a", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_client_keys_endpoint(client, tmp_path, monkeypatch):
    """GET /v1/me/keys returns the client's own keys (masked)."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")

    rec = ak.create_key("me-client", "my key")

    resp = await client.get(
        "/v1/me/keys", headers={"Authorization": f"Bearer {rec['key']}"}
    )
    assert resp.status_code == 200
    keys = resp.json()
    assert len(keys) >= 1
    # Key should be masked — no plaintext
    assert all("key" not in k or k.get("key_display") for k in keys)
    assert keys[0]["label"] == "my key"


# ── Phase 7: health detailed ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_detailed_requires_admin(client):
    """GET /health/detailed without admin key returns 401."""
    resp = await client.get("/health/detailed")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_health_detailed(client):
    """GET /health/detailed with admin key returns provider health."""
    from aioresponses import aioresponses

    with aioresponses() as mock:
        mock.get("http://fake-llm.test/health", status=200)
        mock.get("http://other-llm.test/health", status=200)
        resp = await client.get("/health/detailed", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "ok" in body
    assert "providers" in body
    assert "keys_count" in body


# ── resolve_model_price_with_fallback — sentinel snapshot resilience ────────


def test_resolve_with_fallback_uses_current_month_first(tmp_path, monkeypatch):
    """When the current-month snapshot prices the model, no fallback walk."""
    import plugboard.billing as billing_mod

    monkeypatch.setattr(billing_mod, "_PRICING_DIR", tmp_path)
    (tmp_path / "2026_05.json").write_text(
        '{"models": {"claude-opus-9-9": {"input_price": 5.0, "output_price": 25.0}}}'
    )
    monkeypatch.setattr(billing_mod, "current_pricing_month", lambda: "2026_05")
    entry = billing_mod.resolve_model_price_with_fallback("claude-opus-9-9")
    assert entry is not None
    assert entry["input_price"] == 5.0


def test_resolve_with_fallback_walks_back_on_sentinel(tmp_path, monkeypatch):
    """The real-world scenario: pricing scheduler wrote a sentinel
    snapshot for the current month after a LiteLLM outage, so the model
    is only priced in last month's file. Must walk back."""
    import plugboard.billing as billing_mod

    monkeypatch.setattr(billing_mod, "_PRICING_DIR", tmp_path)
    # Current month: sentinel (model not present)
    (tmp_path / "2026_05.json").write_text(
        '{"models": {"some-other-model": {"input_price": 1.0, "output_price": 1.0}}}'
    )
    # Previous month: has the model
    (tmp_path / "2026_04.json").write_text(
        '{"models": {"claude-opus-9-9": {"input_price": 5.0, "output_price": 25.0}}}'
    )
    monkeypatch.setattr(billing_mod, "current_pricing_month", lambda: "2026_05")
    entry = billing_mod.resolve_model_price_with_fallback("claude-opus-9-9")
    assert entry is not None
    assert entry["input_price"] == 5.0


def test_resolve_with_fallback_static_known_prices_when_all_snapshots_dry(
    tmp_path, monkeypatch
):
    """No snapshot can price the model → final stage hits the static
    ``_KNOWN_PRICES``. Verifies the regression-safe path."""
    import plugboard.billing as billing_mod

    monkeypatch.setattr(billing_mod, "_PRICING_DIR", tmp_path)
    monkeypatch.setattr(billing_mod, "current_pricing_month", lambda: "2026_05")
    # claude-opus-4-6 is in pricing_scraper._KNOWN_PRICES → fallback chain reaches it.
    entry = billing_mod.resolve_model_price_with_fallback("claude-opus-4-6")
    assert entry is not None
    assert entry["input_price"] > 0


def test_resolve_with_fallback_returns_none_when_truly_unknown(tmp_path, monkeypatch):
    """Final safeguard: a totally unknown model returns None so the
    caller can decide what to do (warn, default to 0, etc.)."""
    import plugboard.billing as billing_mod

    monkeypatch.setattr(billing_mod, "_PRICING_DIR", tmp_path)
    monkeypatch.setattr(billing_mod, "current_pricing_month", lambda: "2026_05")
    entry = billing_mod.resolve_model_price_with_fallback("totally-fictional-model-zzz")
    assert entry is None


def test_resolve_with_fallback_handles_malformed_month(tmp_path, monkeypatch):
    """Defensive: malformed month must not crash — single best-effort lookup."""
    import plugboard.billing as billing_mod

    monkeypatch.setattr(billing_mod, "_PRICING_DIR", tmp_path)
    entry = billing_mod.resolve_model_price_with_fallback(
        "claude-opus-9-9", month="not-a-month"
    )
    # Returns None gracefully (no file matches, no static price match).
    assert entry is None


def test_key_issued_before_the_rename_still_resolves(tmp_path, monkeypatch):
    # Keys created as "lle_..." before the Plugboard rename stay valid: the
    # lookup is by hash and both prefixes are recognised as client keys.
    import secrets as _secrets

    import plugboard.apikeys as ak
    from plugboard import app as app_module

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    legacy = "lle_" + _secrets.token_hex(32)
    record = ak.create_key("acme", "migrated key")
    data = ak._read()
    data["keys"][0]["key_hash"] = ak._hash_key(legacy)
    ak._write(data)

    assert legacy.startswith(app_module.KEY_PREFIXES)
    found = ak.get_key_record(legacy)
    assert found is not None and found["key_id"] == record["key_id"]
