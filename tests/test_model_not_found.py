"""404 model_not_found for a model ID no provider knows (PB-REL-04, D-15).

A chat request whose model has no registered provider prefix used to be sent
to every active provider, which all failed: the client got a 502 naming one of
them. When the ID is known nowhere (its prefix names no provider and no
provider lists it), the gateway now answers 404 ``model_not_found`` with a
hint, without calling any provider. Any ID a provider could still serve keeps
today's routing: a bare Ollama tag a provider lists, a family alias /v1/models
publishes for dated snapshots, a provider prefix, a model of a provider whose
model list is unknown. POST /v1/preflight applies the same rule to each pair
(BUG-42): error ``model_not_found``, no ping, nothing billed.
"""

from __future__ import annotations

import copy

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import api_contract
from tests.conftest import _setup_providers
from tests.factories import FAKE_COMPLETION

OLLAMA = {
    "id": "ollama-gpu",
    "name": "Ollama GPU",
    "base_url": "http://ollama.test:11434",
    "bearer_token": "",
    "default_model": "qwen3:8b",
    "provider_type": "sovereign",
    "models": [
        {"id": "gpt-oss:20b-cloud"},
        {"id": "llama3:latest"},
        {"id": "qwen3:8b"},
    ],
    "is_active": True,
}
ACME = {
    "id": "acme-direct",
    "name": "Acme Direct",
    "base_url": "http://acme.test",
    "bearer_token": "acme-token",
    "default_model": "acme-default",
    "models": [{"id": "acme-large"}, {"id": "acme-small"}],
    "extra_model_ids": ["acme-preview"],
    "is_active": True,
}
DORMANT = {
    "id": "dormant",
    "name": "Dormant",
    "base_url": "http://dormant.test",
    "bearer_token": "dormant-token",
    "default_model": "dormant-model",
    "models": [{"id": "dormant-model"}],
    "is_active": False,
}
OPEN = {
    "id": "custom-open",
    "name": "Custom Open",
    "base_url": "http://open.test",
    "bearer_token": "open-token",
    "default_model": "open-default",
    "is_active": True,
}
CODEX = {
    "id": "chatgpt-via-codex-cli",
    "name": "Codex CLI",
    "base_url": "",
    "bearer_token": "",
    "auth_source": "subprocess:codex",
    "default_model": "gpt-6.1-sol",
    "models": [{"id": "gpt-6.1-sol"}],
    "is_active": True,
}

# Lists dated snapshots only, as a refreshed Anthropic catalogue does: /v1/models
# publishes each family name (claude-sonnet-4-5) as an alias of its snapshot.
DATED_ONLY = {
    "id": "anthropic-direct",
    "name": "Anthropic Direct",
    "base_url": "http://anthropic.test/v1",
    "bearer_token": "anthropic-token",
    "default_model": "claude-sonnet-4-5-20250929",
    "models": [
        {"id": "claude-sonnet-4-5-20250929"},
        {"id": "claude-haiku-4-5-20251001"},
        {"id": "claude-opus-4-1-20250805"},
    ],
    "is_active": True,
}

LISTED = [OLLAMA, ACME, DORMANT]


def _isolated_providers(tmp_path, monkeypatch, providers):
    """_setup_providers, with the provider file and model caches restored after.

    _setup_providers assigns config.PROVIDERS_FILE directly and /v1/models
    fills the router's module caches; monkeypatch records them first.
    """
    import plugboard.config as cfg
    from plugboard import router

    for name in ("PROVIDERS_FILE", "PROVIDERS_LOCK"):
        monkeypatch.setattr(cfg, name, getattr(cfg, name))
    for name in (
        "_models_cache",
        "_models_cache_ts",
        "_models_cache_namespace",
        "_models_alias_cache",
        "_model_status_mtime",
    ):
        monkeypatch.setattr(router, name, getattr(router, name))
    return _setup_providers(tmp_path, monkeypatch, providers)


