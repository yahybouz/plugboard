"""Tests for BENCH-007/008/009 — observability & API improvements."""

from __future__ import annotations

import pytest

from plugboard.cli_subprocess import CliSubprocessError
from tests.factories import FAKE_COMPLETION


AUTH = {"Authorization": "Bearer testkey"}


# ── BENCH-007 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_completions_cli_error_detail(client, monkeypatch):
    """Subprocess failure returns enriched 502 with exit_code/stderr/pid."""
    from plugboard import app as app_mod

    exc = CliSubprocessError(
        "CLI gemini exited 1: oops",
        exit_code=1,
        stderr="stderr content" * 10,
        pid=12345,
        provider_name="gemini",
        model_id="gemini-2.5-flash",
    )

    async def _fake_forward(*args, **kwargs):
        raise exc

    monkeypatch.setattr(app_mod, "forward", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "gemini:gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"] == "CLI gemini exited 1: oops"
    assert body["exit_code"] == 1
    assert "stderr" in body
    assert len(body["stderr"]) <= 500
    assert body["pid"] == 12345
    assert body["provider_name"] == "gemini"
    assert body["model_id"] == "gemini-2.5-flash"


@pytest.mark.asyncio
async def test_stream_bypass_cli_error_detail(client, monkeypatch):
    """Stream-bypass subprocess failure also returns enriched 502."""
    from plugboard import app as app_mod
    from plugboard.router import resolve_provider_and_model

    # Force stream + subprocess bypass
    provider, real_model = resolve_provider_and_model("test-provider:fake:model-v1")

    # If provider is HTTP (not subprocess), stream bypass won't trigger cli path.
    # Instead, monkeypatch resolve so it looks like subprocess.
    def _fake_resolve(model_str):
        p, m = resolve_provider_and_model(model_str)
        p["auth_source"] = "subprocess:gemini"
        return p, m

    monkeypatch.setattr(app_mod, "resolve_provider_and_model", _fake_resolve)

    exc = CliSubprocessError(
        "CLI gemini timed out after 10s",
        exit_code=None,
        stderr="",
        pid=None,
        provider_name="gemini",
        model_id="gemini-2.5-flash",
    )

    async def _fake_forward(*args, **kwargs):
        raise exc

    monkeypatch.setattr(app_mod, "forward", _fake_forward)

    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 502
    body = resp.json()
    assert body["detail"] == "CLI gemini timed out after 10s"
    assert body["exit_code"] is None
    assert body["pid"] is None
    assert body["provider_name"] == "gemini"


# ── BENCH-008 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_models_account_compatible_default(client, tmp_path, monkeypatch):
    """When allowed_models is empty, every model is account_compatible."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    rec = ak.create_key("test-client", "test", allowed_models=[])

    resp = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {rec['key']}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    for item in body["data"]:
        assert item.get("account_compatible") is True


@pytest.mark.asyncio
async def test_models_account_compatible_restricted(client, tmp_path, monkeypatch):
    """When allowed_models is set, only matching models are compatible."""
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    rec = ak.create_key("test-client", "test", allowed_models=["fake"])

    resp = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {rec['key']}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    for item in body["data"]:
        mid = item.get("id", "")
        if "fake" in mid:
            assert item["account_compatible"] is True
        else:
            assert item["account_compatible"] is False


# ── BENCH-009 ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preflight_success(client, monkeypatch):
    """Preflight returns ok=True and latency_ms for a reachable model."""
    from plugboard import app as app_mod

    async def _fake_forward(body, timeout=None):
        return FAKE_COMPLETION

    monkeypatch.setattr(app_mod, "forward", _fake_forward)

    resp = await client.post(
        "/v1/preflight",
        headers=AUTH,
        json={
            "items": [
                {"provider": "test-provider", "model": "fake:model-v1"},
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 1
    r = body["results"][0]
    assert r["provider"] == "test-provider"
    assert r["model"] == "fake:model-v1"
    assert r["ok"] is True
    assert isinstance(r["latency_ms"], int)
    assert r["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_preflight_failure(client, monkeypatch):
    """Preflight returns ok=False with a closed error code when forward raises."""
    from plugboard import app as app_mod

    async def _fake_forward(body, timeout=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr(app_mod, "forward", _fake_forward)

    resp = await client.post(
        "/v1/preflight",
        headers=AUTH,
        json={
            "items": [
                {"provider": "test-provider", "model": "fake:model-v1"},
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    r = body["results"][0]
    assert r["ok"] is False
    assert r["error"] == "upstream_error"
    assert "provider down" not in resp.text
    assert isinstance(r["latency_ms"], int)


@pytest.mark.asyncio
async def test_preflight_multiple_items(client, monkeypatch):
    """Preflight handles mixed success/failure across items."""
    from plugboard import app as app_mod

    call_count = 0

    async def _fake_forward(body, timeout=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FAKE_COMPLETION
        raise RuntimeError("fail")

    monkeypatch.setattr(app_mod, "forward", _fake_forward)

    resp = await client.post(
        "/v1/preflight",
        headers=AUTH,
        json={
            "items": [
                {"provider": "test-provider", "model": "fake:model-v1"},
                {"provider": "other-provider", "model": "other:model-v2"},
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["ok"] is True
    assert body["results"][1]["ok"] is False


@pytest.mark.asyncio
async def test_preflight_unauthorized(client):
    """Preflight without auth returns 401."""
    resp = await client.post("/v1/preflight", json={"items": []})
    assert resp.status_code == 401
