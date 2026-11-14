"""BUG-27: a key may list the exact ID /v1/models publishes, and nothing wider.

The published ID is a third accepted spelling next to the native ID and the
owner-qualified ID. The end-to-end checks go through the real registry file. A
provider whose ID is the alias itself owns that spelling: the entry then
designates that provider, never the CLI.
"""

from __future__ import annotations

import copy

import pytest
from fastapi import HTTPException

import plugboard.router as router
from plugboard.app import _model_route_guard
from plugboard.model_identity import model_access_matches
from tests.conftest import _setup_providers
from tests.factories import FAKE_COMPLETION, PROVIDER_A, PROVIDER_B

_CLAUDE = "claude-via-claude-cli"
_CODEX = "chatgpt-via-codex-cli"
_GEMINI = "google-gemini-via-gemini-cli"
_CLI_IDS = (_CLAUDE, _CODEX, _GEMINI)
_DISPLAY = {_CLAUDE: "claude-code", _CODEX: "Codex", _GEMINI: "Gemini"}


def _cli(provider_id, kind, *models, **extra):
    return {
        "id": provider_id,
        "name": provider_id,
        "base_url": "",
        "bearer_token": "",
        "auth_source": f"subprocess:{kind}",
        "default_model": models[0],
        "models": [{"id": m} for m in models],
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        **extra,
    }


def _http(provider_id, *models, **extra):
    return {
        "id": provider_id,
        "name": provider_id,
        "base_url": f"http://{provider_id.replace(':', '-').lower()}.test",
        "bearer_token": "synthetic-token",
        "default_model": models[0],
        "models": [{"id": m} for m in models],
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
        **extra,
    }


CLAUDE_CLI = _cli(_CLAUDE, "claude", "claude-sonnet-4-6", "claude-haiku-4-5")
CODEX_CLI = _cli(_CODEX, "codex", "gpt-6.1-sol", "gpt-6.1-mini")
GEMINI_CLI = _cli(_GEMINI, "gemini", "gemini-4-pro")
_THREE_CLIS = [PROVIDER_A, PROVIDER_B, CLAUDE_CLI, CODEX_CLI, GEMINI_CLI]
_PUBLISHED = [
    "Codex:gpt-6.1-sol",
    "Gemini:gemini-4-pro",
    "claude-code:claude-sonnet-4-6",
]


# ── the rule itself ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "entry, owner, native",
    [
        ("claude-code:claude-sonnet-4-6", _CLAUDE, "claude-sonnet-4-6"),
        ("Codex:gpt-6.1-sol", _CODEX, "gpt-6.1-sol"),
        ("Gemini:gemini-4-pro", _GEMINI, "gemini-4-pro"),
    ],
)
def test_published_id_grants_its_own_model(entry, owner, native):
    assert model_access_matches([entry], owner, native, _CLI_IDS) is True
    # ... and no sibling of the same provider.
    assert model_access_matches([entry], owner, native + "-pro", _CLI_IDS) is False


@pytest.mark.parametrize(
    "entry",
    [
        "Codex:",  # bare prefix
        "Codex",
        "Codex:gpt-6.1",  # prefix of the model name
        "odex:gpt-6.1-sol",  # suffix
        "codex:gpt-6.1-sol",  # case
        "CODEX:gpt-6.1-sol",
        "Codex:GPT-6.1-SOL",
        " Codex:gpt-6.1-sol",  # whitespace
        "Codex:gpt-6.1-sol ",
        "Codex: gpt-6.1-sol",
        "Codex:*",  # wildcards
        "*",
        "*:gpt-6.1-sol",
        "Codex:gpt-*",
        "claude-code:gpt-6.1-sol",  # another alias
    ],
)
def test_partial_or_altered_published_id_grants_nothing(entry):
    assert model_access_matches([entry], _CODEX, "gpt-6.1-sol", _CLI_IDS) is False


def test_bare_alias_prefix_grants_nothing_for_claude_code():
    for entry in ("claude-code:", "claude-code", "claude-code:claude-sonnet"):
        assert (
            model_access_matches([entry], _CLAUDE, "claude-sonnet-4-6", _CLI_IDS)
            is False
        )


@pytest.mark.parametrize(
    "entry, owner",
    [("Codex:", _CODEX), ("claude-code:", _CLAUDE), ("Gemini:", _GEMINI)],
)
def test_bare_alias_is_not_the_published_id_of_an_empty_model(entry, owner):
    """A route with no model name has no published ID to be listed under.

    Reached when a CLI provider has an empty default_model: the request
    'Codex:' resolves to (owner, ''), and the alias alone would equal it.
    """
    assert model_access_matches([entry], owner, "", _CLI_IDS) is False


