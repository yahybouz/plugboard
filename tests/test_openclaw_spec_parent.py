"""Literal SPEC oracles, private scratch; no network, processes, or ML."""

import pytest
from plugboard import (
    router,
    model_exposure,
    model_availability,
    mcp_server,
    app,
    config,
)
from plugboard.legacy_connectors import is_removed_model
from tests.test_decision_routing_v5 import provider, BeforeTransport


@pytest.fixture
def direct(monkeypatch):
    monkeypatch.setattr(model_exposure, "mode", lambda: "all")
    monkeypatch.setattr(router, "_model_status_mtime_changed", lambda: False)
    router.invalidate_models_cache()
    return router


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical,alias,kind",
    [
        ("claude-via-claude-cli", "claude-code", "claude"),
        ("chatgpt-via-codex-cli", "Codex", "codex"),
        ("google-gemini-via-gemini-cli", "Gemini", "gemini"),
    ],
)
@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
async def test_catalogue_id_must_roundtrip_exact_native(
    direct, monkeypatch, canonical, alias, kind, mode
):
    pid = canonical + ":lane"
    p = provider(pid, "qwen3:jev", ["qwen3:jev"], auth_source="subprocess:" + kind)
    monkeypatch.setattr(router, "load_providers", lambda: [p])
    assert router.resolve_provider_and_model(pid + ":qwen3:jev") == (p, "qwen3:jev")
    rows = await router.list_models()
    assert router.resolve_provider_and_model(pid + ":qwen3:jev") == (p, "qwen3:jev")
    assert len(rows) == 1
    request = rows[0]["id"]
    if mode in ("single", "multiple"):
        result = (
            router.resolve_provider_and_model(request)
            if mode == "single"
            else router._resolve_all_providers(request)
        )
        pairs = result if isinstance(result, list) else [result]
        assert [(p["id"], n) for p, n in pairs] == [(pid, "qwen3:jev")]
        return
    from plugboard import cli_subprocess, http_pool, circuit_breaker

    calls = []

    async def complete(kind, body, **kwargs):
        calls.append((kind, body["model"]))
        raise BeforeTransport()

    async def pool():
        return object()

    monkeypatch.setattr(cli_subprocess, "complete", complete)
    monkeypatch.setattr(http_pool, "get_session", pool)
    monkeypatch.setattr(circuit_breaker, "check_circuit", lambda _: {"allowed": True})
    with pytest.raises(BeforeTransport):
        if mode == "forward":
            await router.forward({"model": request})
        else:
            await anext(router.forward_stream({"model": request}))
    assert calls == [(kind, "qwen3:jev")]


@pytest.mark.asyncio
async def test_unknown_connectivity_not_reported_as_connected(direct, monkeypatch):
    p = provider("team-openclaw-notes", "qwen3:jev", ["qwen3:jev"])
    monkeypatch.setattr(router, "load_providers", lambda: [p])
    monkeypatch.setattr(model_availability, "load_model_status", lambda: {})
    rows = await router.list_models()
    assert rows[0].get("connected") is not True


@pytest.mark.asyncio
async def test_missing_mcp_health_must_not_get_green_icon(monkeypatch):
    async def get(path):
        return {
            "data": [{"id": "team:qwen3:jev", "owned_by": "team", "provider": "team"}]
        }

    monkeypatch.setattr(mcp_server, "_api_get", get)
    assert "🟢" not in await mcp_server.list_models()


def test_new_model_transform_must_have_linear_copy_bound():
    class S(str):
        cost = 0

        def strip(self, *a):
            return S(super().strip(*a))

        def lower(self):
            return S(super().lower())

        def partition(self, sep):
            values = super().partition(sep)
            S.cost += len(values[2])
            return tuple(S(v) for v in values)

        def __getitem__(self, k):
            return S(super().__getitem__(k))

    name = S("codex:" * 1024 + "gpt-5")
    assert is_removed_model(name) is False
    assert S.cost <= 32 * len(name), (
        f"{S.cost} copied suffix chars for {len(name)} input chars"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["router", "api", "mcp", "monitor", "api-catalog"])
