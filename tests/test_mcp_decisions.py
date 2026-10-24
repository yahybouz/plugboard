"""Dedicated MCP tool tests through a loopback fake gateway, no model inference.

Run in the MCP environment with pytest available; the normal environment skips
this module when the optional MCP dependency is absent.
"""

import asyncio
import json

import pytest
from aiohttp import web

pytest.importorskip("mcp")
from plugboard import mcp_server  # noqa: E402

QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": "Pick",
        "criteria": {"yes": "Proceed", "no": None},
    }
}


@pytest.fixture
async def gateway(monkeypatch):
    calls = []
    settings = {"status": 200, "body": {"model": "jev", "answers": {}}, "delay": 0}

    async def handle(request):
        body = await request.json() if request.method == "POST" else None
        calls.append(
            (request.method, request.path, request.headers.get("Authorization"), body)
        )
        await asyncio.sleep(settings["delay"])
        if "raw" in settings:
            return web.Response(text=settings["raw"], status=settings["status"])
        return web.json_response(settings["body"], status=settings["status"])

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(mcp_server, "_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(mcp_server, "_API_KEY", "pb_client_only")
    monkeypatch.setattr(mcp_server, "_ADMIN_KEY", "admin_never_forward")
    monkeypatch.setattr(mcp_server, "_HTTP_SESSION", None)
    try:
        yield settings, calls
    finally:
        if mcp_server._HTTP_SESSION is not None:
            await mcp_server._HTTP_SESSION.close()
        await runner.cleanup()


async def _tool(name, *args):
    function = getattr(mcp_server, name, None)
    assert function is not None, f"Missing dedicated MCP tool: {name}"
    return json.loads(await function(*args))


async def test_decision_tools_use_dedicated_routes_and_client_key(gateway):
    settings, calls = gateway
    settings["body"] = {"data": [{"id": "jev", "available": False}]}
    assert (await _tool("list_decision_models"))["data"][0]["id"] == "jev"
    settings["body"] = {"model": "jev", "answers": {}}
    assert (await _tool("decide", "jev", "Synthetic state", QUESTIONS))[
        "model"
    ] == "jev"
    assert calls == [
        ("GET", "/v1/decisions/models", "Bearer pb_client_only", None),
        (
            "POST",
            "/v1/decisions",
            "Bearer pb_client_only",
            {"model": "jev", "state": "Synthetic state", "questions": QUESTIONS},
        ),
    ]


async def test_decision_tools_do_not_escalate_or_dispatch_without_client_key(
    gateway, monkeypatch
):
    _, calls = gateway
    monkeypatch.setattr(mcp_server, "_API_KEY", "")
    for name, args in [
        ("list_decision_models", ()),
        ("decide", ("jev", "private", QUESTIONS)),
    ]:
        assert "error" in await _tool(name, *args)
    assert calls == []


@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500, 503])
async def test_decision_errors_hide_raw_gateway_bodies(gateway, status):
    settings, _ = gateway
    settings.update(
        status=status, raw="state secret pb_client_only admin_never_forward"
    )
    result = await _tool("decide", "jev", "private", QUESTIONS)
    assert "error" in result
    serialized = json.dumps(result)
    for sensitive in (
        "state secret",
        "pb_client_only",
        "admin_never_forward",
        "private",
    ):
        assert sensitive not in serialized


async def test_decision_timeout_is_bounded_and_normalized(gateway, monkeypatch):
    settings, _ = gateway
    settings["delay"] = 0.05
    monkeypatch.setattr(mcp_server, "_DECISION_TIMEOUT", 0.01, raising=False)
    result = await _tool("decide", "jev", "private", QUESTIONS)
    assert result == {
        "error": {"code": "decision_timeout", "message": "Decision gateway timed out"}
    }


@pytest.mark.parametrize(
    "raw",
    ["not-json secret", "[1, 2]", '{"error":"private secret"}', "x" * 524289],
    ids=["malformed", "array", "error", "oversized"],
)
async def test_decision_invalid_or_oversized_json_is_normalized(gateway, raw):
    settings, _ = gateway
    settings["raw"] = raw
    result = await _tool("list_decision_models")
    assert "error" in result
    assert "secret" not in json.dumps(result)


async def test_registered_decision_tools_expose_named_questions():
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}
    assert {"decide", "list_decision_models"} <= tools.keys()
    schema = tools["decide"].inputSchema
    assert schema["required"] == ["model", "state", "questions"]
    assert schema["properties"]["questions"]["type"] == "object"
    assert set(schema["properties"]["model"]["enum"]) == {
        "jev",
        "laya",
        "laya-multilingual",
    }


async def test_removed_kev_is_rejected_before_mcp_transport(monkeypatch):
    async def never(*args):
        pytest.fail("Removed Kev must not reach the gateway")

    monkeypatch.setattr(mcp_server, "_decision_request", never)
    result = await _tool("decide", "kev-0.8b", "Synthetic state", QUESTIONS)
    assert result == {
        "error": {"code": "invalid_request", "message": "Unsupported decision model"}
    }
