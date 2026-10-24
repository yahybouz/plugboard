"""HTTP integration tests; engine ports are fakes, never model inference."""

import asyncio
import copy

import pytest

REQUEST = {
    "model": "jev",
    "state": "The package arrived broken.",
    "questions": {
        "route": {
            "type": "choice",
            "instructions": "Which team?",
            "criteria": {"returns": "Broken item", "billing": "Payment"},
        }
    },
}
ANSWERS = {
    "answers": {
        "route": {
            "type": "choice",
            "choice": "returns",
            "confidence": 0.8,
            "probabilities": {"returns": 0.9, "billing": 0.1},
        }
    }
}


@pytest.fixture
async def decision_client(client, monkeypatch, tmp_path):
    import plugboard.apikeys as keys

    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    rec = keys.create_key("synthetic", allowed_decision_models=["jev"])
    client.headers["Authorization"] = f"Bearer {rec['key']}"
    yield client, rec
    keys._invalidate_cache()


@pytest.mark.parametrize("auth", [None, "testkey", "pb_invalid"])
async def test_decisions_require_real_client_key(client, auth, monkeypatch):
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    headers = {"Authorization": f"Bearer {auth}"} if auth else {}
    for path in ["/v1/decisions/models", "/v1/decisions"]:
        response = await (
            client.get(path, headers=headers)
            if path.endswith("models")
            else client.post(path, headers=headers, json=REQUEST)
        )
        assert response.status_code == 401


async def test_absent_empty_and_inexact_acl_deny(client, monkeypatch, tmp_path):
    import plugboard.apikeys as keys

    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    for policy in [None, [], ["je"], ["jev-latest"]]:
        rec = keys.create_key("synthetic", allowed_decision_models=None)
        # Simulate an old/externally edited record: admission still uses exact IDs.
        data = keys._read()
        data["keys"][-1]["allowed_decision_models"] = policy
        keys._write(data)
        headers = {"Authorization": f"Bearer {rec['key']}"}
        response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
        assert response.status_code == 403
    keys._invalidate_cache()


async def test_api_fake_port_success(decision_client, monkeypatch):
    from plugboard import decision_api

    client, _ = decision_client
    calls = []

    async def fake(model, payload):
        calls.append((model, payload))
        return copy.deepcopy(ANSWERS)

    monkeypatch.setattr(decision_api, "dispatch", fake)
    response = await client.post("/v1/decisions", json=REQUEST)
    assert response.status_code == 200, response.text
    assert response.json()["answers"] == ANSWERS["answers"]
    # No usage in the fake reply: billed at the 64,000-token default context.
    assert response.json()["billing"] == {"status": "estimated", "cost_usd": 0.002688}
    assert calls[0][0] == "jev"


async def test_invalid_request_redacts_body(decision_client, caplog):
    client, _ = decision_client
    secret_state = "SENSITIVE-SYNTHETIC-STATE"
    response = await client.post(
        "/v1/decisions", json={**REQUEST, "state": {"private": secret_state}}
    )
    assert response.status_code == 422
    assert secret_state not in response.text + caplog.text


async def test_removed_kev_is_rejected_even_with_legacy_acl(
    decision_client, monkeypatch
):
    from plugboard import decision_api

    client, rec = decision_client
    rec["allowed_decision_models"] = ["kev-0.8b", "jev", "laya", "laya-multilingual"]
    monkeypatch.setattr(decision_api, "get_key_record", lambda token: rec)

    async def never(*args):
        pytest.fail("Removed Kev must not reach a decision engine")

    monkeypatch.setattr(decision_api, "dispatch", never)
    response = await client.post("/v1/decisions", json={**REQUEST, "model": "kev-0.8b"})
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid decision request"}
    response = await client.get("/v1/decisions/models")
    assert {entry["id"] for entry in response.json()["data"]} == {
        "jev",
        "laya",
        "laya-multilingual",
    }


