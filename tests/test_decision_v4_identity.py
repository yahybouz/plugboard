"""Shared identity regressions: ordinary tags survive, actual decisions stay closed."""

import pytest

# Expected identities are hand-derived from registered prefix + transport + name.
CASES = [
    ("qwen3:kev", False),
    ("qwen3:laya", False),
    ("qwen3:jev", False),
    ("qwen3:convai/laya", False),
    ("qwen3:gpt-5@laya", False),
    ("laya/gpt-5:kev@jev", False),
    ("openclaw/openai--gpt-5:laya", False),
    ("kev-0.8b:latest", True),
    ("convaiinnovations/laya:latest@revision", True),
    ("openclaw/typesafe--jev@revision", True),
    ("ollama:kev-0.8b:latest", True),
    ("ollama:qwen3:kev", False),
    ("codex-cli:kev-0.8b@revision", True),
    ("claude-cli-subprocess:convai/laya", True),
    ("Gemini:openclaw/typesafe--jev", True),
    # YB #78: equivalent direct-native names alongside legacy rejection cases.
    ("gpt-5:laya", False),
    ("jev@revision", True),
    ("Gemini:jev", True),
]
REMOVED_MODELS = {
    "openclaw/openai--gpt-5:laya",
    "openclaw/typesafe--jev@revision",
    "Gemini:openclaw/typesafe--jev",
}
PIDS = ["synthetic", "laya", "jev", "kev-proxy"]


def provider(pid, name, default_only=False):
    return {
        "id": pid,
        "name": pid,
        "base_url": "https://synthetic.invalid/v1",
        "auth_source": "",
        "is_active": True,
        "default_model": name,
        "models": [] if default_only else [{"id": name}, {"id": "gpt-5"}],
    }


@pytest.mark.parametrize("name,decision", CASES)
@pytest.mark.parametrize("pid", PIDS)
def test_registered_and_effective_identity_agree(pid, name, decision):
    from plugboard.decisions import is_decision_model

    assert is_decision_model(name, effective_name=True) is decision
    assert is_decision_model(f"{pid}:{name}", provider_ids=[pid]) is decision


@pytest.mark.parametrize("name,decision", CASES)
@pytest.mark.parametrize("pid", ["laya", "synthetic"])
@pytest.mark.parametrize("form", ["unqualified", "qualified", "default"])
@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_resolvers_use_same_effective_identity(
    monkeypatch, pid, name, decision, form, resolve
):
    from plugboard import router

    monkeypatch.setattr(router, "load_providers", lambda: [provider(pid, name)])
    monkeypatch.setattr(router, "_models_alias_cache", {})
    request = {"unqualified": name, "qualified": f"{pid}:{name}", "default": ""}[form]
    if name in REMOVED_MODELS:
        with pytest.raises(
            ValueError, match="(?i)(unsupported|removed).*(legacy|connector)"
        ):
            getattr(router, resolve)(request)
    elif decision:
        with pytest.raises(ValueError, match="Use /v1/decisions"):
            getattr(router, resolve)(request)
    else:
        result = getattr(router, resolve)(request)
        selected = result[0] if resolve == "_resolve_all_providers" else result
        assert selected[0]["id"] == pid
        assert selected[1] == name


@pytest.mark.parametrize("name,decision", CASES)
@pytest.mark.parametrize("pid", PIDS)
@pytest.mark.parametrize("default_only", [False, True])
async def test_catalogue_uses_same_effective_identity(
    monkeypatch, pid, name, decision, default_only
):
    from plugboard import router

    monkeypatch.setattr(
        router, "load_providers", lambda: [provider(pid, name, default_only)]
    )
    monkeypatch.setattr("plugboard.model_exposure.mode", lambda: "all")
    monkeypatch.setattr(router, "_models_cache", None)
    rows = await router.list_models()
    ids = [row["id"] for row in rows]
    expected_id = f"{pid}:{name}"
    assert (expected_id in ids) is (not decision and name not in REMOVED_MODELS)
    if not default_only:
        assert f"{pid}:gpt-5" in ids


