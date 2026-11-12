"""Ollama switch (PLU-12, D-38): a disabled provider is never called (BUG-44).

The switch is the ``is_active`` field of the provider: turned off, a model
only that provider serves (``ollama:qwen3:8b``, or a bare name only it lists)
used to be sent to every other active provider. It now gets 503
``provider_disabled`` and nothing is called, on every surface.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import api_contract, router
from tests.test_model_not_found import ACME, CODEX, OPEN, _isolated_providers

DISABLED_BODY = {
    "detail": "Provider 'ollama' is turned off by the administrator; "
    "nothing was called.",
    "code": "provider_disabled",
    "hint": "Pick another model from GET /v1/models, or ask the administrator "
    "to turn the provider back on.",
}
ADMIN_KEY = "admin-key-for-tests"

OLLAMA = {
    "id": "ollama",
    "name": "Ollama",
    # Closed port: a test that slipped past the fakes reaches no real Ollama.
    "base_url": "http://127.0.0.1:9",
    "bearer_token": "",
    "default_model": "",
    "provider_type": "sovereign",
    "models": [{"id": "qwen3:8b"}, {"id": "llama3:latest"}],
    "is_active": False,
}
BETA = {
    **ACME,
    "id": "beta-cloud",
    "name": "Beta Cloud",
    "base_url": "http://beta.test",
    "default_model": "beta-1",
    "models": [{"id": "beta-1"}],
    "extra_model_ids": [],
}


@pytest.fixture(autouse=True)
def _no_real_ollama(monkeypatch):
    """Any call to the Ollama daemon fails the test."""
    from plugboard import ollama

    async def _refused(*_args, **_kwargs):
        raise AssertionError("no real Ollama call")

    monkeypatch.setattr(ollama, "check_available", _refused)
    monkeypatch.setattr(ollama, "list_models", _refused)


@pytest.fixture
def off(tmp_path, monkeypatch):
    """Ollama turned off, two active providers with a model list."""
    return _isolated_providers(tmp_path, monkeypatch, [OLLAMA, ACME, BETA])


# ── Which models belong to a disabled provider only ────────────────────────


@pytest.mark.parametrize(
    ("model", "native"),
    [
        ("ollama:qwen3:8b", "qwen3:8b"),
        ("qwen3:8b", "qwen3:8b"),
        ("llama3", "llama3:latest"),  # Ollama reads a bare name as :latest
        (" LLAMA3:latest ", "llama3:latest"),  # any case, surrounding spaces
        ("ollama:not-listed", "not-listed"),  # an explicit prefix is enough
    ],
)
def test_a_model_of_a_disabled_provider_is_recognised(off, model, native):
    provider, got = router.disabled_route(model)
    assert (provider["id"], got) == ("ollama", native)


@pytest.mark.parametrize(
    "model", ["acme-large", "acme-direct:x", "nope:model-x", "made-up", ""]
)
def test_other_models_are_not_disabled(off, model):
    assert router.disabled_route(model) is None


def test_a_model_that_is_not_a_string_is_not_disabled(off):
    assert router.disabled_route(None) is None  # type: ignore[arg-type]


def test_nothing_is_disabled_when_ollama_is_on(tmp_path, monkeypatch):
    _isolated_providers(
        tmp_path, monkeypatch, [{**OLLAMA, "is_active": True}, ACME, BETA]
    )
    assert router.disabled_route("ollama:qwen3:8b") is None
    assert router.disabled_route("qwen3:8b") is None


def test_bare_name_goes_on_when_an_active_provider_has_no_list(tmp_path, monkeypatch):
    """A provider without a model list may serve any name: routed as before."""
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, ACME, OPEN])
    assert router.disabled_route("qwen3:8b") is None
    provider, native = router.disabled_route("ollama:qwen3:8b")
    assert (provider["id"], native) == ("ollama", "qwen3:8b")


def test_longer_active_prefix_wins(tmp_path, monkeypatch):
    gpu = {**OLLAMA, "id": "ollama-gpu", "name": "Ollama GPU", "is_active": True}
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, gpu, ACME])
    assert router.disabled_route("ollama-gpu:qwen3:8b") is None
    assert router.disabled_route("qwen3:8b") is None  # an active provider lists it


def test_a_longer_disabled_owner_keeps_its_namespace(tmp_path, monkeypatch):
    """'acme:eu' off: 'acme:eu:m1' is its model, not 'eu:m1' of active 'acme'."""
    nested = {
        **ACME,
        "id": "acme:eu",
        "name": "Acme EU",
        "models": [{"id": "m1"}],
        "is_active": False,
    }
    _isolated_providers(tmp_path, monkeypatch, [{**ACME, "id": "acme"}, nested])
    provider, native = router.disabled_route("acme:eu:m1")
    assert (provider["id"], native) == ("acme:eu", "m1")
    assert router.disabled_route("acme:acme-large") is None


def test_a_shorter_disabled_owner_does_not_take_a_longer_active_one(
    tmp_path, monkeypatch
):
    nested = {**ACME, "id": "acme:eu", "name": "Acme EU", "models": [{"id": "m1"}]}
    _isolated_providers(
        tmp_path, monkeypatch, [{**ACME, "id": "acme", "is_active": False}, nested]
    )
    assert router.disabled_route("acme:eu:m1") is None
    provider, _native = router.disabled_route("acme:acme-large")
    assert provider["id"] == "acme"


def test_the_only_provider_disabled(tmp_path, monkeypatch):
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA])
    for model in ("ollama:qwen3:8b", "qwen3:8b"):
        provider, native = router.disabled_route(model)
        assert (provider["id"], native) == ("ollama", "qwen3:8b")


@pytest.mark.parametrize("model", ["Codex:gpt-6.1-sol", "openai:gpt-6.1-sol"])
def test_a_display_alias_of_a_disabled_cli_is_recognised(tmp_path, monkeypatch, model):
    _isolated_providers(
        tmp_path, monkeypatch, [{**CODEX, "is_active": False}, ACME, BETA]
    )
    provider, native = router.disabled_route(model)
    assert (provider["id"], native) == ("chatgpt-via-codex-cli", "gpt-6.1-sol")


@pytest.mark.parametrize("model", ["Codex:gpt-6.1-sol", "gpt-6.1-sol"])
def test_a_display_alias_of_an_active_cli_is_not_disabled(tmp_path, monkeypatch, model):
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, CODEX, ACME])
    assert router.disabled_route(model) is None


def test_an_inactive_owner_does_not_hide_an_active_alias(tmp_path, monkeypatch):
    """An inactive provider named 'Codex' leaves 'Codex:' to the Codex CLI."""
    named_codex = {
        **ACME,
        "id": "Codex",
        "name": "Codex",
        "models": [{"id": "gpt-6.1-sol"}],
        "is_active": False,
    }
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, CODEX, named_codex, ACME])
    assert router.disabled_route("Codex:gpt-6.1-sol") is None


def test_unknown_model_rule_is_unchanged(off):
    assert router.is_unknown_model("ollama:qwen3:8b") is False
    assert router.is_unknown_model("nope:model-x") is True


def test_provider_disabled_error_names_the_provider():
    exc = router.ProviderDisabledError("ollama")
    assert exc.provider_id == "ollama"
    assert "Provider 'ollama' is turned off" in str(exc)


# ── What the chat routes answer ─────────────────────────────────────────────


@pytest.fixture
async def http(tmp_path, monkeypatch):
    """App client with a real client key; forward() replaced by a recorder."""
    import plugboard.app as app_module
    import plugboard.ratelimit as rl
    from plugboard import apikeys

    monkeypatch.setattr(app_module, "_API_KEY", "instance-key")
    monkeypatch.setattr(app_module, "_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setattr(rl, "_LIMIT", 0)
    monkeypatch.setattr(apikeys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(apikeys, "KEYS_LOCK", tmp_path / "keys.lock")
    from plugboard import audit, live_requests, session_store, usage

    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    monkeypatch.setattr(audit, "_DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(session_store, "_DB_PATH", tmp_path / "sessions.db")
    live_requests.reset()
    key = apikeys.create_key("acme", "switch")["key"]

    calls: list = []

    async def _forward(body, **_kwargs):
        calls.append(body.get("model"))
        raise RuntimeError("upstream refused the call")

    async def _forward_stream(body, **_kwargs):
        calls.append(body.get("model"))
        raise RuntimeError("upstream refused the call")
        yield b""  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(app_module, "forward", _forward)
    monkeypatch.setattr(app_module, "forward_stream", _forward_stream)
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        yield client, {"Authorization": f"Bearer {key}"}, calls


def _chat(model: str, **extra) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "cache": False,
        **extra,
    }


def _key(**fields) -> dict:
    from plugboard import apikeys

    key = apikeys.create_key("acme", "limited", **fields)["key"]
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize("model", ["ollama:qwen3:8b", "qwen3:8b"])
@pytest.mark.parametrize("stream", [False, True])
async def test_chat_gets_503_and_calls_nobody(off, http, path, model, stream):
    client, auth, calls = http
    resp = await client.post(path, headers=auth, json=_chat(model, stream=stream))

    assert resp.status_code == 503, resp.text
    assert resp.json() == DISABLED_BODY
    api_contract.ErrorResponse.model_validate(resp.json())
    assert "retry-after" not in resp.headers
    assert calls == [], "a disabled provider is never replaced by another one"


async def test_playground_gets_the_same_503(off, http):
    client, _auth, calls = http
    resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "X-Plugboard-Surface": "playground",
        },
        json=_chat("ollama:qwen3:8b"),
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "provider_disabled"
    assert calls == []


async def test_limited_key_gets_403_first(off, http):
    """A key that may not use the model learns nothing about the switch."""
    client, _auth, calls = http
    resp = await client.post(
        "/v1/chat/completions",
        headers=_key(allowed_models=["acme-direct:acme-large"]),
        json=_chat("ollama:qwen3:8b"),
    )
    assert resp.status_code == 403, resp.text
    assert "'ollama:qwen3:8b'" in resp.json()["detail"]
    assert calls == []


@pytest.mark.parametrize("model", ["ollama:qwen3:8b", "qwen3:8b"])
async def test_allowed_key_gets_503_not_403(off, http, model):
    client, _auth, calls = http
    resp = await client.post(
        "/v1/chat/completions",
        headers=_key(allowed_models=["ollama:qwen3:8b"]),
        json=_chat(model),
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "provider_disabled"
    assert calls == []


async def test_limited_key_gets_403_first_for_a_bare_name(off, http):
    client, _auth, calls = http
    resp = await client.post(
        "/v1/chat/completions",
        headers=_key(allowed_models=["acme-direct:acme-large"]),
        json=_chat("qwen3:8b"),
    )
    assert resp.status_code == 403, resp.text
    assert calls == []


async def test_session_turn_gets_503_and_stores_nothing(off, http):
    from plugboard import session_store

    client, auth, calls = http
    resp = await client.post(
        "/v1/chat/completions",
        headers={**auth, "X-LLM-Session-ID": "switch-session"},
        json=_chat("ollama:qwen3:8b"),
    )
    assert resp.status_code == 503, resp.text
    assert calls == []
    assert session_store.load_session("acme", "switch-session") == []


async def test_inflight_names_the_disabled_owner(off, http):
    from plugboard import live_requests

    client, _auth, _calls = http
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        json=_chat("ollama:qwen3:8b"),
    )
    assert resp.status_code == 503, resp.text
    (entry,) = live_requests.snapshot(admin=True)["recent"]
    assert (entry["provider"], entry["status"]) == ("ollama", 503)


async def test_budgeted_key_gets_503_not_402(off, http):
    client, _auth, calls = http
    resp = await client.post(
        "/v1/chat/completions",
        headers=_key(monthly_limit_usd=5.0),
        json=_chat("ollama:qwen3:8b"),
    )
    assert resp.status_code == 503, resp.text
    assert calls == []


async def test_cache_is_not_read_when_disabled(off, http, monkeypatch):
    from plugboard import cache

    client, auth, calls = http
    reads: list = []
    monkeypatch.setattr(cache, "get_cached", lambda *a, **k: reads.append(a))
    body = {**_chat("ollama:qwen3:8b"), "cache": True}
    resp = await client.post("/v1/chat/completions", headers=auth, json=body)

    assert resp.status_code == 503, resp.text
    assert reads == [] and calls == []


async def test_chat_is_routed_again_once_turned_on(off, http):
    client, auth, calls = http
    data = json.loads(off.read_text())
    data["providers"][0]["is_active"] = True
    off.write_text(json.dumps(data))
    router.invalidate_models_cache()

    resp = await client.post(
        "/v1/chat/completions", headers=auth, json=_chat("ollama:qwen3:8b")
    )
    assert resp.status_code == 502, resp.text  # the recorder refuses the call
    assert calls == ["ollama:qwen3:8b"]


# ── What the preflight answers ──────────────────────────────────────────────


@pytest.fixture
def pinged(http, tmp_path, monkeypatch):
    """http, with every ping answered and billed to a usage store of its own."""
    import copy

    import plugboard.app as app_module
    from plugboard import usage
    from tests.factories import FAKE_COMPLETION

    client, auth, calls = http

    async def _forward(body, **_kwargs):
        calls.append(body.get("model"))
        return copy.deepcopy(FAKE_COMPLETION)

    monkeypatch.setattr(app_module, "forward", _forward)
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    return client, auth, calls


async def _preflight(client, auth, provider: str, model: str) -> dict:
    resp = await client.post(
        "/v1/preflight",
        headers=auth,
        json={"items": [{"provider": provider, "model": model}]},
    )
    assert resp.status_code == 200, resp.text
    api_contract.PreflightResponse.model_validate(resp.json())
    (result,) = resp.json()["results"]
    return result


@pytest.mark.parametrize("caller", ["client key", "admin key"])
async def test_preflight_answers_provider_disabled_without_a_ping(off, pinged, caller):
    from plugboard import billing, usage

    client, auth, calls = pinged
    if caller == "admin key":
        auth = {"Authorization": f"Bearer {ADMIN_KEY}"}

    result = await _preflight(client, auth, "ollama", "qwen3:8b")

    assert (result["ok"], result["error"]) == (False, "provider_disabled")
    assert calls == [], "no ping goes to a disabled provider, nor to another"
    assert usage.get_usage(billing.current_pricing_month()) == []


async def test_preflight_with_no_provider_on(tmp_path, monkeypatch, pinged):
    """Ollama alone and off: provider_disabled, not unknown_model."""
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA])
    client, auth, calls = pinged
    result = await _preflight(client, auth, "ollama", "qwen3:8b")
    assert result["error"] == "provider_disabled"
    assert calls == []


async def test_preflight_gives_a_limited_key_forbidden_model_first(off, pinged):
    client, _auth, calls = pinged
    auth = _key(allowed_models=["acme-direct:acme-large"])
    result = await _preflight(client, auth, "ollama", "qwen3:8b")
    assert result["error"] == "forbidden_model"
    assert calls == []


async def test_preflight_maps_a_late_refusal(off, pinged, monkeypatch):
    """Turned off between the checks and the ping: provider_disabled too."""
    import plugboard.app as app_module

    client, auth, _calls = pinged

    async def _forward(body, **_kwargs):
        raise router.ProviderDisabledError("acme-direct")

    monkeypatch.setattr(app_module, "forward", _forward)
    result = await _preflight(client, auth, "acme-direct", "acme-large")
    assert result["error"] == "provider_disabled"


def test_contract_documents_provider_disabled():
    import typing

    assert api_contract.CONTRACT_VERSION == "1.6.0"
    annotation = api_contract.PreflightResult.model_fields["error"].annotation
    literal = next(a for a in typing.get_args(annotation) if a is not type(None))
    assert "provider_disabled" in typing.get_args(literal)
    assert "provider_disabled" in (
        api_contract.ErrorResponse.model_fields["code"].description or ""
    )
    assert "provider_disabled" in (api_contract.ErrorResponse.__doc__ or "")
    assert "provider_disabled" in api_contract._CHAT_ERRORS["503"]
