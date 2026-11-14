"""Lot B: the MCP tools give the client the same code and message as the API.

A fake gateway serves the exact bodies the real error helpers build (quota,
busy, refused subscription model, refused key, timeout). ``chat`` and
``chat_stream`` must show ``<code>: <detail>`` from that body, not the raw
JSON. ``list_models`` reads the very same catalogue route.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web

pytest.importorskip("mcp")
from plugboard import mcp_server  # noqa: E402
from plugboard.app import (  # noqa: E402
    _cli_error_response,
    _providers_busy_response,
)
from plugboard.cli_subprocess import CliSubprocessError  # noqa: E402
from plugboard.router import ProvidersBusyError  # noqa: E402


def _cli(**kw):
    return CliSubprocessError(
        "x", provider_name="claude", model_id="claude-sonnet-4-6", **kw
    )


def _scenarios():
    return {
        "quota": _cli_error_response(_cli(quota=True, retry_after=30), "cust"),
        "unsupported": _cli_error_response(_cli(model_unsupported=True), "cust"),
        "timeout": _cli_error_response(_cli(timed_out=True), "cust"),
        "busy": _providers_busy_response(ProvidersBusyError(["p"], "m"), "cust"),
    }


@pytest.fixture
async def gateway(monkeypatch):
    served = {"scenario": None, "models": [], "built": _scenarios()}

    async def models(request):
        return web.json_response({"object": "list", "data": served["models"]})

    async def chat(request):
        body = await request.json()
        resp = served["built"][served["scenario"]]
        if body.get("stream"):
            # Refused before the first byte: same JSON error, not an SSE body.
            pass
        return web.Response(
            body=resp.body,
            status=resp.status_code,
            headers={"Content-Type": "application/json"},
        )

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(mcp_server, "_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(mcp_server, "_API_KEY", "pb_client")
    monkeypatch.setattr(mcp_server, "_HTTP_SESSION", None)
    try:
        yield served
    finally:
        if mcp_server._HTTP_SESSION is not None:
            await mcp_server._HTTP_SESSION.close()
        await runner.cleanup()


@pytest.mark.parametrize("name", ["quota", "unsupported", "timeout", "busy"])
@pytest.mark.parametrize("tool", ["chat", "chat_stream"])
async def test_chat_error_is_code_and_detail_of_the_api(gateway, name, tool):
    gateway["scenario"] = name
    api = json.loads(gateway["built"][name].body)
    out = await getattr(mcp_server, tool)(model="claude-code:m", message="hi")
    assert out == f"Error: {api['code']}: {api['detail']}"


async def test_error_without_a_code_keeps_the_status(gateway, monkeypatch):
    msg = mcp_server._client_error(401, json.dumps({"detail": "Invalid API key"}))
    assert msg == "HTTP 401: Invalid API key"
    assert mcp_server._client_error(502, "<html>bad gateway</html>").startswith(
        "HTTP 502: <html>"
    )


async def test_mcp_list_models_shows_exactly_the_api_ids(gateway):
    gateway["models"] = [
        {"id": "claude-code:claude-sonnet-4-6", "owned_by": "claude-code"},
        {"id": "Codex:gpt-5.6-luna", "owned_by": "Codex"},
    ]
    out = await mcp_server.list_models()
    ids = {m["id"] for m in gateway["models"]}
    assert ids == set(mcp_server._model_ids_from_text(out))


async def test_stream_error_event_is_reported(gateway, monkeypatch):
    """A stream cut after its first chunk ends with an error event: the tool
    returns the text it got and the same ``code: message``."""
    sse = (
        'data: {"choices":[{"delta":{"content":"par"}}]}\n\n'
        'data: {"error":{"message":"The stream was interrupted.",'
        '"code":"stream_interrupted"}}\n\n'
        "data: [DONE]\n\n"
    )

    async def chat(request):
        return web.Response(text=sse, content_type="text/event-stream")

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(mcp_server, "_BASE_URL", f"http://127.0.0.1:{port}")
    try:
        out = await mcp_server.chat_stream(model="m", message="hi")
    finally:
        await runner.cleanup()
    assert out.startswith("par")
    assert "Error: stream_interrupted: The stream was interrupted." in out


def test_client_error_shapes():
    long = json.dumps({"detail": "x" * 500, "code": "quota_exhausted"})
    assert mcp_server._client_error(429, long) == "quota_exhausted: " + "x" * 300
    listed = json.dumps({"detail": [{"loc": ["body"]}]})
    assert mcp_server._client_error(422, listed) == 'HTTP 422: [{"loc": ["body"]}]'


async def test_forbidden_model_body_of_the_real_handler(gateway):
    from plugboard.app import _model_not_allowed_handler, _ModelNotAllowed

    exc = _ModelNotAllowed(status_code=403, detail="Model 'p:m' not allowed.")
    body = json.loads((await _model_not_allowed_handler(None, exc)).body)
    assert (
        mcp_server._client_error(403, json.dumps(body))
        == "forbidden_model: Model 'p:m' not allowed."
    )