@pytest.mark.parametrize("name,decision", CASES)
@pytest.mark.parametrize("qualified", [False, True])
async def test_monitoring_uses_same_identity(monkeypatch, name, decision, qualified):
    from plugboard import monitoring
    from tests.test_decision_v2_fixes import Session

    session = Session()
    monkeypatch.setattr(monitoring, "load_providers", lambda: [provider("laya", name)])
    monkeypatch.setattr(monitoring.aiohttp, "ClientSession", lambda: session)
    request = f"laya:{name}" if qualified else name
    result = await monitoring.check_model_availability(request, "synthetic")
    removed = name in REMOVED_MODELS
    assert result["ok"] is (not decision and not removed)
    assert len(session.posts) == (0 if decision or removed else 1)
    if removed:
        assert "removed legacy connector" in result["error"].lower()
    elif decision:
        assert result["error"] == "decision_model_not_chat"


@pytest.mark.parametrize("name,decision", CASES)
@pytest.mark.parametrize("qualified", [False, True])
async def test_http_guard_uses_same_identity(
    client, monkeypatch, name, decision, qualified
):
    from plugboard import app as gateway
    from unittest.mock import AsyncMock

    monkeypatch.setattr(gateway, "load_providers", lambda: [provider("laya", name)])
    monkeypatch.setattr(
        gateway,
        "forward",
        AsyncMock(return_value={"choices": [{"message": {"content": "synthetic"}}]}),
    )
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={
            "model": f"laya:{name}" if qualified else name,
            "messages": [{"role": "user", "content": "synthetic"}],
            "cache": False,
        },
    )
    if name in REMOVED_MODELS:
        assert response.status_code == 400
        assert "removed legacy connector" in response.json()["detail"].lower()
    elif decision:
        assert response.status_code == 400
        assert response.json()["detail"] == "Use /v1/decisions for decision models"
    else:
        assert response.status_code == 200


@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
@pytest.mark.parametrize(
    "name", ["jev", "convai/laya:latest@revision", "openclaw/typesafe--jev"]
)
def test_unambiguous_decision_needs_no_provider_store(monkeypatch, resolve, name):
    from plugboard import router

    monkeypatch.setattr(
        router, "load_providers", lambda: pytest.fail("private store read")
    )
    error = (
        "(?i)(unsupported|removed).*(legacy|connector)"
        if name.startswith("openclaw/")
        else "Use /v1/decisions"
    )
    with pytest.raises(ValueError, match=error):
        getattr(router, resolve)(name)


@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
@pytest.mark.parametrize("name", ["qwen3:kev", "unknown:jev", "unknown:laya"])
def test_unknown_prefix_preserves_no_provider_error(monkeypatch, resolve, name):
    from plugboard import router

    monkeypatch.setattr(router, "load_providers", lambda: [])
    with pytest.raises(ValueError, match="No active providers configured"):
        getattr(router, resolve)(name)


@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
@pytest.mark.parametrize("pid", ["ollama", "Codex", "codex-cli"])
def test_registered_transport_homonym_has_priority(monkeypatch, resolve, pid):
    from plugboard import router

    ordinary = {
        "id": pid,
        "models": [{"id": "gpt-5:kev"}],
        "default_model": "gpt-5:kev",
    }
    monkeypatch.setattr(router, "load_providers", lambda: [ordinary])
    result = getattr(router, resolve)(f"{pid}:gpt-5:kev")
    selected = result[0] if resolve == "_resolve_all_providers" else result
    assert selected == (ordinary, "gpt-5:kev")


@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
@pytest.mark.parametrize(
    "pid,canonical",
    [
        ("Codex", "chatgpt-via-codex-cli"),
        ("Gemini", "google-gemini-via-gemini-cli"),
        ("claude-code", "claude-via-claude-cli"),
    ],
)
def test_registered_clean_alias_wins_over_canonical_transport(
    monkeypatch, resolve, pid, canonical
):
    from plugboard import router

    selected_provider = provider(pid, "qwen3:kev", default_only=True)
    monkeypatch.setattr(
        router,
        "load_providers",
        lambda: [selected_provider, provider(canonical, "gpt-5")],
    )
    result = getattr(router, resolve)(f"{pid}:qwen3:kev")
    selected = result[0] if resolve == "_resolve_all_providers" else result
    assert selected == (selected_provider, "qwen3:kev")
