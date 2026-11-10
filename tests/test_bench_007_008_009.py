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


# ── BUG-6: a client key never sees the CLI process details ────────────────


_LEAKY = CliSubprocessError(
    "CLI 'gemini' exited 1: secret-path /home/op/.config token=abc123",
    exit_code=1,
    stderr="secret-path /home/op/.config token=abc123",
    pid=4242,
    provider_name="gemini",
    model_id="gemini-2.5-flash",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_cli_error_hides_process_details_from_client_keys(
    client, tmp_path, monkeypatch, caplog, stream
):
    import logging

    import plugboard.apikeys as ak
    from plugboard import app as app_mod
    from plugboard.router import resolve_provider_and_model

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    key = ak.create_key("cli-client", "test")["key"]

    def _fake_resolve(model_str):
        p, m = resolve_provider_and_model(model_str)
        p["auth_source"] = "subprocess:gemini"
        return p, m

    async def _fake_forward(*args, **kwargs):
        raise _LEAKY

    monkeypatch.setattr(app_mod, "resolve_provider_and_model", _fake_resolve)
    monkeypatch.setattr(app_mod, "forward", _fake_forward)
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING, logger=app_mod.log.name):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": stream,
            },
        )
    assert resp.status_code == 502
    body = resp.json()
    assert set(body) == {"detail", "error_ref", "provider_name", "model_id"}
    assert "secret-path" not in resp.text and "4242" not in resp.text
    assert body["error_ref"] in body["detail"]
    # The operator finds the full error in the log under the same reference.
    logged = [
        r.getMessage() for r in caplog.records if body["error_ref"] in r.getMessage()
    ]
    assert logged and "secret-path" in logged[0]


@pytest.mark.asyncio
async def test_cli_error_keeps_details_for_admin(client, monkeypatch):
    from plugboard import app as app_mod

    async def _fake_forward(*args, **kwargs):
        raise _LEAKY

    monkeypatch.setattr(app_mod, "forward", _fake_forward)
    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH,  # the admin key in this fixture
        json={
            "model": "gemini:gemini-2.5-flash",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    body = resp.json()
    assert resp.status_code == 502
    assert body["stderr"].startswith("secret-path") and body["pid"] == 4242
    assert body["error_ref"]


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
    rec = ak.create_key("test-client", "test", allowed_models=["fake:model-v1"])

    resp = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {rec['key']}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    compat = {item["id"]: item["account_compatible"] for item in body["data"]}
    assert compat, "catalogue must not be empty"
    for mid, ok in compat.items():
        # Exact effective identity only; no substring matching.
        assert ok is (mid == "test-provider:fake:model-v1"), mid


def _restricted_key(tmp_path, monkeypatch, allowed_models):
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    return ak.create_key("test-client", "test", allowed_models=allowed_models)


_MODEL_LISTS = [
    "/v1/models",
    "/v1/models/benchmarks",
    "/v2/models",
    "/v2/models/benchmarks",
]


async def _model_ids(client, key, path="/v1/models"):
    resp = await client.get(path, headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    return [item["id"] for item in resp.json()["data"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _MODEL_LISTS)
async def test_models_restricted_key_lists_only_chat_allowed_models(
    client, tmp_path, monkeypatch, path
):
    """BUG-24: a key limited by allowed_models only sees what chat accepts."""
    from fastapi import HTTPException

    from plugboard.app import _check_model_access

    # One qualified grant and one native-only grant, as chat accepts both.
    rec = _restricted_key(
        tmp_path, monkeypatch, ["other-provider:other:model-v2", "fake:model-v1"]
    )
    full = await _model_ids(client, "testkey", path)  # admin: whole catalogue

    def chat_accepts(model_id):
        try:
            _check_model_access("test-client", model_id, key_record=rec)
        except (HTTPException, ValueError):
            return False
        return True

    listed = await _model_ids(client, rec["key"], path)
    assert sorted(listed) == [
        "other-provider:other:model-v2",
        "test-provider:fake:model-v1",
    ]
    assert listed == [mid for mid in full if chat_accepts(mid)]
    assert len(full) > len(listed)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _MODEL_LISTS)
async def test_models_unrestricted_key_lists_whole_catalogue(
    client, tmp_path, monkeypatch, path
):
    """Without allowed_models, the key sees the same list as the admin."""
    rec = _restricted_key(tmp_path, monkeypatch, [])
    listed = await _model_ids(client, rec["key"], path)
    assert listed == await _model_ids(client, "testkey", path)


async def _model_info(client, key, model_id):
    resp = await client.get(
        f"/v1/models/{model_id}/info", headers={"Authorization": f"Bearer {key}"}
    )
    return resp.status_code, resp.json()


@pytest.mark.asyncio
async def test_model_info_refused_model_answers_as_unknown(
    client, tmp_path, monkeypatch
):
    """BUG-24: a limited key gets the unknown-model answer for a refused model."""
    import plugboard.benchmarks as bench

    rec = _restricted_key(tmp_path, monkeypatch, ["fake:model-v1"])
    refused = "other-provider:claude-sonnet-4-6"  # has a curated profile
    admin_view = await _model_info(client, "testkey", refused)
    assert admin_view[1]["info_source"] == "benchmark_db"

    # Allowed model: the limited key sees what the admin sees.
    allowed = "test-provider:fake:model-v1"
    assert await _model_info(client, rec["key"], allowed) == await _model_info(
        client, "testkey", allowed
    )

    limited_view = await _model_info(client, rec["key"], refused)
    # What the route answers when the profile database does not know the model.
    monkeypatch.setattr(bench, "_MODELS_DB", {})
    unknown_view = await _model_info(client, "testkey", refused)
    assert limited_view == unknown_view
    assert limited_view[0] == 200 and limited_view[1]["info_source"] == "derived"


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
