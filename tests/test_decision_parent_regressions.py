"""Parent counterexamples for independent SPEC findings; no upstream calls."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "name,error",
    [
        ("ollama:kev-0.8b:latest", "decisions"),
        ("openclaw/typesafe--jev", "removed"),
        ("typesafe/jev", "decisions"),
    ],
)
def test_parent_decision_alias_never_reaches_chat_candidate(monkeypatch, name, error):
    from plugboard import router

    provider = {
        "id": "synthetic-chat",
        "is_active": True,
        "models": [],
        "default_model": "ordinary-chat",
    }
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    with pytest.raises(ValueError, match=error):
        router._resolve_all_providers(name)


@pytest.mark.parametrize(
    "resolve", ["_resolve_all_providers", "resolve_provider_and_model"]
)
def test_parent_decision_default_never_becomes_chat_candidate(monkeypatch, resolve):
    from plugboard import router

    provider = {
        "id": "synthetic-chat",
        "is_active": True,
        "models": [],
        "default_model": "kev-0.8b",
    }
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    with pytest.raises(ValueError, match="decisions"):
        getattr(router, resolve)("")


def test_parent_regular_chat_resolution_still_works(monkeypatch):
    from plugboard import router

    provider = {
        "id": "synthetic-chat",
        "is_active": True,
        "models": [],
        "default_model": "ordinary-chat",
    }
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    assert router._resolve_all_providers("ordinary-chat")[0][1] == "ordinary-chat"


@pytest.mark.parametrize("model", ["laya", "laya-multilingual", "kev-0.8b"])
def test_parent_metadata_reports_actual_local_timeout(model):
    from plugboard.decision_api import _metadata
    from plugboard.decision_runtime import TIMEOUT_SECONDS

    # Decision 72 removes Kev capabilities; retain this case as a rejection.
    if model == "kev-0.8b":
        with pytest.raises(ValueError, match="unknown_model"):
            _metadata(model)
        return
    assert _metadata(model)["limits"]["timeout_seconds"] == TIMEOUT_SECONDS


def test_parent_multilingual_head_budget_matches_pinned_checkpoint():
    from plugboard.decision_api import _metadata

    assert _metadata("laya-multilingual")["limits"]["question_branch_tokens"] == 256
    assert _metadata("laya")["limits"]["question_branch_tokens"] == 192


@pytest.mark.parametrize("operation", ["list_decision_models", "decide"])
def test_parent_sync_sdk_timeout_is_closed_sdk_error(monkeypatch, operation):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "sdk/python"))
    import plugboard_sdk.client as sdk

    def timeout(*args, **kwargs):
        raise TimeoutError("SYNTHETIC-PRIVATE-TRANSPORT-TEXT")

    monkeypatch.setattr(sdk, "urlopen", timeout)
    client = sdk.PlugboardClient("synthetic-not-a-real-key")
    with pytest.raises(sdk.PlugboardError) as error:
        if operation == "decide":
            client.decide(
                "jev",
                "Synthetic state",
                {"q": {"type": "noul", "instructions": "Is this a synthetic test?"}},
            )
        else:
            client.list_decision_models()
    assert error.value.status == 0
    assert "SYNTHETIC-PRIVATE-TRANSPORT-TEXT" not in str(error.value)


@pytest.mark.parametrize("provider_id", ["laya", "kev-proxy", "jev"])
@pytest.mark.parametrize(
    "resolve", ["_resolve_all_providers", "resolve_provider_and_model"]
)
def test_parent_legal_provider_name_preserves_regular_chat(
    monkeypatch, provider_id, resolve
):
    from plugboard import router

    provider = {
        "id": provider_id,
        "name": provider_id,
        "is_active": True,
        "models": [{"id": "gpt-5"}],
        "default_model": "gpt-5",
    }
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    result = getattr(router, resolve)(provider_id + ":gpt-5")
    selected = result[0] if resolve == "_resolve_all_providers" else result
    assert selected[1] == "gpt-5"


@pytest.mark.parametrize("provider_id", ["laya", "kev-proxy", "jev"])
async def test_parent_legal_provider_name_preserves_chat_catalogue(
    monkeypatch, provider_id
):
    from plugboard import router

    provider = {
        "id": provider_id,
        "name": provider_id,
        "is_active": True,
        "base_url": "https://synthetic.invalid/v1",
        "auth_source": "",
        "models": [{"id": "gpt-5"}],
        "default_model": "gpt-5",
    }
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    rows = await router.list_models()
    assert any(row["id"].endswith("gpt-5") for row in rows)


@pytest.mark.parametrize(
    "model,expected_posts",
    [
        ("kev-0.8b", 0),
        ("ollama:kev-0.8b:latest", 0),
        ("openclaw/typesafe--jev", 0),
        ("typesafe/jev", 0),
        ("gpt-5", 1),
    ],
)
async def test_parent_discovery_never_chats_to_decision_default(
    monkeypatch, model, expected_posts
):
    from unittest.mock import AsyncMock
    from plugboard import router
    import plugboard.http_pool

    posts = []

    class Response:
        status = 404
        headers = {"Content-Type": "application/json"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return {}

    class Session:
        def get(self, *args, **kwargs):
            return Response()

        def post(self, *args, **kwargs):
            posts.append(kwargs.get("json"))
            return Response()

    monkeypatch.setattr(
        plugboard.http_pool, "get_session", AsyncMock(return_value=Session())
    )
    for name in (
        "_discover_ollama_models",
        "_discover_anthropic_api_models",
        "_discover_gemini_api_models",
    ):
        monkeypatch.setattr(router, name, AsyncMock(return_value=None))
    provider = {
        "id": "synthetic-chat",
        "name": "Synthetic",
        "base_url": "https://synthetic.invalid/v1",
        "auth_source": "",
        "default_model": model,
        "models": [],
        "is_active": True,
    }
    if model == "openclaw/typesafe--jev":
        with pytest.raises(ValueError, match="removed"):
            await router.discover_models(provider)
    else:
        await router.discover_models(provider)
    assert len(posts) == expected_posts


@pytest.mark.parametrize(
    "model,provider_ids,expected",
    [
        ("qwen3:kev", [], False),
        ("laya:ollama:kev-0.8b:latest", ["laya"], True),
        ("jev:ollama:kev-0.8b:latest", ["jev"], True),
        ("kev-proxy:ollama:kev-0.8b:latest", ["kev-proxy"], True),
    ],
)
def test_parent_v3_identity_context_is_consistent(model, provider_ids, expected):
    from plugboard.decisions import is_decision_model

    assert is_decision_model(model, provider_ids=provider_ids) is expected


@pytest.mark.parametrize(
    "enforced,accepted", [(False, False), (True, False), (True, True)]
)
def test_parent_auth_logs_never_include_bearer_prefix(
    monkeypatch, caplog, enforced, accepted
):
    import importlib
    import logging
    from fastapi import HTTPException
    from starlette.requests import Request

    app_module = importlib.import_module("plugboard.app")
    marker = "lle_SYNTHETIC_ONLY_PARENT_LOG_MARKER"
    monkeypatch.setattr(
        app_module, "_API_KEY", "SYNTHETIC_ENFORCEMENT" if enforced else ""
    )
    monkeypatch.setattr(app_module, "_is_admin_key", lambda token: False)
    monkeypatch.setattr(app_module, "_is_admin_session", lambda token: False)
    record = {"client_id": "synthetic-parent-client", "key_id": "synthetic-parent-key"}
    monkeypatch.setattr(
        app_module, "get_key_record", lambda token: record if accepted else None
    )
    request = Request(
        {"type": "http", "headers": [(b"authorization", ("Bearer " + marker).encode())]}
    )
    with caplog.at_level(logging.DEBUG, logger="plugboard.app"):
        if accepted:
            assert app_module._resolve_client(request) == "synthetic-parent-client"
        else:
            with pytest.raises(HTTPException) as error:
                app_module._resolve_client(request)
            assert error.value.status_code == 401
    assert marker[:8] not in caplog.text
    assert marker not in caplog.text
