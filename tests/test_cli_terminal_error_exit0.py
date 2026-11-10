"""A CLI that exits 0 after a terminal error is not an answer (BUG-29).

claude, codex and gemini can end a run in error and still exit 0. The text
around that error (claude's ``result`` of an ``is_error`` event, a partial
assistant message, gemini's ``response`` next to an ``error`` object) was
served as HTTP 200, billed to the client key, stored as the session's
assistant turn and written to the response cache. It is now a provider
failure like a non-zero exit: 502, or 429 on a quota, after the other
candidate providers were tried.

Every test goes through the real router (``router.forward``): only the CLI
process and the HTTP upstream are fakes.
"""

from __future__ import annotations

import json
import uuid

import pytest
from aioresponses import aioresponses
from yarl import URL

from tests.factories import FAKE_COMPLETION, PROVIDER_A

_A = "http://fake-llm.test/v1/chat/completions"
# Unqualified on purpose: every active provider is a candidate.
_MODEL = "fake:model-v1"
_CLI_ID = "pbtest-cli"
_QUIET = b"warning: terminal not detected\n"


@pytest.fixture(autouse=True)
def _closed_circuits(monkeypatch):
    """Failures of one test never open the breaker for the next one."""
    import plugboard.alerts as alerts
    import plugboard.circuit_breaker as breaker

    monkeypatch.setattr(breaker, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})