@pytest.fixture
def listed(tmp_path, monkeypatch):
    """Every active provider has a model list."""
    return _isolated_providers(tmp_path, monkeypatch, LISTED)


@pytest.fixture
def dated_only(tmp_path, monkeypatch):
    """One active provider lists dated snapshots only."""
    return _isolated_providers(tmp_path, monkeypatch, [*LISTED, DATED_ONLY])


# ── Which IDs are known nowhere ─────────────────────────────────────────────


@pytest.mark.parametrize("model", ["nope:model-x", "made-up-model", "Codex:gpt-9"])
def test_an_id_no_provider_knows_is_unknown(listed, model):
    from plugboard.router import is_unknown_model

    assert is_unknown_model(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "gpt-oss:20b-cloud",  # bare Ollama tag a provider lists
        "GPT-OSS:20B-Cloud",  # Ollama ignores case
        " gpt-oss:20b-cloud\t",  # and surrounding spaces
        "llama3",  # Ollama reads a name without a tag as :latest
        "acme-large",  # listed model
        "acme-preview",  # extra_model_ids
        "acme-default",  # default model, absent from the list
        "dormant-model",  # listed by an inactive provider only
        "acme-direct:not-listed",  # active provider prefix
        "dormant:anything",  # inactive provider prefix
        "",  # no model: the default model
    ],
)
def test_an_id_a_provider_may_know_is_not_unknown(listed, model):
    from plugboard.router import is_unknown_model

    assert is_unknown_model(model) is False


@pytest.mark.parametrize(
    "model", ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"]
)
def test_a_family_alias_of_dated_snapshots_is_not_unknown(dated_only, model):
    """/v1/models publishes the family as an alias: known, even on a cold cache."""
    from plugboard.router import is_unknown_model

    assert is_unknown_model(model) is False


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-sonnet-4", "claude"])
def test_a_name_no_family_alias_matches_stays_unknown(dated_only, model):
    from plugboard.router import is_unknown_model

    assert is_unknown_model(model) is True


def test_a_provider_without_a_model_list_may_serve_any_id(tmp_path, monkeypatch):
    from plugboard.router import is_unknown_model

    _isolated_providers(tmp_path, monkeypatch, [*LISTED, OPEN])
    assert is_unknown_model("nope:model-x") is False


def test_an_inactive_provider_without_a_model_list_does_not_count(
    tmp_path, monkeypatch
):
    from plugboard.router import is_unknown_model

    _isolated_providers(tmp_path, monkeypatch, [*LISTED, {**OPEN, "is_active": False}])
    assert is_unknown_model("nope:model-x") is True


@pytest.mark.parametrize(
    "model", ["Codex:gpt-6.1-sol", "gpt-6.1-sol", "openai:gpt-6.1-sol"]
)
def test_cli_aliases_of_a_listed_model_are_not_unknown(tmp_path, monkeypatch, model):
    """Display alias, bare name and the vendor spelling resolve as before."""
    from plugboard.router import is_unknown_model

    _isolated_providers(tmp_path, monkeypatch, [*LISTED, CODEX])
    assert is_unknown_model(model) is False


def test_no_active_provider_keeps_the_existing_answer(tmp_path, monkeypatch):
    from plugboard.router import is_unknown_model

    _isolated_providers(tmp_path, monkeypatch, [DORMANT])
    assert is_unknown_model("nope:model-x") is False


# ── What the chat routes answer ─────────────────────────────────────────────


