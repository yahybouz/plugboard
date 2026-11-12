"""An exhausted provider quota answers 429 with a readable reason (BUG-26).

Before the fix, an upstream HTTP 429 (after retries and fallback) and a
subscription CLI that failed on its quota both reached the client as a
generic 502. A client key now gets 429, a reason composed by the gateway
(provider + quota/limit exhausted + delay when the upstream gives one) and
never the upstream body or the CLI's stderr, pid or paths; the admin keeps
the details. A fallback provider that answers still wins (200).
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from yarl import URL

from tests.factories import FAKE_COMPLETION

_A = "http://fake-llm.test/v1/chat/completions"
_B = "http://other-llm.test/v1/chat/completions"
# Unqualified on purpose: both providers are candidates, so the fallback runs.
_MODEL = "fake:model-v1"
_UPSTREAM_BODY = '{"error": {"message": "upstream-secret org-42 quota"}}'
ADMIN = {"Authorization": "Bearer testkey"}  # the admin key in the fixture


@pytest.fixture
def no_retry_delay(monkeypatch):
    import plugboard.router as router

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])


@pytest.fixture
def a_first(monkeypatch):
    """test-provider is the preferred candidate, other-provider the fallback."""
    import plugboard.router as router

    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 100, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 100, "errors": 0},
        },
    )


@pytest.fixture
def client_key_record(tmp_path, monkeypatch):
    """(Authorization header, key_id) of a plain client key (not the admin)."""
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    rec = ak.create_key("quota-client", "test")
    return {"Authorization": f"Bearer {rec['key']}"}, rec["key_id"]


@pytest.fixture
def client_key(client_key_record):
    """Authorization header of a plain client key (not the admin)."""
    return client_key_record[0]


def _chat(stream: bool = False) -> dict:
    return {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        # A cached answer from another test would skip the provider call.
        "cache": False,
    }


def _calls(mock, url: str) -> int:
    return len(mock.requests.get(("POST", URL(url)), []))


# ── HTTP provider answers 429 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upstream_429_with_retry_after_gives_429_to_a_client_key(
    client, client_key, a_first, no_retry_delay
):
    with aioresponses() as mock:
        for url in (_A, _B):
            mock.post(
                url,
                status=429,
                body=_UPSTREAM_BODY,
                headers={"Retry-After": "120"},
                repeat=True,
            )
        resp = await client.post(
            "/v1/chat/completions", headers=client_key, json=_chat()
        )
        # The fallback provider was tried before giving up.
        assert _calls(mock, _A) >= 1 and _calls(mock, _B) >= 1

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "120"
    body = resp.json()
    assert body["retry_after"] == 120
    assert "quota" in body["detail"].lower()
    assert "other-provider" in body["detail"]
    assert "120 seconds" in body["detail"]
    # Composed by the gateway: nothing of the upstream body.
    assert "upstream-secret" not in resp.text and "org-42" not in resp.text


@pytest.mark.asyncio
async def test_upstream_429_without_retry_after_has_no_delay(
    client, client_key, a_first, no_retry_delay
):
    with aioresponses() as mock:
        for url in (_A, _B):
            mock.post(url, status=429, body=_UPSTREAM_BODY, repeat=True)
        resp = await client.post(
            "/v1/chat/completions", headers=client_key, json=_chat()
        )

    assert resp.status_code == 429, resp.text
    assert "Retry-After" not in resp.headers
    body = resp.json()
    assert "retry_after" not in body
    assert "quota" in body["detail"].lower()
    assert "upstream-secret" not in resp.text


@pytest.mark.asyncio
async def test_upstream_429_http_date_retry_after_becomes_seconds(
    client, client_key, a_first, no_retry_delay
):
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=600), usegmt=True
    )
    with aioresponses() as mock:
        for url in (_A, _B):
            mock.post(
                url,
                status=429,
                body=_UPSTREAM_BODY,
                headers={"Retry-After": when},
                repeat=True,
            )
        resp = await client.post(
            "/v1/chat/completions", headers=client_key, json=_chat()
        )

    assert resp.status_code == 429, resp.text
    assert 590 <= resp.json()["retry_after"] <= 600
    assert resp.headers["Retry-After"] == str(resp.json()["retry_after"])


_SEVEN_DAYS = 7 * 24 * 3600


@pytest.mark.parametrize(
    "header",
    ["99999999", "Wed, 01 Jan 2031 00:00:00 GMT"],
    ids=["seconds", "http-date"],
)
def test_retry_after_is_capped_at_seven_days(header):
    from plugboard.router import _retry_after_seconds

    assert _retry_after_seconds(header) == _SEVEN_DAYS


@pytest.mark.asyncio
async def test_upstream_429_keeps_the_upstream_detail_for_the_admin(
    client, a_first, no_retry_delay
):
    with aioresponses() as mock:
        for url in (_A, _B):
            mock.post(
                url,
                status=429,
                body=_UPSTREAM_BODY,
                headers={"Retry-After": "30"},
                repeat=True,
            )
        resp = await client.post("/v1/chat/completions", headers=ADMIN, json=_chat())

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "30"
    assert "upstream-secret" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_streamed_request_upstream_429_gives_429(client, client_key):
    with aioresponses() as mock:
        mock.post(
            _A,
            status=429,
            body=_UPSTREAM_BODY,
            headers={"Retry-After": "45"},
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=client_key,
            json={**_chat(stream=True), "model": "test-provider:fake:model-v1"},
        )

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "45"
    assert "test-provider" in resp.json()["detail"]
    assert "upstream-secret" not in resp.text


@pytest.mark.asyncio
async def test_fallback_provider_answers_after_a_429(
    client, client_key, a_first, no_retry_delay
):
    with aioresponses() as mock:
        mock.post(_A, status=429, body=_UPSTREAM_BODY, repeat=True)
        mock.post(_B, payload=FAKE_COMPLETION, status=200)
        resp = await client.post(
            "/v1/chat/completions", headers=client_key, json=_chat()
        )
        assert _calls(mock, _A) >= 1 and _calls(mock, _B) == 1

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"


# ── Subscription CLI fails on its quota ───────────────────────────────────

# Sourced formats only: the repository's own tests, gemini-cli issues
# #1906/#5377/#15033 and its JsonFormatter.formatError (stdout
# {"error": {"type", "message", "code"}}), codex exec events, Claude Code's
# help-center notices, the Anthropic and OpenAI error types.
_GEMINI_JSON_ERROR = (
    b'{"session_id":"s-1","error":{"type":"Error","message":'
    b'"[API Error: Resource has been exhausted (e.g. check quota).]","code":1}}'
)
_CLI_QUOTA_OUTPUTS = [
    pytest.param(
        "claude",
        b'{"type":"error","message":"rate limit exceeded"}\n',
        id="claude-rate-limit",
    ),
    pytest.param(
        "agy",
        b'{"status":"ERROR","response":"","error":"quota exceeded"}',
        id="agy-quota",
    ),
    pytest.param(
        "gemini",
        b'{"error": {"code": 429, "message": "Resource exhausted"}}',
        id="gemini-resource-exhausted",
    ),
    pytest.param("gemini", _GEMINI_JSON_ERROR, id="gemini-json-error-object"),
]
# Neutral: the quota must be read from the CLI's error, not from stderr.
_STDERR = b"warning: terminal not detected /home/op/.config token=abc123\n"


@pytest.fixture
def cli_fails(monkeypatch):
    """The real CLI wrapper runs on a fake process (exit 1 by default)."""
    from unittest.mock import AsyncMock, MagicMock

    from plugboard import cli_subprocess as cs

    def _setup(
        kind: str, stdout: bytes, stderr: bytes = _STDERR, returncode: int = 1
    ) -> None:
        proc = MagicMock()
        proc.returncode = returncode
        proc.pid = 4242
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        monkeypatch.setattr(cs, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
        monkeypatch.setattr(
            cs.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

    return _setup


@pytest.fixture
def cli_route(monkeypatch, cli_fails):
    """forward() calls the CLI of `kind` directly (no provider config)."""
    from plugboard import app as app_mod
    from plugboard import cli_subprocess as cs

    state: dict = {}

    async def _forward(body, **_kwargs):
        return await cs.complete(state["kind"], {**body, "model": "some-model"})

    monkeypatch.setattr(app_mod, "forward", _forward)

    def _setup(kind: str, stdout: bytes, **kwargs) -> None:
        cli_fails(kind, stdout, **kwargs)
        state["kind"] = kind

    return _setup


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stdout"), _CLI_QUOTA_OUTPUTS)
async def test_cli_quota_error_gives_429_without_process_details(
    client, client_key, cli_route, kind, stdout
):
    cli_route(kind, stdout)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert "quota" in body["detail"].lower()
    assert kind in body["detail"]
    for leak in ("/home/op", "token=abc123", "4242", "stderr", "exit_code"):
        assert leak not in resp.text


@pytest.mark.asyncio
async def test_cli_quota_in_stderr_only_gives_429(client, client_key, cli_route):
    cli_route("gemini", b"", stderr=b"429 Resource exhausted\n")
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text


@pytest.mark.asyncio
async def test_cli_quota_error_keeps_details_for_the_admin(client, cli_route):
    cli_route("claude", b'{"type":"error","message":"rate limit exceeded"}\n')
    resp = await client.post("/v1/chat/completions", headers=ADMIN, json=_chat())

    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["pid"] == 4242
    assert "rate limit exceeded" in body["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "status"),
    # A prompt too long is the request's fault: 400 (test_cli_prompt_too_long).
    [("model overloaded", 502), ("Input exceeds the context length limit", 400)],
)
async def test_cli_error_other_than_quota_is_not_429(
    client, client_key, cli_route, message, status
):
    event = '{"type":"error","message":"%s"}\n' % message
    cli_route("claude", event.encode())
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == status, resp.text


# The model's own words are not a quota error, nor is a full disk.
_NOT_QUOTA = [
    pytest.param(
        "codex",
        b'{"type":"item.completed","item":{"type":"agent_message",'
        b'"text":"When the rate limit exceeded the plan, retry"}}\n',
        b"",
        1,
        id="codex-model-text-in-fallback-detail",
    ),
    pytest.param(
        "gemini",
        b"Sure: the rate limit exceeded message means...",
        b"",
        0,
        id="gemini-invalid-json-quotes-the-model",
    ),
    pytest.param(
        "codex",
        b"",
        b"OSError: [Errno 122] Disk quota exceeded: '/tmp/x'\n",
        1,
        id="disk-quota",
    ),
    # The CLI's error says something else: a retry log in stderr is ignored.
    pytest.param(
        "gemini",
        b'{"error":{"type":"ApiError","message":"models/x is not found","code":1}}',
        b"Retrying after error: Resource has been exhausted (e.g. check quota).\n",
        1,
        id="gemini-not-found-with-a-quota-retry-log",
    ),
    # gemini's error object must be the whole stdout, not a line the model
    # wrote among others.
    pytest.param(
        "gemini",
        b'The API may answer:\n{"error":{"message":"quota exceeded"}}\nThen retry.',
        b"",
        1,
        id="gemini-model-text-holds-an-error-line",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stdout", "stderr", "code"), _NOT_QUOTA)
async def test_model_output_or_disk_quota_stays_502(
    client, client_key, cli_route, kind, stdout, stderr, code
):
    cli_route(kind, stdout, stderr=stderr, returncode=code)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 502, resp.text


# codex `exec --json` out of ChatGPT quota: the reason is in stdout's events
# (codex exits 1 on such an error); stderr may only hold an unrelated line.
_CODEX_USAGE_LIMIT = (
    b'{"type":"thread.started","thread_id":"0199-test"}\n'
    b'{"type":"turn.started"}\n'
    b'{"type":"error","message":"You\'ve hit your usage limit. Upgrade to Pro'
    b' or try again at 6:00 PM."}\n'
    b'{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit.'
    b' Upgrade to Pro or try again at 6:00 PM."}}\n'
)
_CODEX_STDERR = (
    b"ERROR codex_core::codex: failed to record rollout items:"
    b" thread 0199-test not found /home/op/.codex\n"
)


@pytest.mark.asyncio
async def test_codex_reconnect_error_then_answer_is_served(
    client, client_key, cli_route
):
    """A transient `error` event (openai/codex#32984) is not the outcome."""
    stdout = (
        b'{"type":"thread.started","thread_id":"0199-test"}\n'
        b'{"type":"turn.started"}\n'
        b'{"type":"error","message":"Reconnecting... 1/5 (rate limit exceeded)"}\n'
        b'{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
        b'"text":"Paris."}}\n'
        b'{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n'
    )
    cli_route("codex", stdout, returncode=0)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "Paris."


@pytest.mark.asyncio
async def test_codex_turn_failed_on_quota_with_exit_0_gives_429(
    client, client_key, cli_route
):
    stdout = (
        b'{"type":"turn.started"}\n'
        b'{"type":"turn.failed","error":{"message":"You\'ve hit your usage'
        b' limit."}}\n'
    )
    cli_route("codex", stdout, returncode=0)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text


_GEMINI_QUOTA_AFTER_A_LOG = (
    b"Loaded cached credentials.\n"
    b'{\n  "error": {\n    "type": "ApiError",\n    "message": "Resource has'
    b' been exhausted (e.g. check quota).",\n    "code": 429\n  }\n}\n'
)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 1])
async def test_gemini_quota_object_after_a_log_line_gives_429(
    client, client_key, cli_route, code
):
    """A CLI log line before the error object does not hide the quota."""
    cli_route("gemini", _GEMINI_QUOTA_AFTER_A_LOG, stderr=b"", returncode=code)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text


