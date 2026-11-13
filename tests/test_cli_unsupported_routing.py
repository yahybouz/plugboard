"""The router drops a CLI subscription route for a request it cannot serve.

A CLI subscription route (``auth_source`` ``subprocess:<cli>``) hands the
conversation to a CLI as plain text: tools, a structured response format or a
non-text content part used to be dropped silently. forward() now leaves such
a route out of the candidates; when no other candidate is left it raises
``UnsupportedOnCliError`` before the key's route guard, any provider slot, any
circuit read and any CLI call. forward_stream() takes its routes the same way
(PLU-26). ``cli_refusal(body)`` tells the chat route beforehand whether
forward() would raise it.

No CLI runs: ``cli_subprocess.complete`` is replaced by a counter. The HTTP
provider is a real server on 127.0.0.1 that records the body it received.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web

from tests.factories import FAKE_COMPLETION, PROVIDER_A

CLI_ID = "pbtest-claude-cli"
CLI_MODEL = "pbtest-claude-model"
CLI_PROVIDER = {
    "id": CLI_ID,
    "name": "Claude CLI test",
    "base_url": "subprocess://claude",
    "bearer_token": "(via CLI)",
    "default_model": CLI_MODEL,
    "auth_source": "subprocess:claude",
    "provider_type": "cloud",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
OTHER_CLI = {
    **CLI_PROVIDER,
    "id": "pbtest-codex-cli",
    "auth_source": "subprocess:codex",
}

ONE_CLI_ROUTE = f"{CLI_ID}:{CLI_MODEL}"
# No registered prefix: every active provider is a candidate, in file order.
ANY_ROUTE = "pbtest-shared-model"

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


def _chat(model: str, **extra) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        **extra,
    }


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Fresh breaker, alerts, metrics, admission; counters on every side effect."""
    import plugboard.alerts as alerts
    import plugboard.config as cfg
    import plugboard.metrics as metrics
    import plugboard.router as router
    from plugboard import circuit_breaker as cb
    from plugboard import cli_subprocess, provider_admission

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
    monkeypatch.setattr(cfg, "PROVIDERS_FILE", data_dir / "providers.json")
    monkeypatch.setattr(cfg, "PROVIDERS_LOCK", data_dir / "providers.json.lock")
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    # Equal load-balancing scores keep the candidates in providers.json order.
    monkeypatch.setattr(metrics, "_stats", {})
    provider_admission.reset()

    calls = {"complete": [], "acquire": 0, "circuit": 0, "guard": 0}

    async def fake_complete(kind, body, *, timeout_s=None):
        calls["complete"].append((kind, body))
        return json.loads(json.dumps(FAKE_COMPLETION))

    real_acquire = provider_admission.try_acquire
    real_check = cb.check_circuit

    def counting_acquire(provider):
        calls["acquire"] += 1
        return real_acquire(provider)

    def counting_check(key):
        calls["circuit"] += 1
        return real_check(key)

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    monkeypatch.setattr(provider_admission, "try_acquire", counting_acquire)
    monkeypatch.setattr(cb, "check_circuit", counting_check)

    def guard(provider, real_model):
        calls["guard"] += 1

    def write(*providers: dict) -> None:
        cfg.PROVIDERS_FILE.write_text(json.dumps({"providers": list(providers)}))
        router.invalidate_models_cache()

    yield router, calls, write, guard
    provider_admission.reset()