async def test_removed_kev_cannot_be_provisioned(admin_client, monkeypatch, tmp_path):
    import plugboard.apikeys as keys

    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    response = await admin_client.post(
        "/admin/keys",
        headers={"Authorization": "Bearer adminkey"},
        json={"client_id": "synthetic", "allowed_decision_models": ["kev-0.8b"]},
    )
    assert response.status_code == 422
    with pytest.raises(ValueError):
        keys.create_key("synthetic", allowed_decision_models=["kev-0.8b"])
    keys._invalidate_cache()


async def test_untrusted_output_and_exception_redacted(
    decision_client, monkeypatch, caplog
):
    from plugboard import decision_api

    client, _ = decision_client
    leak = "SYNTHETIC-UPSTREAM-SECRET"
    for output in [
        {"answers": {"unexpected": {"private": leak}}},
        {"answers": {"route": {**ANSWERS["answers"]["route"], "private": leak}}},
        {
            "answers": {
                "route": {
                    **ANSWERS["answers"]["route"],
                    "probabilities": {"returns": float("nan"), "billing": 0.1},
                }
            }
        },
    ]:

        async def fake(model, payload):
            return output

        monkeypatch.setattr(decision_api, "dispatch", fake)
        response = await client.post("/v1/decisions", json=REQUEST)
        assert response.status_code == 502
        assert leak not in response.text + caplog.text

    async def broken(model, payload):
        raise RuntimeError(leak)

    monkeypatch.setattr(decision_api, "dispatch", broken)
    response = await client.post("/v1/decisions", json=REQUEST)
    assert response.status_code == 502
    assert leak not in response.text + caplog.text


async def test_admission_limits_before_dispatch(decision_client, monkeypatch):
    from plugboard import decision_api

    client, rec = decision_client
    calls = []

    async def fake(*args):
        calls.append(args)
        return ANSWERS

    monkeypatch.setattr(decision_api, "dispatch", fake)
    rec["monthly_limit_usd"] = 1
    monkeypatch.setattr(decision_api, "get_key_record", lambda token: rec)
    monkeypatch.setattr("plugboard.app._usage.get_total_cost_by_key", lambda *a: 2)
    response = await client.post("/v1/decisions", json=REQUEST)
    assert response.status_code == 429
    assert calls == []
    rec["monthly_limit_usd"] = 0
    rec["rpm_limit"] = 1
    monkeypatch.setattr(decision_api, "check_client_rate_limit", lambda *a: False)
    response = await client.post("/v1/decisions", json=REQUEST)
    assert response.status_code == 429
    assert calls == []


async def test_timeout_and_capacity_are_bounded(decision_client, monkeypatch):
    from plugboard import decision_api

    client, rec = decision_client

    async def slow(*args):
        await asyncio.sleep(1)

    monkeypatch.setattr(decision_api, "dispatch", slow)
    monkeypatch.setattr(decision_api, "TIMEOUT", 0.01)
    response = await client.post("/v1/decisions", json=REQUEST)
    assert response.status_code == 504
    monkeypatch.setattr(decision_api, "_active_keys", {rec["key_id"]})
    response = await client.post("/v1/decisions", json=REQUEST)
    assert response.status_code == 429


async def test_catalogue_metadata_and_unavailable(decision_client, monkeypatch):
    client, _ = decision_client
    monkeypatch.delenv("PLUGBOARD_JEV_API_KEY", raising=False)
    response = await client.get("/v1/decisions/models")
    assert response.status_code == 200
    models = response.json()["data"]
    assert [m["id"] for m in models] == ["jev"]
    assert models[0]["available"] is False
    assert models[0]["confidence_semantics"]
    response = await client.post("/v1/decisions", json=REQUEST)
    assert response.status_code == 503


@pytest.mark.parametrize("model", ["kev-0.8b", "chat-model"])
def test_unsupported_models_have_no_decision_metadata(model):
    from plugboard import decision_api

    with pytest.raises(ValueError, match="unknown_model"):
        decision_api._metadata(model)