_CODEX_RECONNECT = (
    b'{"type":"turn.started"}\n{"type":"error","message":"Reconnecting... 1/5 (%s)"}\n'
)


@pytest.mark.asyncio
async def test_codex_exit_1_reconnect_then_usage_limit_gives_429(
    client, client_key, cli_route
):
    """The event that ends the run decides, not the first `error`."""
    stdout = _CODEX_RECONNECT % b"stream disconnected" + (
        b'{"type":"error","message":"You\'ve hit your usage limit."}\n'
        b'{"type":"turn.failed","error":{"message":"You\'ve hit your usage'
        b' limit."}}\n'
    )
    cli_route("codex", stdout)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text


@pytest.mark.asyncio
async def test_codex_exit_1_last_error_decides_without_turn_failed(
    client, client_key, cli_route
):
    stdout = _CODEX_RECONNECT % b"stream disconnected" + (
        b'{"type":"error","message":"exceeded retry limit, last status: 429"}\n'
    )
    cli_route("codex", stdout)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text


@pytest.mark.asyncio
async def test_codex_exit_1_reconnect_on_rate_limit_then_other_failure_stays_502(
    client, client_key, cli_route
):
    stdout = _CODEX_RECONNECT % b"rate limit exceeded" + (
        b'{"type":"turn.failed","error":{"message":"stream disconnected before'
        b' completion"}}\n'
    )
    cli_route("codex", stdout)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 502, resp.text


