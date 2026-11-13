"""A model the ChatGPT subscription does not serve through codex (BUG-78).

With a ChatGPT sign-in, codex refuses some models it lists in its own cache:
"The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT
account" (exit 1). The chat answered 502 "The codex provider failed", the
refusal quarantined the bare model name for every provider, counted against
the route's circuit, and an unqualified model was sent on to the next
candidate.

Now, like a prompt too long (D-45): the CLI wrapper flags the refusal from
codex's own error or its stderr, forward() raises it at once (no other
provider, no quarantine, no circuit or provider-down count), and the chat
answers 400 with ``code: model_not_supported_by_subscription``, streamed or
not. The codex route's catalogue leaves out the models a ChatGPT
subscription refuses (constat of 11/11), only for a ChatGPT sign-in: with an
API key they stay.

No CLI runs: the process is a fake. The HTTP provider is a real server on
127.0.0.1 that counts its calls.
"""

from __future__ import annotations

import json
import uuid

import pytest
from aiohttp import web

from tests.factories import FAKE_COMPLETION, PROVIDER_A

CLI_ID = "chatgpt-via-codex-cli"
REFUSED = "gpt-6.1-sol"
CLI_PROVIDER = {
    "id": CLI_ID,
    "name": "ChatGPT (via codex CLI)",
    "base_url": "subprocess://codex",
    "bearer_token": "(via CLI)",
    "default_model": "gpt-6-luna",
    "auth_source": "subprocess:codex",
    "provider_type": "cloud",
    "is_active": True,
    "models": [{"id": REFUSED, "name": REFUSED}, {"id": "gpt-6-luna"}],
    "created_at": "2026-01-01T00:00:00+00:00",
}
ADMIN = {"Authorization": "Bearer testkey"}  # the admin key in the fixture

# Process details a client key must never see.
_STDERR_NOISE = b"warning: terminal not detected /home/op/.codex token=abc123\n"
_PID = 4343
_REFUSAL = (
    f"The '{REFUSED}' model is not supported when using Codex with a ChatGPT account."
)
_STDERR_REFUSAL = _STDERR_NOISE + f"ERROR: {_REFUSAL}\n".encode()
# codex --json: the backend's 400 body comes back as a JSON string.
_JSON_REFUSAL = (
    b'{"type":"thread.started","thread_id":"t-1"}\n'
    b'{"type":"turn.started"}\n'
    + json.dumps(
        {
            "type": "error",
            "message": "unexpected status 400 Bad Request: "
            + json.dumps({"detail": _REFUSAL}),
        }
    ).encode()
    + b"\n"
    + json.dumps(
        {"type": "turn.failed", "error": {"message": json.dumps({"detail": _REFUSAL})}}
    ).encode()
    + b"\n"
)

# The models codex lists in its cache (models_cache.json, visibility "list").
_CODEX_CACHE = [
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6.1-terra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
]
_CHATGPT_REFUSED = {"gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-astra"}


