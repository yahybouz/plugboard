"""The provider_down alert counts only the provider's own failures (PLU-6).

The webhook alert ``provider_down`` goes out after ALERT_FAILURE_THRESHOLD
(3) failures in a row of one provider. It follows the circuit breaker's rule
(circuit_breaker.is_provider_fault): HTTP 429 and 5xx, timeouts, network
errors, a 401 or a 403, and on a CLI provider a quota, an unavailable model or
a failed process count. An error the request caused does not: HTTP 400, 413,
422 or any other status, and any HTTP or CLI error saying the context or the
message is too long. Before, one client key sending three over-long prompts
sent "provider down" for a provider that was answering.

Every test goes through the real router and the real alert code: only the
HTTP upstream, the webhook and the CLI process are fakes.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

_A = "http://fake-llm.test/v1/chat/completions"
_WEBHOOK = "https://hooks.example.test/plugboard-alerts"
_HTTP_ID = "test-provider"
_HTTP_MODEL = "test-provider:fake:model-v1"
_CLI_ID = "pbtest-cli"
_CLI_MODEL = f"{_CLI_ID}:pbtest-model"
_MESSAGES = [{"role": "user", "content": "hi"}]
_TOO_LONG = "prompt is too long: 212000 tokens > 200000 maximum"


@pytest.fixture
def alerting(monkeypatch):
    """A fresh alert count with a webhook; fresh breaker; no retry delay."""
    import plugboard.alerts as alerts
    import plugboard.metrics as metrics
    import plugboard.router as router
    from plugboard import circuit_breaker as cb

    monkeypatch.setattr(alerts, "_WEBHOOK_URL", _WEBHOOK)
    monkeypatch.setattr(alerts, "_ALERT_THRESHOLD", 3)
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(metrics, "_stats", {})
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    return alerts


def _sent(mock) -> list:
    """The provider_down alerts the webhook received."""
    calls = mock.requests.get(("POST", URL(_WEBHOOK)), [])
    return [call.kwargs["json"] for call in calls]


# ── HTTP provider (forward) ─────────────────────────────────────────────────


_HTTP_FAULTS = [
    pytest.param({"status": 429, "payload": {"error": "rate limited"}}, id="429"),
    pytest.param({"status": 500, "payload": {"error": "internal"}}, id="500"),
    pytest.param({"status": 502, "payload": {"error": "bad gateway"}}, id="502"),
    pytest.param({"status": 503, "payload": {"error": "unavailable"}}, id="503"),
    pytest.param({"status": 401, "payload": {"error": "invalid api key"}}, id="401"),
    pytest.param({"status": 403, "payload": {"error": "forbidden"}}, id="403"),
    pytest.param({"exception": asyncio.TimeoutError()}, id="timeout"),
    pytest.param({"exception": aiohttp.ClientConnectionError("refused")}, id="network"),
]

_HTTP_REQUEST_ERRORS = [
    pytest.param(400, {"error": {"message": "invalid request"}}, id="400"),
    pytest.param(413, {"error": {"message": "payload too large"}}, id="413"),
    pytest.param(422, {"error": {"message": "unprocessable"}}, id="422"),
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


async def _forward_http() -> None:
    from plugboard.router import forward

    with pytest.raises(RuntimeError):
        await forward({"model": _HTTP_MODEL, "messages": _MESSAGES})


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", _HTTP_FAULTS)
async def test_three_http_provider_faults_send_provider_down(
    _providers_file, alerting, answer
):
    with aioresponses() as mock:
        mock.post(_WEBHOOK, status=200, repeat=True)
        mock.post(_A, repeat=True, **answer)
        for _ in range(2):
            await _forward_http()
        assert _sent(mock) == []
        await _forward_http()
        await alerting.drain()  # the webhook goes out in the background (PLU-27)

    assert len(_sent(mock)) == 1
    alert = _sent(mock)[0]
    assert alert["type"] == "provider_down"
    assert alert["provider_id"] == _HTTP_ID
    assert alert["consecutive_failures"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "payload"), _HTTP_REQUEST_ERRORS)
async def test_http_request_errors_send_no_alert(
    _providers_file, alerting, status, payload
):
    with aioresponses() as mock:
        mock.post(_WEBHOOK, status=200, repeat=True)
        mock.post(_A, status=status, payload=payload, repeat=True)
        for _ in range(3):
            await _forward_http()

    assert _sent(mock) == []
    assert alerting._provider_failures.get(_HTTP_ID, 0) == 0


@pytest.mark.asyncio
async def test_a_request_error_between_provider_faults_neither_counts_nor_resets(
    _providers_file, alerting
):
    """As for the circuit: only a success resets the count."""
    with aioresponses() as mock:
        mock.post(_WEBHOOK, status=200, repeat=True)
        for _ in range(2):
            mock.post(_A, status=503, payload={"error": "unavailable"}, repeat=4)
            await _forward_http()
        mock.post(_A, status=400, payload={"error": {"message": _TOO_LONG}})
        await _forward_http()
        assert _sent(mock) == []
        mock.post(_A, status=503, payload={"error": "unavailable"}, repeat=4)
        await _forward_http()
        await alerting.drain()

    assert [alert["consecutive_failures"] for alert in _sent(mock)] == [3]


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
        # A streamed chat to claude reads the pipes itself (4.14.0); a plain
        # one calls communicate(). Both get the same bytes, or the same fault.
        if isinstance(stdout, BaseException):
            proc.communicate = AsyncMock(side_effect=stdout)
            proc.stdout.read = AsyncMock(side_effect=stdout)
        else:
            proc.communicate = AsyncMock(return_value=(stdout, stderr))
            proc.stdout.read = AsyncMock(side_effect=[stdout, b""])
        proc.stderr.read = AsyncMock(return_value=stderr)
        proc.stdin.drain = AsyncMock()
        proc.wait = AsyncMock(return_value=code)
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


async def _forward_cli() -> None:
    from plugboard.router import forward

    with pytest.raises(RuntimeError):
        await forward({"model": _CLI_MODEL, "messages": _MESSAGES})


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "process"), _CLI_FAULTS)
async def test_three_cli_provider_faults_send_provider_down(
    cli, alerting, kind, process
):
    cli(kind, **process)
    with aioresponses() as mock:
        mock.post(_WEBHOOK, status=200, repeat=True)
        for _ in range(2):
            await _forward_cli()
        assert _sent(mock) == []
        await _forward_cli()
        await alerting.drain()

    assert [(a["type"], a["provider_id"]) for a in _sent(mock)] == [
        ("provider_down", _CLI_ID)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "stdout"), _CLI_REQUEST_ERRORS)
async def test_cli_request_too_long_sends_no_alert(cli, alerting, kind, stdout):
    cli(kind, stdout=stdout)
    with aioresponses() as mock:
        mock.post(_WEBHOOK, status=200, repeat=True)
        for _ in range(3):
            await _forward_cli()

    assert _sent(mock) == []
    assert alerting._provider_failures.get(_CLI_ID, 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process", "alerts_sent"),
    [
        pytest.param({"stderr": b"Error: quota exceeded"}, 1, id="quota-alerts"),
        pytest.param(
            {"stdout": _claude_error("Prompt is too long")}, 0, id="too-long-does-not"
        ),
    ],
)
async def test_streamed_cli_request_is_judged_the_same(
    client, cli, alerting, monkeypatch, process, alerts_sent
):
    """The API answers a streamed CLI request through forward()."""
    from plugboard import app as app_mod

    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    cli("claude", **process)
    with aioresponses() as mock:
        mock.post(_WEBHOOK, status=200, repeat=True)
        for _ in range(3):
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer testkey"},
                json={"model": _CLI_MODEL, "messages": _MESSAGES, "stream": True},
            )
            assert resp.status_code != 200, resp.text

    assert len(_sent(mock)) == alerts_sent
