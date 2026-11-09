"""MCP embedding tools through a loopback fake gateway (client key only)."""

import json

import pytest
from aiohttp import web

pytest.importorskip("mcp")
from plugboard import mcp_server  # noqa: E402

MODEL = "ollama:qwen3-embedding:0.6b"


def _served(vectors):
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in reversed(list(enumerate(vectors)))
        ],
        "model": MODEL,
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
        "plugboard": {
            "provider_id": "ollama",
            "native_model": "qwen3-embedding:0.6b",
            "digest": "d" * 64,
            "dimensions": len(vectors[0]),
            "cost_usd": 0.0,
            "usage_recorded": True,
        },
    }


@pytest.fixture
async def gateway(monkeypatch):
    calls = []
    settings = {"status": 200, "body": {}}

    async def handle(request):
        body = await request.json() if request.method == "POST" else None
        calls.append(
            (request.method, request.path, request.headers.get("Authorization"), body)
        )
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


async def test_embed_calls_the_embeddings_route_with_the_client_key(gateway):
    settings, calls = gateway
    settings["body"] = _served([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]])
    out = json.loads(await mcp_server.embed(["a", "b"], MODEL))
    assert out["model"] == MODEL and out["count"] == 2 and out["dimensions"] == 3
    assert out["usage"] == {"prompt_tokens": 5, "total_tokens": 5}
    assert out["vectors"] == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]  # input order
    assert out["provider_id"] == "ollama"
    assert calls == [
        (
            "POST",
            "/v1/embeddings",
            "Bearer pb_client_only",
            {"model": MODEL, "input": ["a", "b"], "encoding_format": "float"},
        )
    ]


async def test_embed_can_omit_vectors(gateway):
    settings, _ = gateway
    settings["body"] = _served([[0.1, 0.2]])
    out = json.loads(await mcp_server.embed("one text", MODEL, include_vectors=False))
    assert "vectors" not in out and out["count"] == 1 and out["dimensions"] == 2


async def test_list_embedding_models_uses_the_client_route(gateway):
    settings, calls = gateway
    settings["body"] = {"object": "list", "data": [{"id": MODEL, "available": True}]}
    out = json.loads(await mcp_server.list_embedding_models())
    assert out["data"][0]["id"] == MODEL
    assert calls == [("GET", "/v1/embeddings/models", "Bearer pb_client_only", None)]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "client_key_refused"),
        (403, "model_not_permitted"),
        (404, "model_not_available"),
        (422, "invalid_request"),
        (503, "engine_unavailable"),
        (500, "gateway_error"),
    ],
)
async def test_embed_errors_are_bounded(gateway, status, code):
    settings, _ = gateway
    settings["status"] = status
    settings["body"] = {"detail": "x" * 5000}  # long upstream detail is dropped
    out = json.loads(await mcp_server.embed("t", MODEL))
    assert out["error"]["code"] == code
    assert len(out["error"]["message"]) < 200


async def test_embed_keeps_a_short_gateway_detail(gateway):
    settings, _ = gateway
    settings["status"] = 404
    settings["body"] = {"detail": "Embedding model not available"}
    out = json.loads(await mcp_server.embed("t", "ollama:other"))
    assert out["error"]["message"].endswith("Embedding model not available")


@pytest.mark.parametrize("raw", ["not json", "[]", json.dumps({"data": []})])
async def test_embed_rejects_invalid_gateway_answers(gateway, raw):
    settings, _ = gateway
    settings["raw"] = raw
    out = json.loads(await mcp_server.embed("t", MODEL))
    assert out["error"]["code"] == "invalid_response"


@pytest.mark.parametrize(
    ("value", "model"), [([], MODEL), ([""], MODEL), (["a"] * 257, MODEL), ("a", "  ")]
)
async def test_embed_validates_before_calling(gateway, value, model):
    _, calls = gateway
    out = json.loads(await mcp_server.embed(value, model))
    assert out["error"]["code"] == "invalid_request"
    assert calls == []


async def test_tools_never_use_the_admin_key(gateway, monkeypatch):
    _, calls = gateway
    monkeypatch.setattr(mcp_server, "_API_KEY", "")
    for result in (
        await mcp_server.embed("t", MODEL),
        await mcp_server.list_embedding_models(),
    ):
        assert json.loads(result)["error"]["code"] == "client_key_required"
    assert calls == []


async def test_embedding_tools_are_registered():
    names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert {"embed", "list_embedding_models"} <= names
