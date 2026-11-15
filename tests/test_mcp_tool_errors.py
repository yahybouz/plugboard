"""BUG-107: a chat the gateway refuses comes back as an MCP tool error.

The tools are called through a real MCP client session (in memory), so the
test reads what an MCP client reads: ``isError`` and the text content. A
refusal (``forbidden_model`` from the real handler, ``quota_exhausted``) must
give ``isError: true`` with the same ``Error: <code>: <detail>`` text as
before; an answer must give ``isError: false`` and the model's text.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web

pytest.importorskip("mcp")
from mcp.shared.memory import (  # noqa: E402
    create_connected_server_and_client_session,
)

from plugboard import mcp_server  # noqa: E402
from plugboard.app import (  # noqa: E402
    _cli_error_response,
    _model_not_allowed_handler,
    _ModelNotAllowed,
)
from plugboard.cli_subprocess import CliSubprocessError  # noqa: E402

FORBIDDEN = "forbidden_model: Model 'p:m' not allowed."


async def _forbidden_body() -> tuple[int, bytes]:
    exc = _ModelNotAllowed(status_code=403, detail="Model 'p:m' not allowed.")
    resp = await _model_not_allowed_handler(None, exc)
    return resp.status_code, resp.body


def _quota_body() -> tuple[int, bytes]:
    err = CliSubprocessError(
        "x", provider_name="claude", model_id="m", quota=True, retry_after=30
    )
    resp = _cli_error_response(err, "cust")
    return resp.status_code, resp.body


def _sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"


@pytest.fixture
async def gateway(monkeypatch):
    served: dict = {"status": 200, "body": b"", "sse": ""}

    async def chat(request):
        body = await request.json()
        if served["status"] == 200 and body.get("stream"):
            return web.Response(text=served["sse"], content_type="text/event-stream")
        return web.Response(
            body=served["body"],
            status=served["status"],
            headers={"Content-Type": "application/json"},
        )

    app = web.Application()
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


async def _call(tool: str, model: str = "p:m"):
    async with create_connected_server_and_client_session(mcp_server.mcp) as session:
        result = await session.call_tool(tool, {"model": model, "message": "hi"})
    assert len(result.content) == 1
    return result.isError, result.content[0].text


@pytest.mark.parametrize("tool", ["chat", "chat_stream"])
async def test_forbidden_model_is_a_tool_error_with_the_same_text(gateway, tool):
    gateway["status"], gateway["body"] = await _forbidden_body()
    assert gateway["status"] == 403
    assert await _call(tool) == (True, f"Error: {FORBIDDEN}")


@pytest.mark.parametrize("tool", ["chat", "chat_stream"])
async def test_quota_refusal_is_a_tool_error(gateway, tool):
    gateway["status"], gateway["body"] = _quota_body()
    api = json.loads(gateway["body"])
    assert await _call(tool) == (True, f"Error: {api['code']}: {api['detail']}")


@pytest.mark.parametrize("tool", ["chat", "chat_stream"])
async def test_an_answer_is_not_a_tool_error(gateway, tool):
    gateway["body"] = json.dumps(
        {"choices": [{"message": {"content": "pong"}}]}
    ).encode()
    gateway["sse"] = _sse({"choices": [{"delta": {"content": "pong"}}]})
    assert await _call(tool) == (False, "pong")


@pytest.mark.parametrize("tool", ["chat", "chat_stream"])
async def test_removed_connector_is_a_tool_error(gateway, tool):
    is_error, text = await _call(tool, model="openclaw:gpt-5")
    assert is_error is True
    assert text.startswith("Error: ") and "removed" in text.lower()


async def test_stream_cut_by_an_error_event_is_a_tool_error(gateway):
    gateway["sse"] = _sse(
        {"choices": [{"delta": {"content": "par"}}]},
        {"error": {"message": "The stream was interrupted.", "code": "x_cut"}},
    )
    is_error, text = await _call("chat_stream")
    assert is_error is True
    assert text == "par\n\nError: x_cut: The stream was interrupted."
