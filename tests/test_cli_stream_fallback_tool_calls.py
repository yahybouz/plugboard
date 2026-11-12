"""A streamed request that falls back from a CLI route keeps its tool calls.

The chat route answers a streamed request whose first route is a CLI
subscription (``auth_source`` ``subprocess:<cli>``) by calling ``forward()``
without streaming and replaying the complete answer as SSE. When the request
carries what a CLI cannot serve (``tools``), ``forward()`` drops the CLI and
an HTTP provider answers with ``message.tool_calls``. The replay used to send
the ``content`` only and always end with ``finish_reason: "stop"``: the
client got a 200 stream with no tool call. The replay (live, and from the
response cache) now sends the tool calls in the OpenAI streaming shape and
ends with the answer's own ``finish_reason``. A plain-text CLI answer is
replayed byte for byte as before.

No CLI runs: ``cli_subprocess.complete`` is replaced by a counter. The HTTP
provider is a real server on 127.0.0.1 that records the body it received.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web

from tests.factories import FAKE_COMPLETION, PROVIDER_A

CLI_ID = "pbtest-codex-cli"
CLI_PROVIDER = {
    "id": CLI_ID,
    "name": "Codex CLI test",
    "base_url": "subprocess://codex",
    "bearer_token": "(via CLI)",
    "default_model": "pbtest-codex-model",
    "auth_source": "subprocess:codex",
    "provider_type": "cloud",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
# Neither provider lists its models: both are candidates for any name.
HTTP_BASE = {k: v for k, v in PROVIDER_A.items() if k != "models"}
MODEL = "pbtest-any-model"

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
TOOL_CALLS = [
    {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
    },
    {
        "id": "call_2",
        "type": "function",
        "function": {"name": "get_time", "arguments": "{}"},
    },
]


def _chat(**extra) -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "weather?"}],
        "stream": True,
        **extra,
    }


def _events(text: str) -> list:
    """The ``data:`` payloads of an SSE body, ``[DONE]`` kept as a string."""
    out = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


def _reassemble(events: list) -> tuple[str, list, str | None]:
    """Content, tool calls (merged by index) and final finish_reason."""
    content = ""
    calls: dict[int, dict] = {}
    finish = None
    for evt in events:
        if evt == "[DONE]":
            continue
        choice = evt["choices"][0]
        delta = choice.get("delta") or {}
        content += delta.get("content") or ""
        for tc in delta.get("tool_calls") or []:
            slot = calls.setdefault(
                tc["index"],
                {"id": None, "type": None, "function": {"name": "", "arguments": ""}},
            )
            slot["id"] = tc.get("id") or slot["id"]
            slot["type"] = tc.get("type") or slot["type"]
            fn = tc.get("function") or {}
            slot["function"]["name"] += fn.get("name") or ""
            slot["function"]["arguments"] += fn.get("arguments") or ""
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
    return content, [calls[i] for i in sorted(calls)], finish


@pytest.fixture
def isolated(monkeypatch, tmp_path, _providers_file):
    """Fresh breaker, alerts, metrics, cache and usage; a counter for the CLI."""
    import plugboard.alerts as alerts
    import plugboard.cache as response_cache
    import plugboard.metrics as metrics
    import plugboard.model_quarantine as quarantine
    import plugboard.router as router
    import plugboard.usage as usage
    from plugboard import circuit_breaker as cb
    from plugboard import cli_subprocess, provider_admission

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    # Equal load-balancing scores keep the candidates in providers.json order.
    monkeypatch.setattr(metrics, "_stats", {})
    monkeypatch.setattr(response_cache, "_DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    qfile = tmp_path / "quarantined_models.json"
    monkeypatch.setattr(quarantine, "_QUARANTINE_FILE", qfile)
    for fn in (
        quarantine.record_failure,
        quarantine.record_account_unsupported,
        quarantine.record_terminal_unavailable,
    ):
        monkeypatch.setitem(fn.__kwdefaults__, "path", qfile)
    provider_admission.reset()

    completed: list = []
    answer = json.loads(json.dumps(FAKE_COMPLETION))
    answer["choices"][0]["message"]["content"] = "Hello there, dear friend!"

    async def fake_complete(kind, body, *, timeout_s=None):
        completed.append((kind, body))
        return json.loads(json.dumps(answer))

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)

    def write(*providers: dict) -> None:
        _providers_file.write_text(json.dumps({"providers": list(providers)}))
        router.invalidate_models_cache()

    write(CLI_PROVIDER)
    yield completed, write
    provider_admission.reset()


@pytest.fixture
async def http_provider():
    """An HTTP provider on 127.0.0.1 that answers two tool calls."""
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
        yield {**HTTP_BASE, "base_url": f"http://127.0.0.1:{site.port}"}, received
    finally:
        await runner.cleanup()


def _assert_tool_call_stream(resp) -> None:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _events(resp.text)
    assert events[-1] == "[DONE]"
    content, calls, finish = _reassemble(events)
    assert calls == TOOL_CALLS
    assert finish == "tool_calls"
    assert content == ""
    # No empty content delta: the answer had no text.
    for evt in events[:-1]:
        assert "content" not in evt["choices"][0]["delta"], evt
    # One delta carries the calls, each with its position as index.
    (with_calls,) = [e for e in events[:-1] if e["choices"][0]["delta"]]
    assert [tc["index"] for tc in with_calls["choices"][0]["delta"]["tool_calls"]] == [
        0,
        1,
    ]


@pytest.mark.asyncio
async def test_a_streamed_fallback_from_a_cli_keeps_the_tool_calls(
    client, isolated, http_provider
):
    completed, write = isolated
    http, received = http_provider
    write(CLI_PROVIDER, http)

    resp = await client.post(
        "/v1/chat/completions",
        headers=ADMIN,
        json=_chat(tools=TOOLS, cache=False),
    )

    _assert_tool_call_stream(resp)
    assert len(received) == 1
    assert received[0]["tools"] == TOOLS
    assert completed == []


@pytest.mark.asyncio
async def test_the_cache_replay_keeps_the_tool_calls(client, isolated, http_provider):
    completed, write = isolated
    http, received = http_provider
    write(CLI_PROVIDER, http)

    first = await client.post(
        "/v1/chat/completions", headers=ADMIN, json=_chat(tools=TOOLS)
    )
    second = await client.post(
        "/v1/chat/completions", headers=ADMIN, json=_chat(tools=TOOLS)
    )

    _assert_tool_call_stream(first)
    assert first.headers["x-cache"] == "MISS"
    _assert_tool_call_stream(second)
    assert second.headers["x-cache"] == "HIT"
    # The second answer came from the cache, not from a provider.
    assert len(received) == 1
    assert completed == []


def _plain_text_stream(completion_id: str, usage: dict) -> str:
    """The exact SSE body the bypass sent for a plain-text CLI answer."""
    head = (
        f'"id": "{completion_id}", "object": "chat.completion.chunk", '
        '"created": 1774000000, "model": "fake:model-v1"'
    )
    chunks = [
        f'data: {{{head}, "choices": [{{"index": 0, "delta": '
        f'{{"content": "{tok}"}}, "finish_reason": null}}]}}\n\n'
        for tok in ("Hello ", "there, ", "dear ", "friend!")
    ]
    chunks.append(
        f'data: {{{head}, "choices": [{{"index": 0, "delta": {{}}, '
        f'"finish_reason": "stop"}}], "usage": {json.dumps(usage)}}}\n\n'
    )
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", [False, True], ids=["live", "from-cache"])
async def test_a_plain_text_cli_answer_is_replayed_as_before(client, isolated, cache):
    completed, _ = isolated
    body = _chat() if cache else _chat(cache=False)

    resp = await client.post("/v1/chat/completions", headers=ADMIN, json=body)
    if cache:
        assert resp.headers["x-cache"] == "MISS"
        resp = await client.post("/v1/chat/completions", headers=ADMIN, json=body)
        assert resp.headers["x-cache"] == "HIT"

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    usage = {
        "prompt_tokens": 5,
        "completion_tokens": 2,
        "total_tokens": 7,
        "cost_usd": 0.0,
    }
    assert resp.text == _plain_text_stream("chatcmpl-test", usage)
    assert len(completed) == 1
