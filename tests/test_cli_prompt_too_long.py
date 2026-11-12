"""A CLI subscription route that finds the prompt too long answers 400.

The CLI said the messages do not fit the model's context ("Prompt is too
long", "prompt is too long: 250000 tokens > 200000 maximum"): the request
caused it, so forward() sent it on to the next candidate provider and the
client got 502 once every candidate had failed. The same messages fail the
same way anywhere, so forward() now raises at once: no other provider is
called, the route's circuit and the model quarantine are left alone, the
provider slot is given back, and the chat route answers 400 with
``code: context_length_exceeded``.

The CLI wrapper decides it, like the quota, from the CLI's own error or its
stderr, never from the model's answer. No CLI runs: the process is a fake.
The HTTP provider is a real server on 127.0.0.1 that counts its calls.
"""

from __future__ import annotations

import json
import uuid

import pytest
from aiohttp import web

from tests.factories import FAKE_COMPLETION, PROVIDER_A

CLI_ID = "pbtest-claude-cli"
CLI_PROVIDER = {
    "id": CLI_ID,
    "name": "Claude CLI test",
    "base_url": "subprocess://claude",
    "bearer_token": "(via CLI)",
    "default_model": "pbtest-claude-model",
    "auth_source": "subprocess:claude",
    "provider_type": "cloud",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
# No registered prefix: every active provider is a candidate, in file order.
ANY_ROUTE = "pbtest-shared-model"
ADMIN = {"Authorization": "Bearer testkey"}  # the admin key in the fixture

# Process details a client key must never see.
_STDERR = b"warning: terminal not detected /home/op/.config token=abc123\n"
_PID = 4242

_CLAUDE_TOO_LONG = b'{"type":"result","is_error":true,"result":"Prompt is too long"}\n'
_CLAUDE_TOO_LONG_EXIT_0 = (
    b'{"type":"result","subtype":"success","is_error":true,'
    b'"result":"Prompt is too long"}\n'
)


def _chat(model: str = ANY_ROUTE, stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        # A cached answer from another test would skip the provider call.
        "cache": False,
    }


@pytest.fixture
def isolated(_providers_file, monkeypatch, tmp_path):
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
    # Equal load-balancing scores keep the candidates in providers.json order.
    monkeypatch.setattr(metrics, "_stats", {})
    monkeypatch.setattr(sessions, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(sessions, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(sessions, "_INITIALISED", False)
    # The record functions bound the file as a default when imported.
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

    def _setup(stdout: bytes, stderr: bytes = _STDERR, returncode: int = 1) -> list:
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
    """The CLI route first, then PROVIDER_A served on 127.0.0.1.

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
    rec = ak.create_key(f"too-long-{uuid.uuid4().hex[:12]}", "test")
    return {"Authorization": f"Bearer {rec['key']}"}


# ── The CLI wrapper decides ───────────────────────────────────────────────


async def _cli_error(kind: str):
    from plugboard import cli_subprocess as cs

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete(kind, {**_chat(), "model": "some-model"})
    return info.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "stdout", "stderr", "code"),
    [
        pytest.param("claude", _CLAUDE_TOO_LONG, _STDERR, 1, id="claude-exit-1"),
        pytest.param("claude", _CLAUDE_TOO_LONG_EXIT_0, _STDERR, 0, id="claude-exit-0"),
        pytest.param(
            "codex",
            b"",
            b"prompt is too long: 250000 tokens > 200000 maximum\n",
            1,
            id="stderr-only",
        ),
    ],
)
async def test_a_cli_saying_the_prompt_is_too_long_is_flagged(
    cli_process, kind, stdout, stderr, code
):
    cli_process(stdout, stderr, code)
    exc = await _cli_error(kind)

    assert exc.request_too_long is True
    assert exc.quota is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "stdout", "stderr", "code"),
    [
        pytest.param(
            "claude",
            b'{"type":"result","is_error":true,"result":"model not found"}\n',
            _STDERR,
            1,
            id="model-not-found",
        ),
        # The CLI's error decides: a "too long" line in stderr is ignored.
        pytest.param(
            "claude",
            b'{"type":"result","is_error":true,"result":"model not found"}\n',
            b"retrying: prompt is too long\n",
            1,
            id="cli-error-wins-over-stderr",
        ),
        # The model's own words are not the CLI's error.
        pytest.param(
            "codex",
            b'{"type":"item.completed","item":{"type":"agent_message",'
            b'"text":"Your prompt is too long for the context window"}}\n',
            b"",
            1,
            id="model-text",
        ),
    ],
)
async def test_another_cli_error_is_not_flagged(
    cli_process, kind, stdout, stderr, code
):
    cli_process(stdout, stderr, code)
    exc = await _cli_error(kind)

    assert exc.request_too_long is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    ["You've hit your limit", "Claude AI usage limit reached (context window)"],
)
async def test_a_quota_is_never_flagged_too_long(cli_process, result):
    event = {"type": "result", "is_error": True, "result": result}
    cli_process(json.dumps(event).encode() + b"\n")
    exc = await _cli_error("claude")

    assert exc.quota is True
    assert exc.request_too_long is False


def test_the_flag_defaults_to_false():
    from plugboard.cli_subprocess import CliSubprocessError

    assert CliSubprocessError("boom").request_too_long is False
    assert CliSubprocessError("boom", request_too_long=True).request_too_long is True


# ── The router stops at the CLI route ────────────────────────────────────


@pytest.mark.asyncio
async def test_forward_does_not_send_a_prompt_too_long_elsewhere(
    isolated, http_provider, cli_process
):
    import plugboard.router as router
    from plugboard import circuit_breaker as cb
    from plugboard import provider_admission
    from plugboard.cli_subprocess import CliSubprocessError

    received = http_provider
    spawned = cli_process(_CLAUDE_TOO_LONG)

    with pytest.raises(CliSubprocessError) as info:
        await router.forward(_chat())

    assert info.value.request_too_long is True
    assert len(spawned) == 1
    assert received == []
    assert all(state.failures == 0 for state in cb._circuits.values())
    assert not isolated.exists()
    assert provider_admission.in_flight(CLI_ID) == 0


@pytest.mark.asyncio
async def test_forward_still_falls_back_on_another_cli_error(
    isolated, http_provider, cli_process
):
    import plugboard.router as router

    received = http_provider
    cli_process(b'{"type":"result","is_error":true,"result":"model overloaded"}\n')

    result = await router.forward(_chat())

    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert len(received) == 1


# ── What the client gets ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_a_client_key_gets_400_without_process_details(
    client, client_key, http_provider, cli_process, stream
):
    received = http_provider
    cli_process(_CLAUDE_TOO_LONG)

    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream=stream)
    )

    assert resp.status_code == 400, resp.text
    assert "retry-after" not in resp.headers
    body = resp.json()
    assert body["code"] == "context_length_exceeded"
    ref = body["error_ref"]
    assert body["detail"] == (
        "The claude provider rejected the request: the messages are too long "
        f"for the model's context (reference {ref} in the gateway log)."
    )
    assert body["provider_name"] == "claude"
    assert "model_id" in body
    for leak in ("/home/op", "token=abc123", str(_PID), "stderr", "exit_code"):
        assert leak not in resp.text
    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_the_admin_gets_400_with_process_details(
    client, http_provider, cli_process, stream
):
    received = http_provider
    cli_process(_CLAUDE_TOO_LONG)

    resp = await client.post(
        "/v1/chat/completions", headers=ADMIN, json=_chat(stream=stream)
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "context_length_exceeded"
    assert body["pid"] == _PID
    assert "token=abc123" in body["stderr"]
    assert body["exit_code"] == 1
    assert "Prompt is too long" in body["detail"]
    assert body["error_ref"]
    assert received == []


@pytest.mark.asyncio
async def test_an_answer_about_a_small_context_window_is_served(
    client, client_key, http_provider, cli_process
):
    received = http_provider
    answer = "the context window is too small"
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": answer,
    }
    cli_process(json.dumps(event).encode() + b"\n", returncode=0)

    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == answer
    assert received == []
