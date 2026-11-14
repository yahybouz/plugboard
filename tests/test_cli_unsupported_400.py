"""The chat route answers 400 ``unsupported_on_cli`` for what a CLI cannot serve.

A CLI subscription route (``auth_source`` ``subprocess:<cli>``) hands the
conversation to a CLI as plain text. Tools, a tool choice that forces a call,
a structured response format or an image used to be dropped, and the client
got free text with status 200. When every candidate route of the model is a
CLI, the chat route now refuses the request with 400 and a stable ``code``,
before the price and rate checks, the session turn and the cache: nothing is
called. A request a CLI can serve, and the same request sent to an HTTP
provider, still go through.

No CLI runs: ``cli_subprocess.complete`` is replaced by a counter. The HTTP
provider is a real server on 127.0.0.1 that records the body it received.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web

from tests.factories import FAKE_COMPLETION, PROVIDER_A

CLI_ID = "pbtest-codex-cli"
CLI_MODEL = "pbtest-codex-model"
CLI_PROVIDER = {
    "id": CLI_ID,
    "name": "Codex CLI test",
    "base_url": "subprocess://codex",
    "bearer_token": "(via CLI)",
    "default_model": CLI_MODEL,
    "auth_source": "subprocess:codex",
    "provider_type": "cloud",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
CLI_ROUTE = f"{CLI_ID}:{CLI_MODEL}"
HTTP_ROUTE = "test-provider:fake:model-v1"

ADMIN = {"Authorization": "Bearer testkey"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]
JSON_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "answer", "schema": {"type": "object"}},
}
IMAGE_MESSAGES = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:,"}},
        ],
    }
]
TOOL_CALLS = [
    {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": "{}"},
    }
]
HINT = (
    "Send plain text messages, or pick a model served by an API or Ollama "
    "provider from GET /v1/models."
)


def _chat(model: str, **extra) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "cache": False,
        **extra,
    }


def _refusal(unsupported: list[str]) -> dict:
    return {
        "detail": f"Model route '{CLI_ID}' is a CLI subscription: it cannot "
        f"serve {', '.join(unsupported)}. Nothing was called.",
        "code": "unsupported_on_cli",
        "unsupported": unsupported,
        "hint": HINT,
    }


@pytest.fixture
def isolated(monkeypatch, _providers_file):
    """Fresh breaker, alerts and metrics; a counter in place of the CLI."""
    import plugboard.alerts as alerts
    import plugboard.metrics as metrics
    import plugboard.router as router
    from plugboard import circuit_breaker as cb
    from plugboard import cli_subprocess

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    monkeypatch.setattr(metrics, "_stats", {})

    completed: list[tuple[str, dict]] = []

    async def fake_complete(kind, body, *, timeout_s=None, on_delta=None):
        completed.append((kind, body))
        return json.loads(json.dumps(FAKE_COMPLETION))

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)

    def write(*providers: dict) -> None:
        _providers_file.write_text(json.dumps({"providers": list(providers)}))
        router.invalidate_models_cache()

    write(CLI_PROVIDER)
    return completed, write


@pytest.fixture
def client_key(tmp_path, monkeypatch):
    """Authorization header of a plain client key (not the admin)."""
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    rec = ak.create_key("cli-unsupported-client", "test")
    return {"Authorization": f"Bearer {rec['key']}"}


@pytest.fixture(params=["client", "admin"])
def headers(request, client_key):
    return client_key if request.param == "client" else ADMIN


@pytest.fixture
async def http_provider():
    """PROVIDER_A on 127.0.0.1: records each body, answers a tool call."""
    received: list[dict] = []
    answer = json.loads(json.dumps(FAKE_COMPLETION))
    answer["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": TOOL_CALLS,
    }
    answer["choices"][0]["finish_reason"] = "tool_calls"

    async def handle(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response(answer)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield {**PROVIDER_A, "base_url": f"http://127.0.0.1:{site.port}"}, received
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_json_schema_on_a_cli_route_answers_400(client, headers, isolated):
    completed, _ = isolated

    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_chat(CLI_ROUTE, response_format=JSON_SCHEMA),
    )

    assert resp.status_code == 400, resp.text
    assert resp.json() == _refusal(["response_format"])
    assert completed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "unsupported"),
    [
        ({"tools": TOOLS}, ["tools"]),
        ({"tool_choice": "required"}, ["tool_choice"]),
        ({"messages": IMAGE_MESSAGES}, ["content:image_url"]),
    ],
    ids=["tools", "tool_choice", "image_url"],
)
async def test_each_unsupported_field_answers_400(
    client, headers, isolated, extra, unsupported
):
    completed, _ = isolated

    resp = await client.post(
        "/v1/chat/completions", headers=headers, json=_chat(CLI_ROUTE, **extra)
    )

    assert resp.status_code == 400, resp.text
    assert resp.json() == _refusal(unsupported)
    assert completed == []


@pytest.mark.asyncio
async def test_a_streamed_request_gets_the_400_as_json(client, headers, isolated):
    completed, _ = isolated

    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_chat(CLI_ROUTE, stream=True, tools=TOOLS),
    )

    assert resp.status_code == 400, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == _refusal(["tools"])
    assert completed == []


@pytest.mark.asyncio
async def test_fields_that_ask_for_nothing_still_reach_the_cli(
    client, headers, isolated
):
    completed, _ = isolated
    body = _chat(
        CLI_ROUTE, tools=[], tool_choice="auto", response_format={"type": "text"}
    )

    resp = await client.post("/v1/chat/completions", headers=headers, json=body)

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"
    assert len(completed) == 1
    assert completed[0][0] == "codex"


@pytest.mark.asyncio
async def test_tools_still_reach_an_http_provider(
    client, headers, isolated, http_provider
):
    completed, write = isolated
    http, received = http_provider
    write(CLI_PROVIDER, http)

    resp = await client.post(
        "/v1/chat/completions", headers=headers, json=_chat(HTTP_ROUTE, tools=TOOLS)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["tool_calls"] == TOOL_CALLS
    assert len(received) == 1
    assert received[0]["tools"] == TOOLS
    assert completed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_a_refusal_raised_by_forward_answers_the_same_400(
    client, headers, isolated, monkeypatch, stream
):
    """A session merges history after admission: forward() is the last check."""
    from plugboard import app as app_mod
    from plugboard.router import UnsupportedOnCliError

    completed, _ = isolated

    async def refusing_forward(body, **_):
        raise UnsupportedOnCliError(CLI_ID, ["content:image_url"])

    monkeypatch.setattr(app_mod, "cli_refusal", lambda body: None)
    monkeypatch.setattr(app_mod, "forward", refusing_forward)

    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_chat(CLI_ROUTE, stream=stream),
    )

    assert resp.status_code == 400, resp.text
    assert resp.json() == _refusal(["content:image_url"])
    assert completed == []


@pytest.mark.asyncio
async def test_a_refusal_raised_by_forward_stream_answers_the_same_400(
    client, headers, isolated, monkeypatch, http_provider
):
    """A streamed request to a non-CLI route goes through forward_stream()."""
    from plugboard import app as app_mod
    from plugboard.router import UnsupportedOnCliError

    completed, write = isolated
    http, received = http_provider
    write(CLI_PROVIDER, http)

    async def refusing_forward_stream(body, **_):
        raise UnsupportedOnCliError(CLI_ID, ["tools"])
        yield b""  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(app_mod, "cli_refusal", lambda body: None)
    monkeypatch.setattr(app_mod, "forward_stream", refusing_forward_stream)

    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_chat(HTTP_ROUTE, stream=True, tools=TOOLS),
    )

    assert resp.status_code == 400, resp.text
    assert resp.json() == _refusal(["tools"])
    assert received == []
    assert completed == []
