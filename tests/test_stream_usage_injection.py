"""Tests for BUG-3: Playground didn't show tokens/cost for upstreams
that don't honour OpenAI's stream_options.include_usage (Ollama, older
OpenClaw, any custom gateway).

The router now injects a trailing usage chunk when the upstream
didn't provide one, so every model's stream carries a final
`data: {... "usage": {...}}` before `data: [DONE]`.
"""

from __future__ import annotations

import json
from typing import List

import pytest


def _extract_usage(sse_text: str) -> dict:
    """Scan SSE for the first chunk that carries a usage block."""
    for line in sse_text.split("\n"):
        line = line.strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if payload.get("usage"):
            return payload["usage"]
    return {}


@pytest.mark.asyncio
async def test_stream_injects_usage_chunk_when_upstream_omits_it(client, monkeypatch):
    """Upstream emits text chunks but NO usage — we must inject one."""
    # Upstream that mimics Ollama-style streaming: chunks without usage.
    sse_chunks = [
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" world"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    from plugboard import app as app_mod
    from plugboard.config import create_provider

    create_provider(
        name="Bug3 Provider",
        base_url="https://bug3.example.com",
        bearer_token="tok",
        default_model="b3-model",
    )

    async def fake_forward_stream(body):
        for ch in sse_chunks:
            yield ch

    monkeypatch.setattr(app_mod, "forward_stream", fake_forward_stream)

    # Swap the resolver so the fake model maps to our provider cleanly.
    def fake_resolve(model_str):
        from plugboard.config import get_provider

        return get_provider("bug3-provider"), "b3-model"

    monkeypatch.setattr(app_mod, "resolve_provider_and_model", fake_resolve)

    res = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={
            "model": "bug3-provider:b3-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert res.status_code == 200
    body = res.text

    usage = _extract_usage(body)
    assert usage, f"no usage chunk found in stream. body tail: {body[-400:]}"
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert "cost_usd" in usage


@pytest.mark.asyncio
async def test_stream_does_not_duplicate_usage_when_upstream_already_sent_it(
    client, monkeypatch
):
    """Upstream already emits OpenAI-style usage on the last chunk — we
    must NOT emit a duplicate."""
    upstream_usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    sse_chunks: List[bytes] = [
        b'data: {"id":"c2","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n',
        f"data: {json.dumps({'id': 'c2', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': upstream_usage})}\n\n".encode(),
        b"data: [DONE]\n\n",
    ]

    from plugboard import app as app_mod
    from plugboard.config import create_provider

    create_provider(
        name="Bug3 Upstream OK",
        base_url="https://bug3ok.example.com",
        bearer_token="tok",
        default_model="ok-model",
    )

    async def fake_forward_stream(body):
        for ch in sse_chunks:
            yield ch

    monkeypatch.setattr(app_mod, "forward_stream", fake_forward_stream)

    def fake_resolve(model_str):
        from plugboard.config import get_provider

        return get_provider("bug3-upstream-ok"), "ok-model"

    monkeypatch.setattr(app_mod, "resolve_provider_and_model", fake_resolve)

    res = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={
            "model": "bug3-upstream-ok:ok-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    body = res.text

    # Count usage blocks — must be exactly 1, with upstream's values
    usage_count = 0
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            try:
                payload = json.loads(line[6:])
                if payload.get("usage"):
                    usage_count += 1
            except json.JSONDecodeError:
                pass
    assert usage_count == 1, (
        f"expected exactly 1 usage chunk, got {usage_count}. body={body[-500:]}"
    )


def _usage_index_and_done_index(sse_text: str) -> tuple:
    """Positions of the first usage event and of `data: [DONE]` in the stream."""
    usage_at = done_at = None
    for i, line in enumerate(raw.strip() for raw in sse_text.split("\n")):
        if line == "data: [DONE]":
            done_at = i if done_at is None else done_at
        elif line.startswith("data: ") and usage_at is None:
            try:
                if json.loads(line[6:]).get("usage"):
                    usage_at = i
            except json.JSONDecodeError:
                pass
    return usage_at, done_at


@pytest.mark.parametrize(
    "chunks",
    [
        # A short answer often reaches the gateway as one network read.
        [
            b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
            b"data: [DONE]\n\n"
        ],
        [
            b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"}}]}\n\n',
            b'data: {"id":"c1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" world"}}]}\n\n',
            b"data: [DONE]\n\n",
        ],
    ],
    ids=["single-read", "several-reads"],
)
@pytest.mark.asyncio
async def test_injected_usage_comes_before_done(client, monkeypatch, chunks):
    """OpenAI clients stop reading at `data: [DONE]`: usage sent after it is lost."""
    from plugboard import app as app_mod
    from plugboard.config import create_provider, get_provider

    create_provider(
        name="Done Order",
        base_url="https://done-order.example.com",
        bearer_token="tok",
        default_model="do-model",
    )

    async def fake_forward_stream(body):
        for ch in chunks:
            yield ch

    monkeypatch.setattr(app_mod, "forward_stream", fake_forward_stream)
    monkeypatch.setattr(
        app_mod,
        "resolve_provider_and_model",
        lambda _m: (get_provider("done-order"), "do-model"),
    )

    res = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={
            "model": "done-order:do-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert res.status_code == 200
    usage_at, done_at = _usage_index_and_done_index(res.text)
    assert usage_at is not None, res.text[-400:]
    assert done_at is not None, res.text[-400:]
    assert usage_at < done_at, f"usage after [DONE]:\n{res.text[-400:]}"
    assert res.text.count("data: [DONE]") == 1
