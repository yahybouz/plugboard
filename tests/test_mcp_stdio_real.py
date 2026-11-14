"""Lot B: the MCP server, started the way a client starts it, over stdio.

The command and variables are those of docs/guide/mcp.md. The gateway is a
fake HTTP server on loopback: no provider, no CLI, no real key. The client is
the MCP SDK's own stdio client, so this is what Claude Code or Cursor does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
KEY = "pb_test_key_not_real_0123456789"

CATALOGUE = [
    {"id": "claude-code:claude-sonnet-4-6", "owned_by": "claude-code"},
    {"id": "Codex:gpt-5.6-luna", "owned_by": "Codex"},
    {"id": "agy:gemini-3-flash", "owned_by": "agy"},
]


@pytest.fixture
async def gateway():
    seen = []

    async def models(request):
        seen.append(("models", dict(request.headers)))
        return web.json_response({"object": "list", "data": CATALOGUE})

    async def me(request):
        seen.append(("me", dict(request.headers)))
        return web.json_response(
            {"client_id": "cust", "current_month": "2026-11", "usage": {}}
        )

    async def chat(request):
        body = await request.json()
        seen.append(("chat", dict(request.headers), body))
        if body["model"] == "Codex:gpt-5.6-luna":
            return web.json_response(
                {
                    "detail": "The Codex provider's subscription does not support "
                    "the model 'gpt-5.6-luna'.",
                    "code": "model_not_supported_by_subscription",
                },
                status=400,
            )
        return web.json_response(
            {
                "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                "usage": {"total_tokens": 7},
            }
        )

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_get("/v1/me", me)
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", seen
    finally:
        await runner.cleanup()


def _params(url, tmp_path):
    # The exact shape of the client configuration in docs/guide/mcp.md.
    return StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "run_mcp.py")],
        env={
            "PLUGBOARD_URL": url,
            "PLUGBOARD_API_KEY": KEY,
            "PLUGBOARD_SKIP_DOTENV": "1",
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
        },
        cwd=str(tmp_path),
    )


def _text(result) -> str:
    return "".join(c.text for c in result.content if getattr(c, "text", None))


async def test_stdio_server_lists_calls_and_reports_errors(gateway, tmp_path):
    url, seen = gateway
    async with stdio_client(
        _params(url, tmp_path), errlog=open(tmp_path / "err", "w")
    ) as (
        read,
        write,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"chat", "chat_stream", "list_models", "check_usage"} <= tools

            listed = _text(await session.call_tool("list_models", {}))
            for model in CATALOGUE:
                assert f"`{model['id']}`" in listed

            ok = _text(
                await session.call_tool(
                    "chat",
                    {"model": "claude-code:claude-sonnet-4-6", "message": "ping"},
                )
            )
            assert ok.startswith("pong")

            refused = _text(
                await session.call_tool(
                    "chat", {"model": "Codex:gpt-5.6-luna", "message": "ping"}
                )
            )
            assert refused.startswith(
                "Error: model_not_supported_by_subscription: The Codex provider"
            )

            usage = _text(await session.call_tool("check_usage", {}))
            assert "cust" in usage

    # The client key, and only it, went to the gateway, tagged as MCP.
    assert seen
    for entry in seen:
        headers = entry[1]
        assert headers["Authorization"] == f"Bearer {KEY}"
        assert headers["X-Plugboard-Surface"] == "mcp"
    chat_body = next(e[2] for e in seen if e[0] == "chat")
    assert chat_body["messages"][-1] == {"role": "user", "content": "ping"}
    # The key never comes back to the caller.
    assert KEY not in listed + ok + refused + usage
    assert KEY not in json.dumps([e[2:] for e in seen])


def test_guide_configurations_match_what_the_test_runs():
    """The guide's JSON snippets name the real script and variables."""
    import re

    text = (ROOT / "docs" / "guide" / "mcp.md").read_text()
    blocks = re.findall(r"```json\n(.*?)```", text, flags=re.DOTALL)
    configs = [
        json.loads(b)["mcpServers"]["plugboard"] for b in blocks if "mcpServers" in b
    ]
    assert len(configs) >= 2
    local, container = configs[0], configs[1]
    assert local["args"][0].endswith("scripts/run_mcp.py")
    assert {"PLUGBOARD_URL", "PLUGBOARD_API_KEY"} <= set(local["env"])
    assert container["command"] == "docker"
    assert "/opt/venv-mcp/bin/python" in container["args"]
    assert container["args"][-1].endswith("scripts/run_mcp.py")
    assert (ROOT / "scripts" / "run_mcp.py").exists()