@pytest.fixture
async def http_provider():
    """PROVIDER_A served on 127.0.0.1; records each request body it receives."""
    received: list[dict] = []

    async def handle(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response(FAKE_COMPLETION)

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


def _nothing_held(calls: dict) -> None:
    from plugboard import circuit_breaker as cb
    from plugboard import provider_admission

    assert calls["complete"] == []
    assert calls["acquire"] == 0
    assert provider_admission.in_flight(CLI_ID) == 0
    assert calls["circuit"] == 0
    assert cb._circuits == {}


@pytest.mark.asyncio
async def test_forward_refuses_tools_on_a_lone_cli_route(isolated):
    router, calls, write, guard = isolated
    write(CLI_PROVIDER)

    with pytest.raises(router.UnsupportedOnCliError) as err:
        await router.forward(_chat(ONE_CLI_ROUTE, tools=TOOLS), route_guard=guard)

    assert err.value.provider_id == CLI_ID
    assert err.value.unsupported == ["tools"]
    assert calls["guard"] == 0
    _nothing_held(calls)


@pytest.mark.asyncio
async def test_forward_skips_the_cli_route_for_the_http_one(isolated, http_provider):
    router, calls, write, guard = isolated
    http, received = http_provider
    write(CLI_PROVIDER, http)  # the CLI, without a model list, comes first

    result = await router.forward(_chat(ANY_ROUTE, tools=TOOLS), route_guard=guard)

    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert calls["complete"] == []
    assert len(received) == 1
    assert received[0]["tools"] == TOOLS
    assert router.served_by()[0]["id"] == PROVIDER_A["id"]


@pytest.mark.asyncio
async def test_forward_still_calls_the_cli_for_a_text_request(isolated):
    router, calls, write, guard = isolated
    write(CLI_PROVIDER)

    result = await router.forward(_chat(ONE_CLI_ROUTE), route_guard=guard)

    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert len(calls["complete"]) == 1
    kind, body = calls["complete"][0]
    assert (kind, body["model"]) == ("claude", CLI_MODEL)


@pytest.mark.asyncio
async def test_forward_stream_refuses_a_json_schema_on_a_cli_route(isolated):
    router, calls, write, guard = isolated
    write(CLI_PROVIDER)
    body = _chat(ONE_CLI_ROUTE, stream=True, response_format=JSON_SCHEMA)

    with pytest.raises(router.UnsupportedOnCliError) as err:
        async for _ in router.forward_stream(body, route_guard=guard):
            pass

    assert err.value.provider_id == CLI_ID
    assert err.value.unsupported == ["response_format"]
    _nothing_held(calls)


def test_is_cli_provider(isolated):
    router = isolated[0]
    assert router.is_cli_provider(CLI_PROVIDER)
    assert not router.is_cli_provider(PROVIDER_A)
    assert not router.is_cli_provider({**PROVIDER_A, "auth_source": "cli:claude"})
    assert not router.is_cli_provider({**PROVIDER_A, "auth_source": None})


def test_cli_refusal_is_none_for_a_text_request(isolated):
    router, _, write, _ = isolated
    write(CLI_PROVIDER)
    assert router.cli_refusal(_chat(ONE_CLI_ROUTE)) is None


def test_cli_refusal_is_none_when_routing_fails(isolated):
    router, _, write, _ = isolated
    write()  # no active provider: routing raises ValueError
    assert router.cli_refusal(_chat(ONE_CLI_ROUTE, tools=TOOLS)) is None
    write(CLI_PROVIDER)
    assert router.cli_refusal(_chat(None, tools=TOOLS)) is None


def test_cli_refusal_is_none_when_a_non_cli_candidate_is_left(isolated):
    router, _, write, _ = isolated
    write(CLI_PROVIDER, PROVIDER_A)
    assert router.cli_refusal(_chat(ANY_ROUTE, tools=TOOLS)) is None


@pytest.mark.asyncio
async def test_cli_refusal_is_none_for_a_disabled_provider(isolated):
    """forward() answers ProviderDisabledError there: cli_refusal must not
    name another, active CLI provider in its place."""
    router, calls, write, _ = isolated
    off = {**CLI_PROVIDER, "id": "pbtest-off-cli", "is_active": False}
    on = {**OTHER_CLI, "id": "pbtest-on-cli"}
    write(off, on)
    body = _chat("pbtest-off-cli:m", tools=TOOLS)

    assert router.cli_refusal(body) is None
    with pytest.raises(router.ProviderDisabledError) as err:
        await router.forward(body)
    assert err.value.provider_id == "pbtest-off-cli"
    assert calls["complete"] == []


def test_cli_refusal_names_the_first_cli_candidate(isolated):
    router, calls, write, _ = isolated
    write(CLI_PROVIDER, OTHER_CLI)
    body = _chat(
        ANY_ROUTE,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": "data:,"}},
                ],
            }
        ],
    )

    refusal = router.cli_refusal(body)

    assert isinstance(refusal, router.UnsupportedOnCliError)
    assert refusal.provider_id == CLI_ID
    assert refusal.unsupported == ["content:image_url"]
    assert calls["complete"] == []