@pytest.fixture
async def http(tmp_path, monkeypatch):
    """App client with a real client key; forward() replaced by a recorder."""
    import plugboard.app as app_module
    import plugboard.ratelimit as rl
    from plugboard import apikeys

    monkeypatch.setattr(app_module, "_API_KEY", "instance-key")
    monkeypatch.setattr(app_module, "_ADMIN_KEY", "admin-key-for-tests")
    monkeypatch.setattr(rl, "_LIMIT", 0)
    monkeypatch.setattr(apikeys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(apikeys, "KEYS_LOCK", tmp_path / "keys.lock")
    key = apikeys.create_key("acme", "not-found")["key"]

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


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
async def test_unknown_model_gets_404_with_a_hint(listed, http, path):
    client, auth, calls = http
    resp = await client.post(path, headers=auth, json=_chat("nope:model-x"))

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "model_not_found"
    assert "nope:model-x" in body["detail"]
    assert "GET /v1/models" in body["hint"]
    api_contract.ErrorResponse.model_validate(body)
    assert calls == [], "no provider is called for a model no provider knows"


@pytest.mark.asyncio
async def test_unknown_model_gets_404_when_streamed(listed, http):
    client, auth, calls = http
    resp = await client.post(
        "/v1/chat/completions", headers=auth, json=_chat("made-up-model", stream=True)
    )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "model_not_found"
    assert calls == []


@pytest.mark.asyncio
async def test_a_very_long_unknown_id_is_shortened_in_the_detail(listed, http):
    client, auth, _calls = http
    model = "nope:" + "x" * 500
    resp = await client.post("/v1/chat/completions", headers=auth, json=_chat(model))

    assert resp.status_code == 404
    assert model not in resp.json()["detail"]
    assert len(resp.text) < 400


@pytest.mark.asyncio
async def test_a_limited_key_gets_403_for_any_model_it_may_not_use(listed, http):
    """403 comes first: a limited key cannot learn which IDs exist elsewhere."""
    from plugboard import apikeys

    client, _auth, calls = http
    key = apikeys.create_key("acme", "limited", allowed_models=["acme-large"])["key"]
    auth = {"Authorization": f"Bearer {key}"}
    for model in ("nope:model-x", "acme-small"):
        resp = await client.post(
            "/v1/chat/completions", headers=auth, json=_chat(model)
        )
        assert resp.status_code == 403, (model, resp.text)
    assert calls == []


@pytest.mark.asyncio
async def test_a_limited_key_allowed_an_unknown_name_gets_404(listed, http):
    from plugboard import apikeys

    client, _auth, calls = http
    key = apikeys.create_key("acme", "limited", allowed_models=["made-up-model"])
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key['key']}"},
        json=_chat("made-up-model"),
    )
    assert resp.status_code == 404, resp.text
    assert calls == []


@pytest.mark.asyncio
async def test_a_budgeted_key_gets_404_not_402_for_an_unknown_model(listed, http):
    from plugboard import apikeys

    client, _auth, calls = http
    key = apikeys.create_key("acme", "budget", monthly_limit_usd=5.0)["key"]
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=_chat("nope:model-x"),
    )
    assert resp.status_code == 404, resp.text
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-oss:20b-cloud", "acme-direct:not-listed"])
async def test_a_known_id_is_routed_as_before(listed, http, model):
    client, auth, calls = http
    resp = await client.post("/v1/chat/completions", headers=auth, json=_chat(model))

    assert resp.status_code == 502, resp.text
    assert calls == [model]


@pytest.mark.asyncio
async def test_a_family_alias_listed_by_v1_models_is_routed_as_before(dated_only, http):
    client, auth, calls = http
    catalogue = await client.get("/v1/models", headers=auth)
    assert catalogue.status_code == 200, catalogue.text
    aliases = {a for m in catalogue.json()["data"] for a in m.get("aliases") or []}
    assert "claude-sonnet-4-5" in aliases

    resp = await client.post(
        "/v1/chat/completions", headers=auth, json=_chat("claude-sonnet-4-5")
    )

    assert resp.status_code == 502, resp.text
    assert calls == ["claude-sonnet-4-5"]