@pytest.mark.asyncio
async def test_codex_exit_0_quota_error_without_an_answer_gives_429(
    client, client_key, cli_route
):
    """No answer to serve: the `error` event is the outcome."""
    stdout = (
        b'{"type":"turn.started"}\n'
        b'{"type":"error","message":"You\'ve hit your usage limit."}\n'
    )
    cli_route("codex", stdout, returncode=0)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text


@pytest.mark.asyncio
async def test_claude_exit_1_retry_error_then_other_result_stays_502(
    client, client_key, cli_route
):
    stdout = (
        b'{"type":"error","message":"rate limit exceeded, retrying"}\n'
        b'{"type":"result","is_error":true,"result":"Invalid model name"}\n'
    )
    cli_route("claude", stdout)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 502, resp.text


@pytest.mark.asyncio
async def test_claude_exit_0_retry_error_then_answer_is_served(
    client, client_key, cli_route
):
    stdout = (
        b'{"type":"error","message":"rate limit exceeded, retrying"}\n'
        b'{"type":"result","subtype":"success","is_error":false,'
        b'"result":"Paris."}\n'
    )
    cli_route("claude", stdout, returncode=0)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "Paris."


@pytest.fixture
def five_pm(monkeypatch):
    """The gateway's local clock reads 17:00 (codex prints its local time)."""
    from datetime import datetime

    from plugboard import cli_subprocess as cs

    monkeypatch.setattr(cs, "_local_now", lambda: datetime(2026, 11, 10, 17, 0, 0))