@pytest.mark.parametrize(
    "model",
    [
        "jev",
        "jev-latest",
        "laya",
        "laya-multilingual",
        "kev-0.8b",
        "test-provider:jev",
        "convaiinnovations/laya",
        "jaredpalmer/kev-0.8b",
    ],
)
async def test_decision_models_rejected_on_chat_before_fallback(
    client, model, monkeypatch
):
    async def never(*args, **kwargs):
        pytest.fail("Chat fallback must not dispatch decision models")

    monkeypatch.setattr("plugboard.app.forward", never)
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={"model": model, "messages": []},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Use /v1/decisions for decision models"


async def test_decisions_filtered_from_chat_catalogue(client, monkeypatch):
    async def fake():
        return [
            {"id": "jev"},
            {"id": "test-provider:laya"},
            {"id": "test-provider:chat"},
        ]

    monkeypatch.setattr("plugboard.app.list_models", fake)
    response = await client.get(
        "/v1/models", headers={"Authorization": "Bearer testkey"}
    )
    assert [m["id"] for m in response.json()["data"]] == ["test-provider:chat"]


def test_router_rejects_decision_before_any_provider(monkeypatch):
    from plugboard import router

    monkeypatch.setattr(
        router, "load_providers", lambda: pytest.fail("Must reject before routing")
    )
    for mid in ["jev", "jaredpalmer/kev-0.8b:latest", "jaredpalmer/kev-0.8b", "laya"]:
        with pytest.raises(ValueError, match="decision"):
            router.resolve_provider_and_model(mid)


async def test_router_catalogue_excludes_decision_models(monkeypatch):
    from plugboard import router

    router.invalidate_models_cache()
    monkeypatch.setattr(
        router,
        "load_providers",
        lambda: [
            {
                "id": "test",
                "models": [
                    {"id": "jev"},
                    {"id": "kev-0.8b"},
                    {"id": "laya"},
                    {"id": "gpt-4o"},
                ],
            }
        ],
    )
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSURE", "all")
    items = await router.list_models()
    assert all(
        not any(x in m["id"] for x in ["jev", "kev-0.8b", "laya"]) for m in items
    )
    router.invalidate_models_cache()


async def test_create_and_rotate_exact_policy(admin_client, monkeypatch, tmp_path):
    import plugboard.apikeys as keys

    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    headers = {"Authorization": "Bearer adminkey"}
    response = await admin_client.post(
        "/admin/keys",
        headers=headers,
        json={"client_id": "synthetic", "allowed_decision_models": ["je"]},
    )
    assert response.status_code == 422
    response = await admin_client.post(
        "/admin/keys",
        headers=headers,
        json={"client_id": "synthetic", "allowed_decision_models": ["jev"]},
    )
    assert response.status_code == 201
    rec = response.json()
    assert rec["allowed_decision_models"] == ["jev"]
    rotated = keys.rotate_key(rec["key_id"])
    assert rotated["allowed_decision_models"] == ["jev"]
    assert keys.get_key_record(rec["key"]) is None
    keys._invalidate_cache()


async def test_revoked_key_and_bad_payload_never_dispatch(decision_client, monkeypatch):
    from plugboard import decision_api
    from plugboard.apikeys import revoke_key

    client, rec = decision_client

    async def never(*args):
        pytest.fail("Unauthorized/invalid request cannot dispatch")

    monkeypatch.setattr(decision_api, "dispatch", never)
    assert (
        await client.post("/v1/decisions", json={**REQUEST, "path": "/etc/passwd"})
    ).status_code == 422
    revoke_key(rec["key_id"])
    assert (await client.post("/v1/decisions", json={})).status_code == 401