@pytest.mark.asyncio
async def test_an_unknown_id_is_routed_when_a_provider_has_no_model_list(
    tmp_path, monkeypatch, http
):
    _isolated_providers(tmp_path, monkeypatch, [*LISTED, OPEN])
    client, auth, calls = http
    resp = await client.post(
        "/v1/chat/completions", headers=auth, json=_chat("nope:model-x")
    )

    assert resp.status_code == 502, resp.text
    assert calls == ["nope:model-x"]


# ── What the preflight answers ──────────────────────────────────────────────


@pytest.fixture
def pinged(http, tmp_path, monkeypatch):
    """http, with every ping answered and billed to a usage store of its own."""
    import plugboard.app as app_module
    from plugboard import usage

    client, auth, calls = http

    async def _forward(body, **_kwargs):
        calls.append(body.get("model"))
        return copy.deepcopy(FAKE_COMPLETION)

    monkeypatch.setattr(app_module, "forward", _forward)
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    return client, auth, calls


def _usage_rows() -> list:
    from plugboard import billing, usage

    return usage.get_usage(billing.current_pricing_month())


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


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["client key", "admin key"])
async def test_preflight_answers_model_not_found_without_a_ping(listed, pinged, caller):
    client, auth, calls = pinged
    if caller == "admin key":
        auth = {"Authorization": "Bearer admin-key-for-tests"}

    result = await _preflight(client, auth, "nope", "model-x")

    assert result["ok"] is False
    assert result["error"] == "model_not_found"
    assert (result["provider"], result["model"]) == ("nope", "model-x")
    assert calls == [], "no provider is pinged for a model no provider knows"
    assert _usage_rows() == [], "a ping that was not sent is not billed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "model"),
    [("acme-direct", "acme-large"), ("acme-direct", "not-listed")],
)
async def test_preflight_pings_a_known_model_as_before(listed, pinged, provider, model):
    client, auth, calls = pinged

    result = await _preflight(client, auth, provider, model)

    assert result["ok"] is True, result
    assert "error" not in result
    assert calls == [f"{provider}:{model}"]
    assert [row["provider_id"] for row in _usage_rows()] == [provider]


@pytest.mark.asyncio
async def test_preflight_pings_a_family_alias_as_before(dated_only, pinged):
    client, auth, calls = pinged

    result = await _preflight(client, auth, "anthropic-direct", "claude-sonnet-4-5")

    assert result["ok"] is True, result
    assert calls == ["anthropic-direct:claude-sonnet-4-5"]
    assert [row["provider_id"] for row in _usage_rows()] == ["anthropic-direct"]


@pytest.mark.asyncio
async def test_preflight_gives_a_limited_key_forbidden_model_first(listed, pinged):
    """As on chat: a limited key cannot learn which IDs exist elsewhere."""
    from plugboard import apikeys

    client, _auth, calls = pinged
    key = apikeys.create_key("acme", "limited", allowed_models=["acme-large"])["key"]

    result = await _preflight(
        client, {"Authorization": f"Bearer {key}"}, "nope", "model-x"
    )

    assert result["error"] == "forbidden_model"
    assert calls == []


@pytest.mark.asyncio
async def test_preflight_gives_a_budgeted_key_model_not_found_not_a_price(
    listed, pinged
):
    from plugboard import apikeys

    client, _auth, calls = pinged
    key = apikeys.create_key("acme", "budget", monthly_limit_usd=5.0)["key"]

    result = await _preflight(
        client, {"Authorization": f"Bearer {key}"}, "nope", "model-x"
    )

    assert result["error"] == "model_not_found"
    assert calls == []
    assert _usage_rows() == []


# ── The contract ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
def test_contract_documents_the_404(path):
    doc = api_contract.build_openapi()
    responses = doc["paths"][path]["post"]["responses"]
    assert "404" in responses
    assert "model_not_found" in responses["404"]["description"]
    error = doc["components"]["schemas"]["ErrorResponse"]["properties"]
    assert {"code", "hint"} <= set(error)