def _chat(model: str = REFUSED, stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        # A cached answer from another test would skip the provider call.
        "cache": False,
    }


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """A private CODEX_HOME; ``sign_in("chatgpt"|"apikey"|None)`` writes it."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CODEX_API_KEY", raising=False)

    def sign_in(mode):
        auth = home / "auth.json"
        if mode is None:
            auth.unlink(missing_ok=True)
            return home
        data = {"auth_mode": mode, "OPENAI_API_KEY": None, "tokens": {}}
        auth.write_text(json.dumps(data), encoding="utf-8")
        return home

    def cache(slugs):
        entries = [
            {"slug": slug, "visibility": "list", "priority": i}
            for i, slug in enumerate(slugs)
        ]
        (home / "models_cache.json").write_text(json.dumps({"models": entries}))

    sign_in.cache = cache
    return sign_in


@pytest.fixture
def isolated(_providers_file, monkeypatch, tmp_path, codex_home):
    """Fresh breaker, alerts, metrics, admission and quarantine file.

    Returns the quarantine file, which no test may create.
    """
    import plugboard.alerts as alerts
    import plugboard.metrics as metrics
    import plugboard.model_quarantine as quarantine
    import plugboard.router as router
    import plugboard.session_store as sessions
    from plugboard import circuit_breaker as cb
    from plugboard import provider_admission

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    monkeypatch.setattr(metrics, "_stats", {})
    monkeypatch.setattr(sessions, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(sessions, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(sessions, "_INITIALISED", False)
    qfile = tmp_path / "quarantined_models.json"
    monkeypatch.setattr(quarantine, "_QUARANTINE_FILE", qfile)
    for fn in (
        quarantine.record_failure,
        quarantine.record_account_unsupported,
        quarantine.record_terminal_unavailable,
    ):
        monkeypatch.setitem(fn.__kwdefaults__, "path", qfile)
    provider_admission.reset()
    yield qfile
    provider_admission.reset()


@pytest.fixture
def cli_process(monkeypatch):
    """The real CLI wrapper runs on a fake process; returns its spawn list."""
    from unittest.mock import AsyncMock, MagicMock

    from plugboard import cli_subprocess as cs

    spawned: list = []

    def _setup(stdout: bytes, stderr: bytes = _STDERR_NOISE, returncode: int = 1):
        proc = MagicMock()
        proc.returncode = returncode
        proc.pid = _PID
        proc.communicate = AsyncMock(return_value=(stdout, stderr))

        async def _spawn(*args, **kwargs):
            spawned.append(args)
            return proc

        monkeypatch.setattr(cs, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
        monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", _spawn)
        return spawned

    return _setup


@pytest.fixture
async def http_provider(_providers_file, isolated):
    """The codex route first, then PROVIDER_A served on 127.0.0.1.

    Returns the list of request bodies the HTTP provider received.
    """
    from plugboard.router import invalidate_models_cache

    received: list[dict] = []

    async def handle(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response(FAKE_COMPLETION)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    http = {**PROVIDER_A, "base_url": f"http://127.0.0.1:{site.port}"}
    _providers_file.write_text(json.dumps({"providers": [CLI_PROVIDER, http]}))
    invalidate_models_cache()
    try:
        yield received
    finally:
        await runner.cleanup()


@pytest.fixture
def client_key(tmp_path, monkeypatch):
    """Authorization header of a plain client key (not the admin)."""
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    rec = ak.create_key(f"codex-sub-{uuid.uuid4().hex[:12]}", "test")
    return {"Authorization": f"Bearer {rec['key']}"}


# ── The CLI wrapper decides ───────────────────────────────────────────────


async def _codex_error():
    from plugboard import cli_subprocess as cs

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("codex", _chat())
    return info.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "stderr", "code"),
    [
        pytest.param(b"", _STDERR_REFUSAL, 1, id="stderr"),
        pytest.param(_JSON_REFUSAL, _STDERR_NOISE, 1, id="json-exit-1"),
        pytest.param(_JSON_REFUSAL, _STDERR_NOISE, 0, id="json-exit-0"),
    ],
)
async def test_the_codex_refusal_is_flagged(cli_process, stdout, stderr, code):
    cli_process(stdout, stderr, code)
    exc = await _codex_error()

    assert exc.model_unsupported is True
    assert exc.quota is False
    assert exc.request_too_long is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        pytest.param(
            b'{"type":"turn.failed","error":{"message":"model overloaded"}}\n',
            b"",
            id="another-error",
        ),
        # The CLI's error decides: the refusal text in stderr is ignored.
        pytest.param(
            b'{"type":"turn.failed","error":{"message":"model overloaded"}}\n',
            _STDERR_REFUSAL,
            id="cli-error-wins-over-stderr",
        ),
        # The model's own words are not the CLI's error.
        pytest.param(
            b'{"type":"item.completed","item":{"type":"agent_message","text":"'
            + _REFUSAL.encode()
            + b'"}}\n',
            b"",
            id="model-text",
        ),
    ],
)
async def test_another_codex_error_is_not_flagged(cli_process, stdout, stderr):
    cli_process(stdout, stderr, 1)
    exc = await _codex_error()

    assert exc.model_unsupported is False


def test_the_flag_defaults_to_false():
    from plugboard.cli_subprocess import CliSubprocessError

    assert CliSubprocessError("boom").model_unsupported is False
    assert CliSubprocessError("boom", model_unsupported=True).model_unsupported


def test_the_refusal_is_not_a_provider_fault():
    from plugboard.circuit_breaker import is_provider_fault

    assert is_provider_fault(f"CLI 'codex' exited 1: {_REFUSAL}") is False
    assert is_provider_fault("CLI 'codex' exited 1: model overloaded") is True


# ── The router stops at the codex route ──────────────────────────────────


@pytest.mark.asyncio
async def test_forward_does_not_send_the_refused_model_elsewhere(
    isolated, http_provider, cli_process
):
    import plugboard.alerts as alerts
    import plugboard.router as router
    from plugboard import circuit_breaker as cb
    from plugboard import provider_admission
    from plugboard.cli_subprocess import CliSubprocessError

    received = http_provider
    spawned = cli_process(b"", _STDERR_REFUSAL)

    with pytest.raises(CliSubprocessError) as info:
        await router.forward(_chat())

    assert info.value.model_unsupported is True
    assert len(spawned) == 1
    assert received == []
    assert all(state.failures == 0 for state in cb._circuits.values())
    assert alerts._provider_failures == {}
    assert not isolated.exists()
    assert provider_admission.in_flight(CLI_ID) == 0


@pytest.mark.asyncio
async def test_the_stream_route_neither_quarantines_nor_counts(
    isolated, http_provider, cli_process
):
    import plugboard.router as router
    from plugboard import circuit_breaker as cb
    from plugboard.cli_subprocess import CliSubprocessError

    cli_process(b"", _STDERR_REFUSAL)

    # Since PLU-26 a route that fails before its first chunk hands the stream
    # over to the next one, and no error event is sent: this refusal must stop
    # the stream instead, so the chat route answers 400 (no other provider).
    with pytest.raises(CliSubprocessError) as raised:
        [
            chunk
            async for chunk in router.forward_stream(_chat(f"{CLI_ID}:{REFUSED}", True))
        ]

    assert raised.value.model_unsupported
    assert all(state.failures == 0 for state in cb._circuits.values())
    assert not isolated.exists()


# ── What the client gets ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [REFUSED, f"Codex:{REFUSED}"], ids=["bare", "Codex"])
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_a_client_key_gets_400_model_not_supported_by_subscription(
    client, client_key, http_provider, cli_process, stream, model
):
    received = http_provider
    cli_process(b"", _STDERR_REFUSAL)

    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(model, stream)
    )

    assert resp.status_code == 400, resp.text
    assert "retry-after" not in resp.headers
    body = resp.json()
    assert body["code"] == "model_not_supported_by_subscription"
    ref = body["error_ref"]
    assert body["detail"] == (
        f"The codex provider's subscription does not support the model "
        f"'{REFUSED}': pick another model from GET /v1/models "
        f"(reference {ref} in the gateway log)."
    )
    assert body["hint"]
    assert body["provider_name"] == "codex"
    assert body["model_id"] == REFUSED
    for leak in ("/home/op", "token=abc123", str(_PID), "stderr", "exit_code"):
        assert leak not in resp.text
    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_the_admin_gets_400_with_process_details(
    client, http_provider, cli_process, stream
):
    received = http_provider
    cli_process(b"", _STDERR_REFUSAL)

    resp = await client.post(
        "/v1/chat/completions", headers=ADMIN, json=_chat(stream=stream)
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "model_not_supported_by_subscription"
    assert body["pid"] == _PID
    assert "not supported when using Codex" in body["detail"]
    assert body["exit_code"] == 1
    assert received == []


def test_the_contract_documents_the_code():
    from plugboard import api_contract

    code_doc = api_contract.ErrorResponse.model_fields["code"].description or ""
    assert "model_not_supported_by_subscription" in code_doc
    assert "model_not_supported_by_subscription" in api_contract._CHAT_ERRORS["400"]


# ── The codex route's catalogue ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_discovery_leaves_out_what_a_chatgpt_sign_in_refuses(codex_home):
    from plugboard import router

    codex_home("chatgpt")
    codex_home.cache(_CODEX_CACHE)

    found = [m["id"] for m in await router.discover_models(dict(CLI_PROVIDER))]

    assert found == [m for m in _CODEX_CACHE if m not in _CHATGPT_REFUSED]
    assert {"gpt-6-luna", "gpt-5.6-terra", "gpt-5.6-luna"} <= set(found)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["apikey", None], ids=["api-key", "signed-out"])
async def test_discovery_keeps_them_without_a_chatgpt_sign_in(codex_home, mode):
    from plugboard import router

    codex_home(mode)
    codex_home.cache(_CODEX_CACHE)

    found = [m["id"] for m in await router.discover_models(dict(CLI_PROVIDER))]

    assert found == _CODEX_CACHE


@pytest.mark.asyncio
async def test_a_codex_api_key_wins_over_the_chatgpt_sign_in(codex_home, monkeypatch):
    from plugboard import router

    codex_home("chatgpt")
    codex_home.cache(_CODEX_CACHE)
    monkeypatch.setenv("CODEX_API_KEY", "test-not-a-real-key")

    found = [m["id"] for m in await router.discover_models(dict(CLI_PROVIDER))]

    assert found == _CODEX_CACHE


@pytest.mark.asyncio
async def test_the_refresh_drops_them_from_the_saved_list_without_quarantine(
    isolated, codex_home
):
    from plugboard import model_availability as ma

    codex_home("chatgpt")
    provider = {
        **CLI_PROVIDER,
        "default_model": REFUSED,
        "models": [{"id": m, "name": m} for m in _CODEX_CACHE],
    }

    report = await ma.refresh_subprocess_provider_models(
        provider, probe_kinds=set(), quarantine_path=isolated
    )

    kept = [m["id"] for m in report["models"]]
    assert kept == [m for m in _CODEX_CACHE if m not in _CHATGPT_REFUSED]
    assert report["default_model"] == "gpt-6-luna"
    assert {d["id"] for d in report["dropped"]} == _CHATGPT_REFUSED
    assert {d["reason"] for d in report["dropped"]} == {"not_supported_by_subscription"}
    assert not isolated.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chatgpt", "apikey"])
async def test_the_published_catalogue_follows_the_sign_in(
    _providers_file, codex_home, mode
):
    from plugboard import router

    codex_home(mode)
    provider = {**CLI_PROVIDER, "models": [{"id": m} for m in _CODEX_CACHE]}
    _providers_file.write_text(json.dumps({"providers": [provider, PROVIDER_A]}))
    router.invalidate_models_cache()

    listed = {
        m["id"].split(":", 1)[1]
        for m in await router.list_models()
        if m.get("owned_by") == CLI_ID
    }

    if mode == "chatgpt":
        assert not listed & _CHATGPT_REFUSED
        assert {"gpt-6-luna", "gpt-5.6-terra", "gpt-5.6-luna"} <= listed
    else:
        assert _CHATGPT_REFUSED <= listed


# ── Routing follows the catalogue (review of BUG-78) ─────────────────────


@pytest.mark.asyncio
async def test_with_a_chatgpt_sign_in_a_bare_refused_name_goes_elsewhere(
    client, client_key, http_provider, cli_process, codex_home
):
    # Saved list not refreshed yet: the codex route still lists the model,
    # but with a ChatGPT sign-in it is neither published nor routed there.
    received = http_provider
    codex_home("chatgpt")
    spawned = cli_process(b"", _STDERR_REFUSAL)

    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 200, resp.text
    assert spawned == []
    assert len(received) == 1


@pytest.mark.asyncio
async def test_with_a_chatgpt_sign_in_the_codex_prefix_still_gets_400(
    client, client_key, http_provider, cli_process, codex_home
):
    received = http_provider
    codex_home("chatgpt")
    spawned = cli_process(b"", _STDERR_REFUSAL)

    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(f"Codex:{REFUSED}")
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "model_not_supported_by_subscription"
    assert len(spawned) == 1
    assert received == []


@pytest.mark.parametrize("mode", ["chatgpt", "apikey"])
def test_a_name_only_the_refusing_route_lists_is_unknown(
    _providers_file, codex_home, mode
):
    from plugboard import router

    codex_home(mode)
    listed_http = {**PROVIDER_A, "models": [{"id": "other-model"}]}
    _providers_file.write_text(json.dumps({"providers": [CLI_PROVIDER, listed_http]}))
    router.invalidate_models_cache()

    assert router.is_unknown_model(REFUSED) is (mode == "chatgpt")
    assert router.is_unknown_model(f"Codex:{REFUSED}") is False


@pytest.mark.asyncio
async def test_exit_0_refusal_is_named_and_not_counted_on_the_stream_route(
    isolated, http_provider, cli_process
):
    import plugboard.router as router
    from plugboard import circuit_breaker as cb
    from plugboard import cli_subprocess as cs

    cli_process(_JSON_REFUSAL, _STDERR_NOISE, 0)
    exc = await _codex_error()
    assert exc.model_unsupported is True
    assert "not supported when using Codex" in str(exc)

    # Since PLU-26 the route raises instead of sending an error event, and a
    # subscription's refusal stops the stream (D-51): the chat route answers
    # 400, with the CLI's message for the admin only.
    with pytest.raises(cs.CliSubprocessError) as raised:
        [
            chunk
            async for chunk in router.forward_stream(_chat(f"{CLI_ID}:{REFUSED}", True))
        ]

    assert raised.value.model_unsupported
    assert all(state.failures == 0 for state in cb._circuits.values())
    assert not isolated.exists()


@pytest.mark.parametrize(
    ("auth", "chatgpt"),
    [
        pytest.param({"tokens": {"id_token": "x"}}, True, id="tokens-no-mode"),
        pytest.param(
            {"tokens": {"id_token": "x"}, "OPENAI_API_KEY": "test-key"},
            False,
            id="tokens-and-key",
        ),
        pytest.param({"auth_mode": "apikey"}, False, id="apikey"),
        pytest.param("not json", False, id="unreadable"),
    ],
)
def test_the_sign_in_is_read_from_auth_json(codex_home, auth, chatgpt):
    from plugboard import cli_subprocess as cs

    home = codex_home(None)
    text = auth if isinstance(auth, str) else json.dumps(auth)
    (home / "auth.json").write_text(text, encoding="utf-8")

    assert cs.codex_uses_chatgpt_sign_in() is chatgpt
    assert bool(cs.codex_refused_models()) is chatgpt