@pytest.mark.parametrize("provider_ids", [None, _CLI_IDS])
@pytest.mark.parametrize("owner", _CLI_IDS)
def test_empty_model_name_is_never_granted(owner, provider_ids):
    """No spelling grants a route without a model name.

    The owner ID with an empty name ('chatgpt-via-codex-cli:') would equal the
    owner-qualified spelling, and the empty entry ('') the native one.
    """
    for entry in (f"{owner}:", "", _DISPLAY[owner] + ":"):
        assert model_access_matches([entry], owner, "", provider_ids) is False, entry


def test_empty_model_name_stays_open_to_an_unrestricted_key():
    """An empty allowed_models still means no restriction at all."""
    assert model_access_matches([], _CODEX, "", _CLI_IDS) is True
    assert model_access_matches([], _CODEX, "") is True


def test_published_id_needs_the_registry():
    """Callers that pass no registry (embeddings) keep the two former spellings."""
    assert model_access_matches(["Codex:gpt-6.1-sol"], _CODEX, "gpt-6.1-sol") is False
    assert model_access_matches(["gpt-6.1-sol"], _CODEX, "gpt-6.1-sol") is True
    assert (
        model_access_matches([f"{_CODEX}:gpt-6.1-sol"], _CODEX, "gpt-6.1-sol") is True
    )


def test_published_id_does_not_cover_another_owner():
    ids = (*_CLI_IDS, "test-provider", "my-codex")
    entry = ["Codex:gpt-6.1-sol"]
    assert model_access_matches(entry, "test-provider", "gpt-6.1-sol", ids) is False
    assert model_access_matches(entry, "my-codex", "gpt-6.1-sol", ids) is False


def test_provider_registered_under_the_alias_owns_the_spelling():
    """A provider whose ID is the alias owns that spelling, active or not."""
    ids = (*_CLI_IDS, "Codex")
    entry = ["Codex:gpt-6.1-sol"]
    assert model_access_matches(entry, "Codex", "gpt-6.1-sol", ids) is True
    assert model_access_matches(entry, _CODEX, "gpt-6.1-sol", ids) is False


def test_known_limit_native_homonym_is_also_granted():
    """An entry also equals another provider's native model of the same text.

    Native IDs are opaque and were already granted by name alone. An entry
    'Codex:gpt-6.1-sol' therefore covers the Codex CLI model and a provider
    whose native model is literally named 'Codex:gpt-6.1-sol' (an upstream
    gateway). Documented in docs/model-access-correction.md.
    """
    ids = (*_CLI_IDS, "upstream")
    entry = ["Codex:gpt-6.1-sol"]
    assert model_access_matches(entry, _CODEX, "gpt-6.1-sol", ids) is True
    assert model_access_matches(entry, "upstream", "Codex:gpt-6.1-sol", ids) is True
    assert model_access_matches(entry, "upstream", "gpt-6.1-sol", ids) is False


# ── harness: real registry file, real routes, no provider call ───────────────


@pytest.fixture
def keys(tmp_path, monkeypatch):
    import plugboard.apikeys as ak
    import plugboard.usage as u

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    ak._invalidate_cache()
    yield ak
    ak._invalidate_cache()


def _key(ak, allowed_models):
    rec = ak.create_key("limited-client", "synthetic", allowed_models=allowed_models)
    ak._invalidate_cache()
    return rec


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def cli_calls(monkeypatch, tmp_path):
    """Stand in for every CLI: records (kind, model), never spawns a process.

    The response cache gets its own database: the suite shares one, and an
    answer cached by another test file for the same client and prompt would
    be replayed here without any CLI call.
    """
    import plugboard.cache as response_cache

    monkeypatch.setattr(response_cache, "_DATA_DIR", tmp_path / "cache")
    monkeypatch.setattr(response_cache, "_DB_PATH", tmp_path / "cache" / "cache.db")
    calls = []

    async def _complete(kind, body, *, timeout_s=None, on_delta=None):
        calls.append((kind, body.get("model")))
        return copy.deepcopy(FAKE_COMPLETION)

    monkeypatch.setattr("plugboard.cli_subprocess.complete", _complete)
    return calls


