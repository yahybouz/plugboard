"""Ollama switch end to end (PLU-12): off, then on again, without a restart.

One app, one registry. The admin turns Ollama off through the switch: no
Ollama model is listed and no call is made for the chat, the embeddings, the
preflight or the MCP tools. Turned back on, everything comes back. The MCP
tools talk HTTP to a loopback relay that hands each request to the app, so
they go through the very routes a real MCP client uses.
"""

from __future__ import annotations

import json

import httpx
import pytest
from aiohttp import web

from plugboard import ollama, router
from tests.test_model_not_found import ACME, _isolated_providers
from tests.test_ollama_switch import ADMIN_KEY, BETA, OLLAMA, _chat, _preflight

pytest.importorskip("mcp")
from plugboard import mcp_server  # noqa: E402

EMBEDDING_MODEL = "ollama:qwen3-embedding:0.6b"
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
async def app_client(tmp_path, monkeypatch):
    """The app with Ollama on, a client key, a recorded forward() and daemon."""
    import plugboard.app as app_module
    import plugboard.ratelimit as rl
    from plugboard import apikeys, audit, usage
    from tests.factories import FAKE_COMPLETION

    _isolated_providers(
        tmp_path, monkeypatch, [{**OLLAMA, "is_active": True}, ACME, BETA]
    )
    monkeypatch.setenv("PLUGBOARD_EMBEDDING_MODELS", EMBEDDING_MODEL)
    monkeypatch.setattr(app_module, "_API_KEY", "instance-key")
    monkeypatch.setattr(app_module, "_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setattr(rl, "_LIMIT", 0)
    monkeypatch.setattr(apikeys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(apikeys, "KEYS_LOCK", tmp_path / "keys.lock")
    monkeypatch.setattr(audit, "_DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    key = apikeys.create_key("acme", "switch-e2e")["key"]

    calls: list = []

    async def _forward(body, **_kwargs):
        calls.append(body.get("model"))
        return dict(FAKE_COMPLETION)

    async def _daemon_list():
        calls.append("ollama.list_models")
        return [{"name": "qwen3:8b"}, {"name": "llama3:latest"}]

    async def _no_daemon(*_args, **_kwargs):
        raise AssertionError("no real Ollama call")

    monkeypatch.setattr(app_module, "forward", _forward)
    monkeypatch.setattr(ollama, "list_models", _daemon_list)
    monkeypatch.setattr(ollama, "check_available", _no_daemon)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        yield client, {"Authorization": f"Bearer {key}"}, key, calls


@pytest.fixture
async def mcp_tools(app_client, monkeypatch):
    """The MCP server's HTTP client pointed at a loopback relay to the app."""
    client, _auth, key, _calls = app_client
    hop = {"authorization", "content-type", "x-plugboard-surface", "accept"}

    async def relay(request: web.Request) -> web.Response:
        resp = await client.request(
            request.method,
            request.path_qs,
            content=await request.read(),
            headers={k: v for k, v in request.headers.items() if k.lower() in hop},
        )
        return web.Response(
            status=resp.status_code,
            body=resp.content,
            content_type=resp.headers.get("content-type", "application/json").split(
                ";"
            )[0],
        )

    gateway = web.Application()
    gateway.router.add_route("*", "/{path:.*}", relay)
    runner = web.AppRunner(gateway)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(mcp_server, "_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(mcp_server, "_API_KEY", key)
    monkeypatch.setattr(mcp_server, "_ADMIN_KEY", "")
    monkeypatch.setattr(mcp_server, "_HTTP_SESSION", None)
    try:
        yield mcp_server
    finally:
        if mcp_server._HTTP_SESSION is not None:
            await mcp_server._HTTP_SESSION.close()
        await runner.cleanup()


async def _model_ids(client, auth) -> list:
    resp = await client.get("/v1/models", headers=auth)
    assert resp.status_code == 200, resp.text
    return [m["id"] for m in resp.json()["data"]]


async def _switch(client, enabled: bool) -> dict:
    resp = await client.put(
        "/admin/ollama/switch", json={"enabled": enabled}, headers=ADMIN
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_switch_off_then_on_without_restart(app_client, mcp_tools):
    client, auth, _key, calls = app_client
    assert "ollama:qwen3:8b" in await _model_ids(client, auth)

    # ── Off ──
    assert (await _switch(client, False))["enabled"] is False
    assert not [m for m in await _model_ids(client, auth) if m.startswith("ollama:")]
    v2 = (await client.get("/v2/models", headers=auth)).json()["data"]
    assert not [m for m in v2 if m["id"].startswith("ollama:")]

    for model in ("ollama:qwen3:8b", "qwen3:8b"):
        for stream in (False, True):
            resp = await client.post(
                "/v1/chat/completions",
                headers=auth,
                json=_chat(model, stream=stream),
            )
            assert resp.status_code == 503, (model, stream, resp.text)
            assert resp.json()["code"] == "provider_disabled"

    resp = await client.post(
        "/v1/embeddings", headers=auth, json={"model": EMBEDDING_MODEL, "input": "hi"}
    )
    assert resp.status_code == 503, resp.text
    emb = (await client.get("/v1/embeddings/models", headers=auth)).json()["data"]
    assert [(m["id"], m["available"]) for m in emb] == [(EMBEDDING_MODEL, False)]

    assert (await _preflight(client, auth, "ollama", "qwen3:8b"))[
        "error"
    ] == "provider_disabled"

    assert "ollama:" not in await mcp_tools.list_models()
    out = await mcp_tools.chat(model="ollama:qwen3:8b", message="hi")
    assert "HTTP 503" in out and "provider_disabled" in out
    out = json.loads(await mcp_tools.embed(input="hi", model=EMBEDDING_MODEL))
    assert out["error"]["code"] == "engine_unavailable"

    assert calls == [], "while off, nothing is called"

    # ── On again ──
    state = await _switch(client, True)
    assert state["enabled"] is True
    assert calls == ["ollama.list_models"], "one model sync when turned back on"
    calls.clear()
    router.invalidate_models_cache()

    assert "ollama:qwen3:8b" in await _model_ids(client, auth)
    assert "ollama:qwen3:8b" in await mcp_tools.list_models()
    resp = await client.post(
        "/v1/chat/completions", headers=auth, json=_chat("ollama:qwen3:8b")
    )
    assert resp.status_code == 200, resp.text
    out = await mcp_tools.chat(model="ollama:qwen3:8b", message="hi")
    assert not out.startswith("Error"), out
    assert calls == ["ollama:qwen3:8b", "ollama:qwen3:8b"]
    emb = (await client.get("/v1/embeddings/models", headers=auth)).json()["data"]
    assert [(m["id"], m["available"]) for m in emb] == [(EMBEDDING_MODEL, True)]
    assert (await _preflight(client, auth, "ollama", "qwen3:8b"))["ok"] is True
