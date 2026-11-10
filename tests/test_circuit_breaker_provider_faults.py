"""The circuit breaker counts only the provider's own failures (PLU-3).

A route's circuit opens after 3 failures in a row. It counts what says the
provider is failing: HTTP 429 and 5xx, timeouts, network errors, a 401 or a
403, and on a CLI provider a quota, an unavailable model or a failed process.
An error the request caused does not count: HTTP 400, 413 and 422, and any
HTTP or CLI error whose text says the context or the message is too long.
Before, one client key sending three over-long prompts opened the route for
everyone (the LLM pricing pass skipped it, /admin/circuits showed it open).

Every test goes through the real router: only the HTTP upstream and the CLI
process are fakes.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aioresponses import aioresponses

_A = "http://fake-llm.test/v1/chat/completions"
_HTTP_MODEL = "test-provider:fake:model-v1"
_HTTP_KEY = "test-provider:fake:model-v1"
_CLI_ID = "pbtest-cli"
_CLI_MODEL = f"{_CLI_ID}:pbtest-model"
_CLI_KEY = f"{_CLI_ID}:pbtest-model"
_MESSAGES = [{"role": "user", "content": "hi"}]
_TOO_LONG = "prompt is too long: 212000 tokens > 200000 maximum"


@pytest.fixture
def breaker(monkeypatch):
    """A fresh breaker; no alert webhook, no retry delay."""
    import plugboard.alerts as alerts
    import plugboard.router as router
    from plugboard import circuit_breaker as cb

    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(router, "_IN_FLIGHT_BY_PROVIDER", {})

    async def _no_alert(_provider_id):
        return None

    monkeypatch.setattr(router, "record_provider_failure", _no_alert)
    return cb


def _failures(key: str) -> int:
    from plugboard import circuit_breaker as cb

    circuit = cb._circuits.get(key)
    return circuit.failures if circuit else 0


# ── HTTP provider, non-stream (forward) ─────────────────────────────────────


_HTTP_FAULTS = [
    pytest.param({"status": 429, "payload": {"error": "rate limited"}}, id="429"),
    pytest.param({"status": 500, "payload": {"error": "internal"}}, id="500"),
    pytest.param({"status": 502, "payload": {"error": "bad gateway"}}, id="502"),
    pytest.param({"status": 503, "payload": {"error": "unavailable"}}, id="503"),
    pytest.param({"status": 529, "payload": {"error": "overloaded"}}, id="529"),
    pytest.param({"status": 401, "payload": {"error": "invalid api key"}}, id="401"),
    pytest.param({"status": 403, "payload": {"error": "forbidden"}}, id="403"),
    pytest.param({"exception": asyncio.TimeoutError()}, id="timeout"),
    pytest.param({"exception": aiohttp.ClientConnectionError("refused")}, id="network"),
]

_HTTP_REQUEST_ERRORS = [
    pytest.param(400, {"error": {"message": "invalid request"}}, id="400"),
    pytest.param(413, {"error": {"message": "payload too large"}}, id="413"),
    pytest.param(422, {"error": {"message": "unprocessable"}}, id="422"),
    # Only the statuses listed above count: any other one does not either.
    pytest.param(404, {"error": {"message": "not found"}}, id="404"),
    pytest.param(400, {"error": {"message": _TOO_LONG}}, id="400-prompt-too-long"),
    pytest.param(
        500,
        {
            "error": {
                "message": "This model's maximum context length is 8192 tokens",
                "code": "context_length_exceeded",
            }
        },
        id="500-context-length",
    ),
    pytest.param(
        503,
        {"error": "the request exceeds the available context size"},
        id="503-context-size",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", _HTTP_FAULTS)
async def test_http_provider_fault_counts(_providers_file, breaker, answer):
    from plugboard.router import forward

    with aioresponses() as mock:
        mock.post(_A, repeat=True, **answer)
        with pytest.raises(RuntimeError):
            await forward({"model": _HTTP_MODEL, "messages": _MESSAGES})

    assert _failures(_HTTP_KEY) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "payload"), _HTTP_REQUEST_ERRORS)
async def test_http_request_error_does_not_count(
    _providers_file, breaker, status, payload
):
    from plugboard.router import forward

    with aioresponses() as mock:
        mock.post(_A, status=status, payload=payload, repeat=True)
        with pytest.raises(RuntimeError, match=f"HTTP {status}"):
            await forward({"model": _HTTP_MODEL, "messages": _MESSAGES})

    assert _failures(_HTTP_KEY) == 0


@pytest.mark.asyncio
async def test_three_request_errors_leave_the_route_closed(_providers_file, breaker):
    from plugboard.router import forward

    with aioresponses() as mock:
        mock.post(
            _A, status=400, payload={"error": {"message": _TOO_LONG}}, repeat=True
        )
        for _ in range(3):
            with pytest.raises(RuntimeError, match="HTTP 400"):
                await forward({"model": _HTTP_MODEL, "messages": _MESSAGES})

    assert breaker.check_circuit(_HTTP_KEY)["state"] == breaker.CLOSED


# ── CLI provider (forward; a streamed CLI request also goes there) ──────────


@pytest.fixture
def cli(_providers_file, monkeypatch):
    """A lone CLI provider whose process is a fake (stdout, stderr, exit)."""
    import plugboard.model_quarantine as quarantine
    import plugboard.router as router
    from plugboard import cli_subprocess as cs

    # The model stays visible to the next tests: no quarantine file write.
    monkeypatch.setattr(quarantine, "record_terminal_unavailable", MagicMock())
    monkeypatch.setattr(quarantine, "record_account_unsupported", MagicMock())
    monkeypatch.setattr(cs, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    # The fake process has no real pid: never signal it.
    monkeypatch.setattr(cs, "_kill_group", AsyncMock())

    def _setup(kind: str, stdout: bytes = b"", stderr: bytes = b"", code: int = 1):
        _providers_file.write_text(
            json.dumps(
                {
                    "providers": [
                        {
                            "id": _CLI_ID,
                            "name": "CLI test",
                            "base_url": f"subprocess://{kind}",
                            "bearer_token": "(via CLI)",
                            "default_model": "pbtest-model",
                            "auth_source": f"subprocess:{kind}",
                            "is_active": True,
                            "created_at": "2026-01-01T00:00:00+00:00",
                        }
                    ]
                }
            )
        )
        router.invalidate_models_cache()
        proc = MagicMock()
        proc.returncode = code
        proc.pid = 4242
        if isinstance(stdout, BaseException):
            proc.communicate = AsyncMock(side_effect=stdout)
        else:
            proc.communicate = AsyncMock(return_value=(stdout, stderr))
        monkeypatch.setattr(
            cs.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

    return _setup


def _claude_error(message: str) -> bytes:
    event = {"type": "result", "subtype": "success", "is_error": True}
    return (json.dumps({**event, "result": message}) + "\n").encode()


_CODEX_CONTEXT_WINDOW = (
    b'{"type":"thread.started","thread_id":"0199-test"}\n'
    b'{"type":"turn.started"}\n'
    b'{"type":"error","message":"Your input exceeds the context window of this '
    b'model. Please adjust your input and try again."}\n'
    b'{"type":"turn.failed","error":{"message":"Your input exceeds the context '
    b'window of this model. Please adjust your input and try again."}}\n'
)

_CLI_FAULTS = [
    pytest.param(
        "claude", {"stderr": b"Error: rate limit exceeded, try again later"}, id="quota"
    ),
    pytest.param(
        "claude",
        {"stderr": b"Error: model 'pbtest-model' does not exist"},
        id="model-unavailable",
    ),
    pytest.param("claude", {"stderr": b"Segmentation fault", "code": 139}, id="crash"),
    pytest.param("claude", {"stdout": asyncio.TimeoutError()}, id="timeout"),
]

_CLI_REQUEST_ERRORS = [
    pytest.param("claude", _claude_error("Prompt is too long"), id="claude"),
    pytest.param("codex", _CODEX_CONTEXT_WINDOW, id="codex"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "process"), _CLI_FAULTS)
async def test_cli_provider_fault_counts(cli, breaker, kind, process):
    from plugboard.router import forward

    cli(kind, **process)
    with pytest.raises(RuntimeError):
        await forward({"model": _CLI_MODEL, "messages": _MESSAGES})

    assert _failures(_CLI_KEY) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stdout"), _CLI_REQUEST_ERRORS)
async def test_cli_request_too_long_does_not_count(cli, breaker, kind, stdout):
    from plugboard.router import forward

    cli(kind, stdout=stdout)
    with pytest.raises(RuntimeError, match="(?i)too long|context window"):
        await forward({"model": _CLI_MODEL, "messages": _MESSAGES})

    assert _failures(_CLI_KEY) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process", "counted"),
    [
        pytest.param({"stderr": b"Error: quota exceeded"}, 1, id="quota-counts"),
        pytest.param(
            {"stdout": _claude_error("Prompt is too long")}, 0, id="too-long-does-not"
        ),
    ],
)
async def test_streamed_cli_request_is_judged_the_same(
    client, cli, breaker, monkeypatch, process, counted
):
    """The API answers a streamed CLI request through forward()."""
    from plugboard import app as app_mod

    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    cli("claude", **process)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={"model": _CLI_MODEL, "messages": _MESSAGES, "stream": True},
    )

    assert resp.status_code in (429, 502), resp.text
    assert _failures(_CLI_KEY) == counted


# ── HTTP provider, stream (forward_stream) ──────────────────────────────────


async def _stream(client):
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={"model": _HTTP_MODEL, "messages": _MESSAGES, "stream": True},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", _HTTP_FAULTS)
async def test_streamed_http_provider_fault_counts(client, breaker, answer):
    with aioresponses() as mock:
        mock.post(_A, **answer)
        resp = await _stream(client)

    assert resp.status_code != 200
    assert _failures(_HTTP_KEY) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "payload"), _HTTP_REQUEST_ERRORS)
async def test_streamed_http_request_error_does_not_count(
    client, breaker, status, payload
):
    with aioresponses() as mock:
        mock.post(_A, status=status, payload=payload)
        resp = await _stream(client)

    assert resp.status_code != 200
    assert _failures(_HTTP_KEY) == 0


@pytest.mark.asyncio
async def test_streamed_success_closes_the_circuit_after_cooldown(
    client, breaker, monkeypatch
):
    """Read, then recorded: a stream-only route does not stay open for good."""
    from tests.factories import FAKE_STREAM_BODY

    monkeypatch.setattr(breaker, "_COOLDOWN_SECONDS", -1.0)
    with aioresponses() as mock:
        for _ in range(3):
            mock.post(_A, status=503, payload={"error": "unavailable"})
        mock.post(
            _A, status=200, body=FAKE_STREAM_BODY, content_type="text/event-stream"
        )
        for _ in range(3):
            assert (await _stream(client)).status_code != 200
        assert breaker._circuits[_HTTP_KEY].state == breaker.OPEN
        resp = await _stream(client)

    assert resp.status_code == 200
    assert breaker._circuits[_HTTP_KEY].state == breaker.CLOSED


# ── CLI provider, forward_stream (not reached from the API) ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process", "counted"),
    [
        pytest.param({"stderr": b"Error: quota exceeded"}, 1, id="quota-counts"),
        pytest.param({"stderr": b"Segmentation fault", "code": 139}, 1, id="crash"),
        pytest.param(
            {"stdout": _claude_error("Prompt is too long")}, 0, id="too-long-does-not"
        ),
    ],
)
async def test_forward_stream_cli_failure_is_judged_the_same(
    cli, breaker, process, counted
):
    from plugboard.router import forward_stream

    cli("claude", **process)
    body = {"model": _CLI_MODEL, "messages": _MESSAGES, "stream": True}
    chunks = [chunk async for chunk in forward_stream(body)]

    assert b"subprocess_error" in chunks[0]
    assert _failures(_CLI_KEY) == counted