@pytest.fixture
def registry(client, tmp_path, monkeypatch):
    """Rewrite the registry file the app reads."""

    def _set(providers):
        pfile = tmp_path / "data" / "providers.json"
        pfile.unlink()
        (tmp_path / "data").rmdir()
        _setup_providers(tmp_path, monkeypatch, providers)

    return _set


async def _ids(client, key, path="/v1/models"):
    resp = await client.get(path, headers=_auth(key))
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()["data"]]


async def _chat(client, key, model, *, stream=False):
    body = {"model": model, "messages": [{"role": "user", "content": "Hi"}]}
    if stream:
        body["stream"] = True
    return await client.post("/v1/chat/completions", headers=_auth(key), json=body)


async def _preflight_empty_model(client, key, owner):
    """Preflight the CLI under its alias and under its ID, without a model name."""
    resp = await client.post(
        "/v1/preflight",
        headers=_auth(key),
        json={
            "items": [
                {"provider": _DISPLAY[owner], "model": ""},
                {"provider": owner, "model": ""},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


_MODEL_LISTS = [
    "/v1/models",
    "/v1/models/benchmarks",
    "/v2/models",
    "/v2/models/benchmarks",
]


# ── end to end with a limited key ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _MODEL_LISTS)
async def test_lists_hold_exactly_the_published_ids_of_the_key(
    client, registry, keys, cli_calls, path
):
    registry(_THREE_CLIS)
    rec = _key(keys, list(_PUBLISHED))
    full = await _ids(client, "testkey", path)
    assert set(_PUBLISHED) < set(full)
    listed = await _ids(client, rec["key"], path)
    assert sorted(listed) == _PUBLISHED
    assert listed == [mid for mid in full if mid in _PUBLISHED]
    assert cli_calls == []


@pytest.mark.asyncio
async def test_chat_accepts_the_published_id_and_only_it(
    client, registry, keys, cli_calls
):
    registry(_THREE_CLIS)
    rec = _key(keys, ["Codex:gpt-6.1-sol"])

    ok = await _chat(client, rec["key"], "Codex:gpt-6.1-sol")
    assert ok.status_code == 200, ok.text
    assert cli_calls == [("codex", "gpt-6.1-sol")]

    for refused in (
        "Codex:gpt-6.1-mini",  # sibling under the same alias
        f"{_CODEX}:gpt-6.1-mini",
        "claude-code:claude-sonnet-4-6",  # another CLI
        "Gemini:gemini-4-pro",
        "test-provider:fake:model-v1",  # another provider
    ):
        resp = await _chat(client, rec["key"], refused)
        assert resp.status_code == 403, f"{refused}: {resp.status_code} {resp.text}"
    assert cli_calls == [("codex", "gpt-6.1-sol")]


@pytest.mark.asyncio
async def test_chat_accepts_every_spelling_of_the_granted_model(
    client, registry, keys, cli_calls
):
    """The grant is on the identity: the request may still use any alias."""
    registry(_THREE_CLIS)
    rec = _key(keys, ["Codex:gpt-6.1-sol"])
    for spelling in ("Codex:gpt-6.1-sol", f"{_CODEX}:gpt-6.1-sol"):
        resp = await _chat(client, rec["key"], spelling)
        assert resp.status_code == 200, f"{spelling}: {resp.text}"
    assert cli_calls == [("codex", "gpt-6.1-sol")] * 2


@pytest.mark.asyncio
async def test_model_info_follows_the_published_id(client, registry, keys, cli_calls):
    registry(_THREE_CLIS)
    rec = _key(keys, ["claude-code:claude-sonnet-4-6"])

    async def info(key, model_id):
        resp = await client.get(f"/v1/models/{model_id}/info", headers=_auth(key))
        return resp.status_code, resp.json()

    allowed = "claude-code:claude-sonnet-4-6"
    admin_view = await info("testkey", allowed)
    assert admin_view[1]["info_source"] == "benchmark_db"
    assert await info(rec["key"], allowed) == admin_view

    # A model the key may not call is refused as chat refuses it.
    refused_id = "other-provider:claude-sonnet-4-6"
    chat = await _chat(client, rec["key"], refused_id)
    assert chat.status_code == 403 and chat.json()["code"] == "forbidden_model"
    assert await info(rec["key"], refused_id) == (chat.status_code, chat.json())
    assert cli_calls == []


@pytest.mark.asyncio
async def test_preflight_follows_the_published_id(client, registry, keys, cli_calls):
    registry(_THREE_CLIS)
    rec = _key(keys, ["Codex:gpt-6.1-sol"])
    resp = await client.post(
        "/v1/preflight",
        headers=_auth(rec["key"]),
        json={
            "items": [
                {"provider": "Codex", "model": "gpt-6.1-sol"},
                {"provider": "Codex", "model": "gpt-6.1-mini"},
                {"provider": "claude-code", "model": "claude-sonnet-4-6"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["ok"] for r in results] == [True, False, False]
    assert [r.get("error") for r in results] == [
        None,
        "forbidden_model",
        "forbidden_model",
    ]
    assert cli_calls == [("codex", "gpt-6.1-sol")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    ["Codex:", "claude-code:", "Codex:gpt-6.1", "codex:gpt-6.1-sol", "Codex:*"],
)
async def test_partial_entries_still_open_nothing(
    client, registry, keys, cli_calls, entry
):
    registry(_THREE_CLIS)
    rec = _key(keys, [entry])
    assert await _ids(client, rec["key"]) == []
    resp = await _chat(client, rec["key"], "Codex:gpt-6.1-sol")
    assert resp.status_code == 403
    assert cli_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry, cli",
    [("Codex:", CODEX_CLI), ("claude-code:", CLAUDE_CLI), ("Gemini:", GEMINI_CLI)],
)
async def test_bare_alias_opens_nothing_when_the_cli_has_no_default_model(
    client, registry, keys, cli_calls, entry, cli
):
    """Without a default model, 'Codex:' must not open the CLI's fallback model."""
    registry([PROVIDER_A, dict(cli, default_model="", models=[])])
    rec = _key(keys, [entry])
    assert await _ids(client, rec["key"]) == []
    resp = await _chat(client, rec["key"], entry)
    assert resp.status_code == 403, resp.text
    assert cli_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["owner-id", "empty"])
@pytest.mark.parametrize(
    "cli", [CODEX_CLI, CLAUDE_CLI, GEMINI_CLI], ids=lambda c: c["id"]
)
async def test_empty_model_entry_opens_nothing_when_the_cli_has_no_default_model(
    client, registry, keys, cli_calls, cli, form
):
    """'chatgpt-via-codex-cli:' or '' must not open a model the key never named.

    Both requests resolve to the CLI with an empty model name. Past the guard,
    cli_subprocess.complete replaces it with the default model Plugboard
    hard-codes for that kind of CLI and passes that one with --model.
    """
    owner = cli["id"]
    registry([PROVIDER_A, dict(cli, default_model="", models=[])])
    rec = _key(keys, [f"{owner}:" if form == "owner-id" else ""])
    # Already empty before the fix: no catalogue entry has an empty name.
    for path in _MODEL_LISTS:
        assert await _ids(client, rec["key"], path) == [], path
    for request in (_DISPLAY[owner] + ":", f"{owner}:"):
        resp = await _chat(client, rec["key"], request)
        assert resp.status_code == 403, f"{request}: {resp.status_code} {resp.text}"
    assert cli_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["owner-id", "empty"])
@pytest.mark.parametrize(
    "cli", [CODEX_CLI, CLAUDE_CLI, GEMINI_CLI], ids=lambda c: c["id"]
)
async def test_empty_model_entry_is_refused_on_preflight(
    client, registry, keys, cli_calls, cli, form
):
    """Before the fix, the preflight pinged the CLI with the empty name."""
    owner = cli["id"]
    registry([PROVIDER_A, dict(cli, default_model="", models=[])])
    rec = _key(keys, [f"{owner}:" if form == "owner-id" else ""])
    results = await _preflight_empty_model(client, rec["key"], owner)
    assert [(r["ok"], r.get("error")) for r in results] == [
        (False, "forbidden_model")
    ] * 2, results
    assert cli_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["owner-id", "empty"])
@pytest.mark.parametrize(
    "cli", [CODEX_CLI, CLAUDE_CLI, GEMINI_CLI], ids=lambda c: c["id"]
)
async def test_empty_model_entry_is_refused_on_a_streamed_chat(
    client, registry, keys, cli_calls, cli, form
):
    """Before the fix, the streamed chat answered 200 from the CLI."""
    owner = cli["id"]
    registry([PROVIDER_A, dict(cli, default_model="", models=[])])
    rec = _key(keys, [f"{owner}:" if form == "owner-id" else ""])
    for request in (_DISPLAY[owner] + ":", f"{owner}:"):
        resp = await _chat(client, rec["key"], request, stream=True)
        assert resp.status_code == 403, f"{request}: {resp.status_code} {resp.text}"
    assert cli_calls == []


@pytest.mark.asyncio
async def test_unrestricted_key_keeps_the_cli_default_model(
    client, registry, keys, cli_calls
):
    """A key without allowed_models is not limited: the empty name still routes."""
    registry([PROVIDER_A, dict(CODEX_CLI, default_model="", models=[])])
    rec = _key(keys, [])
    for request in ("Codex:", f"{_CODEX}:"):
        resp = await _chat(client, rec["key"], request)
        assert resp.status_code == 200, f"{request}: {resp.status_code} {resp.text}"
    assert cli_calls == [("codex", "")] * 2


@pytest.mark.asyncio
async def test_unrestricted_key_keeps_preflight_and_stream_on_the_empty_name(
    client, registry, keys, cli_calls
):
    """Preflight and streamed chat stay open to a key without allowed_models."""
    registry([PROVIDER_A, dict(CODEX_CLI, default_model="", models=[])])
    rec = _key(keys, [])
    results = await _preflight_empty_model(client, rec["key"], _CODEX)
    assert [(r["ok"], r.get("error")) for r in results] == [(True, None)] * 2, results
    for request in ("Codex:", f"{_CODEX}:"):
        resp = await _chat(client, rec["key"], request, stream=True)
        assert resp.status_code == 200, f"{request}: {resp.status_code} {resp.text}"
    assert cli_calls == [("codex", "")] * 4


# ── security, through the real registry ──────────────────────────────────────


@pytest.mark.asyncio
async def test_active_provider_named_codex_owns_the_alias(
    client, registry, keys, cli_calls
):
    """'Codex:X' designates the provider registered as 'Codex', never the CLI."""
    registry([PROVIDER_A, CODEX_CLI, _http("Codex", "gpt-6.1-sol")])
    assert sorted(await _ids(client, "testkey")) == [
        "Codex:gpt-6.1-sol",
        f"{_CODEX}:gpt-6.1-mini",
        f"{_CODEX}:gpt-6.1-sol",
        "test-provider:fake:model-v1",
    ]
    rec = _key(keys, ["Codex:gpt-6.1-sol"])
    assert await _ids(client, rec["key"]) == ["Codex:gpt-6.1-sol"]

    guard = _model_route_guard("limited-client", rec)
    guard({"id": "Codex"}, "gpt-6.1-sol")
    with pytest.raises(HTTPException) as exc:
        guard(CODEX_CLI, "gpt-6.1-sol")
    assert exc.value.status_code == 403

    resp = await _chat(client, rec["key"], f"{_CODEX}:gpt-6.1-sol")
    assert resp.status_code == 403
    assert cli_calls == []


@pytest.mark.asyncio
async def test_inactive_provider_named_codex_still_reserves_the_alias(
    client, registry, keys, cli_calls
):
    """An inactive 'Codex' keeps its namespace: the CLI is not opened by it.

    The request 'Codex:X' is routed to the CLI once 'Codex' is inactive, but
    /v1/models publishes the CLI under its own ID, so the entry grants nothing.
    """
    registry([PROVIDER_A, CODEX_CLI, _http("Codex", "gpt-6.1-sol", is_active=False)])
    assert sorted(await _ids(client, "testkey")) == [
        f"{_CODEX}:gpt-6.1-mini",
        f"{_CODEX}:gpt-6.1-sol",
        "test-provider:fake:model-v1",
    ]
    assert router.resolve_provider_and_model("Codex:gpt-6.1-sol")[0]["id"] == _CODEX

    rec = _key(keys, ["Codex:gpt-6.1-sol"])
    assert await _ids(client, rec["key"]) == []
    for spelling in ("Codex:gpt-6.1-sol", f"{_CODEX}:gpt-6.1-sol"):
        resp = await _chat(client, rec["key"], spelling)
        assert resp.status_code == 403, f"{spelling}: {resp.text}"
    assert cli_calls == []


@pytest.mark.asyncio
async def test_nested_owner_under_the_alias_keeps_its_namespace(
    client, registry, keys, cli_calls
):
    """An owner 'claude-code:team' is not the Claude CLI's model 'team:m1'."""
    registry(
        [
            PROVIDER_A,
            _cli(_CLAUDE, "claude", "team:m1", "claude-sonnet-4-6"),
            _http("claude-code:team", "m1"),
        ]
    )
    assert sorted(await _ids(client, "testkey")) == [
        "claude-code:claude-sonnet-4-6",
        "claude-code:team:m1",
        f"{_CLAUDE}:team:m1",
        "test-provider:fake:model-v1",
    ]
    rec = _key(keys, ["claude-code:team:m1"])
    assert await _ids(client, rec["key"]) == ["claude-code:team:m1"]

    guard = _model_route_guard("limited-client", rec)
    guard({"id": "claude-code:team"}, "m1")
    with pytest.raises(HTTPException) as exc:
        guard({"id": _CLAUDE}, "team:m1")
    assert exc.value.status_code == 403

    resp = await _chat(client, rec["key"], f"{_CLAUDE}:team:m1")
    assert resp.status_code == 403
    assert cli_calls == []


@pytest.mark.asyncio
async def test_cli_registered_under_another_id_is_not_the_alias(
    client, registry, keys, cli_calls
):
    """Only the three canonical CLI IDs are published under an alias."""
    registry([PROVIDER_A, _cli("my-codex", "codex", "gpt-6.1-sol")])
    assert "my-codex:gpt-6.1-sol" in await _ids(client, "testkey")
    assert router.resolve_provider_and_model("Codex:gpt-6.1-sol")[0]["id"] == "my-codex"

    rec = _key(keys, ["Codex:gpt-6.1-sol"])
    assert await _ids(client, rec["key"]) == []
    resp = await _chat(client, rec["key"], "Codex:gpt-6.1-sol")
    assert resp.status_code == 403
    assert cli_calls == []


@pytest.mark.asyncio
async def test_fallback_candidate_outside_the_policy_is_refused(
    client, registry, keys, cli_calls, monkeypatch
):
    """The guard runs on the fallback too, not only on the first candidate.

    The first candidate is the granted model and fails; the second one is
    outside the policy and must be refused before it is called.
    """
    registry(_THREE_CLIS)
    rec = _key(keys, ["claude-code:claude-sonnet-4-6"])

    async def _claude_is_down(kind, body, *, timeout_s=None, on_delta=None):
        cli_calls.append((kind, body.get("model")))
        if kind == "claude":
            raise RuntimeError("synthetic CLI failure")
        return copy.deepcopy(FAKE_COMPLETION)

    monkeypatch.setattr("plugboard.cli_subprocess.complete", _claude_is_down)
    monkeypatch.setattr(
        router,
        "_resolve_all_providers",
        lambda _m: [(CLAUDE_CLI, "claude-sonnet-4-6"), (CODEX_CLI, "gpt-6.1-sol")],
    )
    monkeypatch.setattr(
        "plugboard.circuit_breaker.check_circuit",
        lambda _m: {"allowed": True, "fallback": None},
    )
    # The synthetic failure must not stay in the process-wide failure counters.
    monkeypatch.setattr("plugboard.circuit_breaker.record_failure", lambda _m: None)

    async def _no_alert(_provider_id):
        return None

    monkeypatch.setattr(router, "record_provider_failure", _no_alert)
    guard = _model_route_guard("limited-client", rec)
    with pytest.raises(HTTPException) as exc:
        await router.forward(
            {"model": "claude-code:claude-sonnet-4-6", "messages": []},
            route_guard=guard,
        )
    assert exc.value.status_code == 403
    assert router.served_by() is None
    assert cli_calls == [("claude", "claude-sonnet-4-6")]


# ── cost: one registry read per guard, one guard per list ────────────────────


def test_guard_reads_the_registry_once(registry, keys, monkeypatch):
    registry(_THREE_CLIS)
    rec = _key(keys, ["Codex:gpt-6.1-sol"])
    reads = []
    real = router.load_providers

    def counting():
        reads.append(1)
        return real()

    monkeypatch.setattr(router, "load_providers", counting)
    guard = _model_route_guard("limited-client", rec)
    guard(CODEX_CLI, "gpt-6.1-sol")
    guard(CODEX_CLI, "gpt-6.1-sol")
    with pytest.raises(HTTPException):
        guard(CLAUDE_CLI, "claude-sonnet-4-6")
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_model_list_builds_one_guard_per_request(
    client, registry, keys, monkeypatch
):
    import plugboard.app as app_mod

    registry(_THREE_CLIS)
    rec = _key(keys, ["Codex:gpt-6.1-sol"])
    assert len(await _ids(client, "testkey")) > 1
    built = []
    real = app_mod._model_route_guard

    def counting(*args, **kwargs):
        built.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(app_mod, "_model_route_guard", counting)
    assert await _ids(client, rec["key"]) == ["Codex:gpt-6.1-sol"]
    assert len(built) == 1