@pytest.mark.asyncio
async def test_codex_usage_limit_turns_the_reopening_time_into_retry_after(
    client, client_key, cli_route, five_pm
):
    cli_route("codex", _CODEX_USAGE_LIMIT, stderr=_CODEX_STDERR)
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "3600"  # 17:00 -> 6:00 PM
    body = resp.json()
    assert body["retry_after"] == 3600
    assert "codex" in body["detail"] and "3600 seconds" in body["detail"]
    # Composed by the gateway: none of the CLI's text, stderr or pid.
    for leak in ("hit your", "Upgrade", "6:00", "rollout", "/home/op", "4242"):
        assert leak not in resp.text


def test_a_past_reopening_time_means_tomorrow(five_pm):
    from plugboard.cli_subprocess import quota_retry_after

    assert quota_retry_after("try again at 4:30 PM.") == 23 * 3600 + 30 * 60
    assert quota_retry_after("try again at 17:30") == 30 * 60
    assert quota_retry_after("try again at 25:00") is None


@pytest.mark.asyncio
async def test_codex_quota_on_a_streamed_request_gives_429(
    client, client_key, cli_route, monkeypatch
):
    from plugboard import app as app_mod
    from plugboard.router import resolve_provider_and_model

    def _as_cli(model_str):
        provider, model = resolve_provider_and_model(model_str)
        return {**provider, "auth_source": "subprocess:codex"}, model

    monkeypatch.setattr(app_mod, "resolve_provider_and_model", _as_cli)
    cli_route("codex", _CODEX_USAGE_LIMIT, stderr=_CODEX_STDERR)
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream=True)
    )

    assert resp.status_code == 429, resp.text
    assert "codex" in resp.json()["detail"]
    assert "rollout" not in resp.text