@pytest.fixture(autouse=True)
def _own_session_store(tmp_path, monkeypatch):
    """The session store keeps the database path of its first import (the
    tmp_path of the first test, removed once that test passes) and creates
    its table once per process: give each test its own file."""
    import plugboard.session_store as sessions

    monkeypatch.setattr(sessions, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(sessions, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(sessions, "_INITIALISED", False)


@pytest.fixture
def key(tmp_path, monkeypatch):
    """A plain client key: (headers with a session id, key_id)."""
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    # A client of its own: the cache and the sessions are kept per client.
    rec = ak.create_key(f"b29-{uuid.uuid4().hex[:12]}", "test")
    headers = {
        "Authorization": f"Bearer {rec['key']}",
        "X-LLM-Session-ID": f"session-{uuid.uuid4().hex[:12]}",
    }
    return headers, rec["key_id"]


@pytest.fixture
def providers(_providers_file, monkeypatch):
    """A CLI provider of `kind`, alone or ahead of an HTTP provider."""
    import plugboard.router as router

    def _setup(kind: str, fallback: bool = False) -> None:
        cli = {
            "id": _CLI_ID,
            "name": "CLI test",
            "base_url": f"subprocess://{kind}",
            "bearer_token": "(via CLI)",
            "default_model": "pbtest-model",
            "auth_source": f"subprocess:{kind}",
            "is_active": True,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        listed = [cli, PROVIDER_A] if fallback else [cli]
        _providers_file.write_text(json.dumps({"providers": listed}))
        router.invalidate_models_cache()
        monkeypatch.setattr(
            router.metrics,
            "get_stats",
            lambda: {
                _CLI_ID: {"latency_avg": 0.1, "requests": 100, "errors": 0},
                "test-provider": {"latency_avg": 2.0, "requests": 100, "errors": 0},
            },
        )
        monkeypatch.setattr(router, "_IN_FLIGHT_BY_PROVIDER", {})

    return _setup


@pytest.fixture
def cli_process(monkeypatch):
    """The real CLI wrapper runs on a fake process (exit 0 by default)."""
    from unittest.mock import AsyncMock, MagicMock

    from plugboard import cli_subprocess as cs

    def _setup(stdout: bytes, stderr: bytes = _QUIET, returncode: int = 0) -> None:
        proc = MagicMock()
        proc.returncode = returncode
        proc.pid = 4242
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        monkeypatch.setattr(cs, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
        monkeypatch.setattr(
            cs.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

    return _setup


class _Ledger:
    """What a request left behind: cache writes, session turns, usage lines."""

    def __init__(self) -> None:
        self.cached: list = []
        self.turns: list = []

    @staticmethod
    def usage(key_id: str) -> list:
        from plugboard import billing, usage

        return usage.get_usage_by_key(billing.current_pricing_month(), key_id)


@pytest.fixture
def ledger(monkeypatch):
    import plugboard.cache as cache
    import plugboard.session_store as sessions

    seen = _Ledger()
    real_set, real_record = cache.set_cached, sessions.record_exchange

    def _set_cached(*args, **kwargs):
        seen.cached.append(args[3])
        return real_set(*args, **kwargs)

    def _record_exchange(client_id, session_id, messages, assistant_text):
        seen.turns.append(assistant_text)
        return real_record(client_id, session_id, messages, assistant_text)

    monkeypatch.setattr(cache, "set_cached", _set_cached)
    monkeypatch.setattr(sessions, "record_exchange", _record_exchange)
    return seen


def _chat(stream: bool = False) -> dict:
    # No "cache": False here: the response cache is on, as for a real client.
    return {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }


def _claude(*events: dict) -> bytes:
    return "".join(json.dumps(evt) + "\n" for evt in events).encode()


def _assistant(text: str, **extra) -> dict:
    message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    return {"type": "assistant", "message": message, **extra}


_CODEX_ANSWER = (
    b'{"type":"thread.started","thread_id":"0199-test"}\n'
    b'{"type":"turn.started"}\n'
    b'{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
    b'"text":"PARTIAL-TEXT the answer starts"}}\n'
)

# Every form ends the run in error and exits 0. "PARTIAL-TEXT" marks what
# the CLI printed around the error: none of it may be served.
_TERMINAL_ERRORS = [
    pytest.param(
        "claude",
        _claude(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 500,
                "result": "PARTIAL-TEXT API Error: internal failure",
            }
        ),
        id="claude-is-error-with-result",
    ),
    pytest.param(
        "claude",
        _claude(
            _assistant("PARTIAL-TEXT Prompt is too long", error="invalid_request"),
            {"type": "result", "subtype": "success", "is_error": True},
        ),
        id="claude-is-error-without-result",
    ),
    pytest.param(
        "claude",
        _claude(
            _assistant("PARTIAL-TEXT the answer starts"),
            {"type": "result", "subtype": "success", "is_error": True, "result": ""},
        ),
        id="claude-is-error-with-empty-result",
    ),
    pytest.param(
        "claude",
        _claude(
            _assistant("PARTIAL-TEXT the answer starts"),
            {
                "type": "result",
                "subtype": "error_during_execution",
                "errors": ["the stream was cut"],
            },
        ),
        id="claude-error-subtype-after-a-partial-answer",
    ),
    pytest.param(
        "codex",
        _CODEX_ANSWER
        + b'{"type":"turn.failed","error":{"message":"stream disconnected before'
        b' completion"}}\n',
        id="codex-turn-failed-after-an-answer",
    ),
    pytest.param(
        "codex",
        _CODEX_ANSWER + b'{"type":"turn.failed"}\n',
        id="codex-turn-failed-without-message",
    ),
    pytest.param(
        "gemini",
        b'{"response": "PARTIAL-TEXT the answer starts", "error": {"type":'
        b' "ApiError", "message": "internal failure", "code": 1}}',
        id="gemini-error-object-next-to-a-response",
    ),
    pytest.param(
        "gemini",
        b'{"error": {"type": "ApiError", "message": "internal failure", "code": 1}}',
        id="gemini-error-object-alone",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stdout"), _TERMINAL_ERRORS)
async def test_terminal_error_at_exit_0_is_not_served_billed_cached_or_recorded(
    client, key, providers, cli_process, ledger, kind, stdout
):
    headers, key_id = key
    providers(kind)
    cli_process(stdout)

    resp = await client.post("/v1/chat/completions", headers=headers, json=_chat())

    assert resp.status_code == 502, resp.text
    assert "PARTIAL-TEXT" not in resp.text
    assert "internal failure" not in resp.text
    assert ledger.usage(key_id) == []
    assert ledger.cached == []
    assert ledger.turns == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stdout"), _TERMINAL_ERRORS)
async def test_terminal_error_at_exit_0_falls_back_to_the_next_provider(
    client, key, providers, cli_process, ledger, kind, stdout
):
    headers, key_id = key
    providers(kind, fallback=True)
    cli_process(stdout)

    with aioresponses() as mock:
        mock.post(_A, payload=FAKE_COMPLETION, status=200)
        resp = await client.post("/v1/chat/completions", headers=headers, json=_chat())
        assert len(mock.requests.get(("POST", URL(_A)), [])) == 1

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"
    # What is billed, cached and recorded is the answer of the HTTP provider.
    assert ledger.turns == ["Hello!"]
    assert [c["choices"][0]["message"]["content"] for c in ledger.cached] == ["Hello!"]
    assert len(ledger.usage(key_id)) == 1


@pytest.mark.asyncio
async def test_terminal_error_at_exit_0_is_not_streamed_either(
    client, key, providers, cli_process, ledger
):
    headers, key_id = key
    providers("claude")
    cli_process(_TERMINAL_ERRORS[0].values[1])

    resp = await client.post(
        "/v1/chat/completions", headers=headers, json=_chat(stream=True)
    )

    assert resp.status_code == 502, resp.text
    assert "PARTIAL-TEXT" not in resp.text
    assert ledger.usage(key_id) == []
    assert ledger.cached == []
    assert ledger.turns == []


@pytest.mark.asyncio
async def test_codex_reconnect_on_a_rate_limit_then_a_failed_turn_is_not_a_quota(
    client, key, providers, cli_process, ledger
):
    """The rate limit codex reconnected from is not why the run failed: it
    answered after it. A bare turn.failed gives no reason, so 502, not 429."""
    headers, key_id = key
    providers("codex")
    cli_process(
        b'{"type":"thread.started","thread_id":"0199-test"}\n'
        b'{"type":"turn.started"}\n'
        b'{"type":"error","message":"Reconnecting... 1/5 (rate limit exceeded)"}\n'
        b'{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
        b'"text":"PARTIAL-TEXT Paris."}}\n'
        b'{"type":"turn.failed"}\n'
    )

    resp = await client.post("/v1/chat/completions", headers=headers, json=_chat())

    assert resp.status_code == 502, resp.text
    assert "PARTIAL-TEXT" not in resp.text
    assert ledger.usage(key_id) == []
    assert ledger.cached == []
    assert ledger.turns == []


# ── Quota read from claude's structured events ────────────────────────────

_CLAUDE_REJECTED = "API Error: Request rejected (429)"
_CLAUDE_RATE_LIMITED = [
    pytest.param(
        _claude(
            _assistant(_CLAUDE_REJECTED, error="rate_limit"),
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": _CLAUDE_REJECTED,
            },
        ),
        id="assistant-error-rate-limit",
    ),
    pytest.param(
        _claude(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 429,
                "result": _CLAUDE_REJECTED,
            }
        ),
        id="result-api-error-status-429",
    ),
    pytest.param(
        _claude(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": []},
                "error": "rate_limit",
            },
            {"type": "result", "subtype": "error_during_execution", "is_error": True},
        ),
        id="assistant-error-rate-limit-without-any-text",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize("stdout", _CLAUDE_RATE_LIMITED)
async def test_claude_structured_rate_limit_gives_429(
    client, key, providers, cli_process, ledger, stdout, code
):
    headers, key_id = key
    providers("claude")
    cli_process(stdout, returncode=code)

    resp = await client.post("/v1/chat/completions", headers=headers, json=_chat())

    assert resp.status_code == 429, resp.text
    assert "quota" in resp.json()["detail"].lower()
    assert "Request rejected" not in resp.text
    assert ledger.usage(key_id) == []
    assert ledger.cached == []
    assert ledger.turns == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 1])
async def test_claude_other_structured_error_stays_502(
    client, key, providers, cli_process, code
):
    headers, _ = key
    providers("claude")
    cli_process(
        _claude(
            _assistant("API Error: overloaded", error="overloaded"),
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 529,
                "result": "API Error: overloaded",
            },
        ),
        returncode=code,
    )

    resp = await client.post("/v1/chat/completions", headers=headers, json=_chat())

    assert resp.status_code == 502, resp.text


# ── What must keep working ────────────────────────────────────────────────

_CLAUDE_SAYS_RATE_LIMIT = _claude(
    _assistant("The API answers: rate limit reached, then retry."),
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "The API answers: rate limit reached, then retry.",
    },
)
_CODEX_RECONNECT_THEN_ANSWER = (
    b'{"type":"thread.started","thread_id":"0199-test"}\n'
    b'{"type":"turn.started"}\n'
    b'{"type":"error","message":"Reconnecting... 1/5 (rate limit exceeded)"}\n'
    b'{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
    b'"text":"Paris."}}\n'
    b'{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n'
)
_SERVED = [
    pytest.param(
        "codex",
        _CODEX_RECONNECT_THEN_ANSWER,
        _QUIET,
        "Paris.",
        id="codex-reconnect-then-answer-and-turn-completed",
    ),
    pytest.param(
        "claude",
        _CLAUDE_SAYS_RATE_LIMIT,
        _QUIET,
        "The API answers: rate limit reached, then retry.",
        id="claude-answer-quotes-rate-limit-reached",
    ),
    pytest.param(
        "claude",
        _claude(
            _assistant("Paris."),
            {"type": "result", "subtype": "success", "is_error": False},
        ),
        b"retrying: rate limit reached\n",
        "Paris.",
        id="claude-rate-limit-reached-in-stderr-only",
    ),
    pytest.param(
        "codex",
        _CODEX_RECONNECT_THEN_ANSWER,
        b"retrying: rate limit reached\n",
        "Paris.",
        id="codex-rate-limit-reached-in-stderr-only",
    ),
    pytest.param(
        "gemini",
        b'{"response": "Paris.", "stats": {}}',
        b"retrying: rate limit reached\n",
        "Paris.",
        id="gemini-rate-limit-reached-in-stderr-only",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stdout", "stderr", "answer"), _SERVED)
async def test_a_real_answer_is_served_billed_cached_and_recorded(
    client, key, providers, cli_process, ledger, kind, stdout, stderr, answer
):
    headers, key_id = key
    providers(kind)
    cli_process(stdout, stderr=stderr)

    resp = await client.post("/v1/chat/completions", headers=headers, json=_chat())

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == answer
    # The same ledger the failure tests read: their empty lists mean something.
    assert len(ledger.usage(key_id)) == 1
    assert ledger.turns == [answer]
    assert [c["choices"][0]["message"]["content"] for c in ledger.cached] == [answer]


# stderr still names the quota when the CLI's own stream gives no outcome.
_QUOTA_IN_STDERR = [
    pytest.param(
        "codex",
        b'{"type":"turn.started"}\n'
        b'{"type":"error","message":"Reconnecting... 5/5 (stream disconnected)"}\n',
        b"ERROR: exceeded retry limit, last status: 429 Too Many Requests\n",
        0,
        id="codex-reconnecting-5-of-5-exit-0",
    ),
    pytest.param(
        "gemini",
        b"",
        b"429 Resource exhausted\n",
        1,
        id="gemini-empty-stdout-exit-1",
    ),
    pytest.param(
        "gemini",
        b'{"error": {"type": "ApiError", "message": "request failed", "code": 1}}',
        b"Attempt 3 failed: Resource has been exhausted (e.g. check quota).\n",
        0,
        id="gemini-error-object-exit-0",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stdout", "stderr", "code"), _QUOTA_IN_STDERR)
async def test_quota_named_in_stderr_only_still_gives_429(
    client, key, providers, cli_process, ledger, kind, stdout, stderr, code
):
    headers, key_id = key
    providers(kind)
    cli_process(stdout, stderr=stderr, returncode=code)

    resp = await client.post("/v1/chat/completions", headers=headers, json=_chat())

    assert resp.status_code == 429, resp.text
    assert ledger.usage(key_id) == []