async def test_registered_canonical_colon_owner_not_legacy(direct, monkeypatch, mode):
    # Explicit configured identity, not an inferred HF provider/namespace.
    p = provider("claude-via-claude-cli:openclaw", "qwen3:jev", ["qwen3:jev"])
    request = p["id"] + ":qwen3:jev"
    monkeypatch.setattr(router, "load_providers", lambda: [p])
    monkeypatch.setattr(app, "load_providers", lambda: [p])
    if mode == "router":
        assert router.resolve_provider_and_model(request) == (p, "qwen3:jev")
    elif mode == "mcp":
        sent = []

        async def post(path, body):
            sent.append(body["model"])
            return {"choices": [{"message": {"content": "synthetic"}}]}

        monkeypatch.setattr(mcp_server, "_api_post", post)
        result = await mcp_server.chat(request, "synthetic")
        assert sent == [request], result
    elif mode == "monitor":
        from plugboard import monitoring

        monkeypatch.setattr(monitoring, "load_providers", lambda: [p])

        class End(BaseException):
            pass

        def session():
            raise End()

        monkeypatch.setattr(monitoring.aiohttp, "ClientSession", session)
        with pytest.raises(End):
            await monitoring.check_model_availability(request, "synthetic")
    else:
        from httpx import AsyncClient, ASGITransport

        monkeypatch.setattr(app, "_API_KEY", "synthetic-api")
        monkeypatch.setattr(app, "_ADMIN_KEY", "synthetic-api")
        monkeypatch.setattr(app, "estimate_prompt_tokens", lambda _: 1)
        monkeypatch.setattr(app, "estimate_completion_tokens", lambda _: 1)

        async def forward(body):
            return {
                "choices": [{"message": {"content": "synthetic"}}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

        monkeypatch.setattr(app, "forward", forward)
        async with AsyncClient(
            transport=ASGITransport(app=app.app), base_url="http://synthetic"
        ) as client:
            if mode == "api-catalog":
                reply = await client.get(
                    "/v1/models", headers={"Authorization": "Bearer synthetic-api"}
                )
                assert reply.status_code == 200, reply.text
                assert len(reply.json()["data"]) == 1, reply.text
            else:
                reply = await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer synthetic-api"},
                    json={
                        "model": request,
                        "messages": [{"role": "user", "content": "synthetic"}],
                        "cache": False,
                    },
                )
        assert reply.status_code == 200, reply.text
        assert reply.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_scheduler_discovery_rejects_default_wrapper_before_auth_or_pool(
    monkeypatch,
):
    from plugboard import http_pool

    p = provider("direct", "openclaw/openai--gpt-5", auto_discover=True)
    calls = []

    async def auth(p):
        calls.append("auth")
        return {}

    class TransportEnd(BaseException):
        pass

    class Session:
        def get(self, *args, **kwargs):
            calls.append("GET /models")
            raise TransportEnd()

    async def pool():
        calls.append("pool")
        return Session()

    monkeypatch.setattr(router, "_build_auth_header_async", auth)
    monkeypatch.setattr(http_pool, "get_session", pool)
    try:
        await model_availability.discover_openai_models(p)
    except ValueError as exc:
        assert "removed" in str(exc).lower()
    except TransportEnd:
        pass
    assert calls == [], f"Legacy wrapper config reached {calls}"


@pytest.mark.asyncio
async def test_quarantine_uses_longest_actual_owner_not_prefix(monkeypatch):
    short = provider("owner", "gpt-5", auth_source="openclaw")
    long = provider("owner:lane", "qwen3:jev")
    key = "owner:lane:qwen3:jev"
    previous = {
        "models": {
            key: {
                "connected": True,
                "provider": "owner:lane",
                "last_probed_at": "synthetic-observation",
            }
        }
    }
    monkeypatch.setattr(config, "load_providers", lambda: [short, long])
    monkeypatch.setattr(model_availability, "load_model_status", lambda path: previous)
    monkeypatch.setattr(model_availability, "save_model_status", lambda *args: None)
    result = await model_availability.run_connectivity_probes(probe_kinds=set())
    assert result["models"][key] == previous["models"][key]


@pytest.mark.asyncio
async def test_advertised_long_owner_must_not_select_short_cli_account(
    direct, monkeypatch
):
    short = provider(
        "claude-via-claude-cli",
        "claude-sonnet-4-6",
        ["claude-sonnet-4-6"],
        auth_source="subprocess:claude",
    )
    long = provider(
        "claude-via-claude-cli:lane",
        "qwen3:jev",
        ["qwen3:jev"],
        auth_source="subprocess:claude",
    )
    monkeypatch.setattr(router, "load_providers", lambda: [short, long])
    original = "claude-via-claude-cli:lane:qwen3:jev"
    assert router.resolve_provider_and_model(original) == (long, "qwen3:jev")
    rows = await router.list_models()
    row = next(row for row in rows if row["owned_by"] == long["id"])
    selected, native = router.resolve_provider_and_model(row["id"])
    assert (selected["id"], native) == (long["id"], "qwen3:jev")