@pytest.mark.asyncio
async def test_claude_hit_your_limit_gives_429(client, client_key, cli_route):
    cli_route(
        "claude",
        b'{"type":"result","is_error":true,"result":"You\'ve hit your limit"}\n',
    )
    resp = await client.post("/v1/chat/completions", headers=client_key, json=_chat())

    assert resp.status_code == 429, resp.text
    assert "claude" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_claude_exit_0_usage_limit_is_429_and_not_billed(
    client, client_key_record, cli_route
):
    from plugboard import billing, usage

    headers, key_id = client_key_record
    cli_route(
        "claude",
        b'{"type":"result","subtype":"success","is_error":true,'
        b'"result":"Claude AI usage limit reached"}\n',
        returncode=0,
    )
    resp = await client.post("/v1/chat/completions", headers=headers, json=_chat())

    assert resp.status_code == 429, resp.text
    assert usage.get_usage_by_key(billing.current_pricing_month(), key_id) == []


@pytest.mark.asyncio
async def test_cli_in_quota_falls_back_to_an_http_provider(
    client, client_key, cli_fails, _providers_file, monkeypatch
):
    """The real router loop: the CLI provider first, then an HTTP one."""
    import json

    import plugboard.router as router
    from tests.factories import PROVIDER_A

    codex = {
        "id": "pbtest-codex-cli",
        "name": "Codex CLI test",
        "base_url": "subprocess://codex",
        "bearer_token": "(via CLI)",
        "default_model": "pbtest-codex-model",
        "auth_source": "subprocess:codex",
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    _providers_file.write_text(json.dumps({"providers": [codex, PROVIDER_A]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "pbtest-codex-cli": {"latency_avg": 0.1, "requests": 100, "errors": 0},
            "test-provider": {"latency_avg": 2.0, "requests": 100, "errors": 0},
        },
    )
    cli_fails("codex", _CODEX_USAGE_LIMIT, stderr=_CODEX_STDERR)

    with aioresponses() as mock:
        mock.post(_A, payload=FAKE_COMPLETION, status=200)
        resp = await client.post(
            "/v1/chat/completions", headers=client_key, json=_chat()
        )
        assert _calls(mock, _A) == 1

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"


@pytest.mark.parametrize(
    "message",
    [
        "You've hit your usage limit. Upgrade to Pro or try again later.",
        "You’ve hit your limit",
        "Claude AI usage limit reached",
        '{"error": {"code": "insufficient_quota"}}',
        "Resource has been exhausted (e.g. check quota).",
        '"status": "RESOURCE_EXHAUSTED"',
        '"reason": "rateLimitExceeded"',
        "5-hour limit reached - resets 3pm",
        "You've reached your weekly limit. It resets Monday at 9:00 AM.",
        '{"type":"error","error":{"type":"rate_limit_error","message":"x"}}',
        "stream error: exceeded retry limit, last status: 429 Too Many Requests",
    ],
)
def test_reported_quota_formats_are_recognised(message):
    from plugboard.cli_subprocess import is_quota_error

    assert is_quota_error(f"CLI 'x' exited 1: {message}")


def test_disk_quota_is_not_a_provider_quota():
    from plugboard.cli_subprocess import is_quota_error

    assert not is_quota_error("OSError: [Errno 122] Disk quota exceeded")