async def test_body_size_and_jev_score_limit(decision_client, monkeypatch):
    from plugboard import decision_api

    client, _ = decision_client

    async def never(*args):
        pytest.fail("Oversized request cannot dispatch")

    monkeypatch.setattr(decision_api, "dispatch", never)
    response = await client.post("/v1/decisions", content=b" " * 65537)
    assert response.status_code == 413
    response = await client.post(
        "/v1/decisions",
        json={
            **REQUEST,
            "questions": {
                "s": {
                    "type": "score",
                    "instructions": "Rate",
                    "criteria": [str(i) for i in range(11)],
                }
            },
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("runtime_not_configured", 503),
        ("assets_incomplete", 503),
        ("worker_timeout", 504),
        ("worker_busy", 429),
        ("worker_context_limit", 422),
        ("worker_failed", 502),
    ],
)
async def test_local_runtime_errors_have_normalized_http_status(
    decision_client, monkeypatch, code, status
):
    from plugboard import decision_api, decision_runtime

    client, rec = decision_client
    rec["allowed_decision_models"] = ["laya"]
    monkeypatch.setattr(decision_api, "get_key_record", lambda token: rec)

    async def failing(*args):
        raise decision_runtime.RuntimeFailure(code)

    monkeypatch.setattr(decision_runtime, "infer", failing)
    response = await client.post("/v1/decisions", json={**REQUEST, "model": "laya"})
    assert response.status_code == status
    assert code not in response.text


@pytest.mark.parametrize(
    "model", ["jev-preview", "jev-1.14.0", "laya-typed-decisions", "kev-4b"]
)
async def test_all_decision_engine_variants_rejected(client, model, monkeypatch):
    async def never(*args, **kwargs):
        pytest.fail("No chat fallback for other decision variants")

    monkeypatch.setattr("plugboard.app.forward", never)
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={"model": model, "messages": []},
    )
    assert response.status_code == 400


def test_actual_forward_resolver_rejects_decision(monkeypatch):
    from plugboard import router

    monkeypatch.setattr(
        router, "load_providers", lambda: pytest.fail("decision routed to chat")
    )
    with pytest.raises(ValueError, match="decision"):
        router._resolve_all_providers("jev")


async def test_http_to_real_synthetic_subprocess_fake_engine(
    decision_client, monkeypatch, tmp_path
):
    """Full API->worker pipe integration with a fake engine, not ML inference."""
    import sys
    from plugboard import decision_api, decision_runtime

    client, rec = decision_client
    rec["allowed_decision_models"] = ["laya"]
    monkeypatch.setattr(decision_api, "get_key_record", lambda token: rec)
    monkeypatch.setattr(decision_runtime, "available", lambda model: (True, "ready"))
    monkeypatch.setenv("PLUGBOARD_DECISION_ASSETS", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_DECISION_PYTHON", sys.executable)
    code = (
        'import json,sys; r=json.load(sys.stdin); assert r["model"]=="laya"; print('
        + repr(__import__("json").dumps(ANSWERS))
        + ")"
    )
    monkeypatch.setattr(
        decision_runtime, "_command", lambda *a: [sys.executable, "-c", code]
    )
    response = await client.post("/v1/decisions", json={**REQUEST, "model": "laya"})
    assert response.status_code == 200
    assert response.json()["answers"] == ANSWERS["answers"]


async def test_local_missing_configuration_is_genuinely_unavailable(
    decision_client, monkeypatch
):
    from plugboard import decision_api

    client, rec = decision_client
    rec["allowed_decision_models"] = ["laya", "laya-multilingual"]
    monkeypatch.setattr(decision_api, "get_key_record", lambda token: rec)
    monkeypatch.delenv("PLUGBOARD_DECISION_PYTHON", raising=False)
    response = await client.get("/v1/decisions/models")
    assert len(response.json()["data"]) == 2
    assert all(m["available"] is False for m in response.json()["data"])
    for model in rec["allowed_decision_models"]:
        response = await client.post("/v1/decisions", json={**REQUEST, "model": model})
        assert response.status_code == 503


async def test_deeply_nested_json_is_bounded_and_normalized(decision_client):
    client, _ = decision_client
    response = await client.post(
        "/v1/decisions",
        content="[" * 1100 + "0" + "]" * 1100,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
