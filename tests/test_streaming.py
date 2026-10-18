"""Tests for streaming (stream: true) support."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from tests.factories import FAKE_STREAM_BODY, FAKE_STREAM_CHUNK_1, FAKE_STREAM_CHUNK_2

AUTH = {"Authorization": "Bearer testkey"}
_MODEL = "test-provider:fake:model-v1"
_STREAM_BODY = {
    "model": _MODEL,
    "messages": [{"role": "user", "content": "Hi"}],
    "stream": True,
}


# ── API-level streaming tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_content_type(client):
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            status=200,
            body=FAKE_STREAM_BODY,
            content_type="text/event-stream",
        )
        resp = await client.post(
            "/v1/chat/completions", headers=AUTH, json=_STREAM_BODY
        )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_stream_chunks_passed_through(client):
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            status=200,
            body=FAKE_STREAM_BODY,
            content_type="text/event-stream",
        )
        resp = await client.post(
            "/v1/chat/completions", headers=AUTH, json=_STREAM_BODY
        )
    assert resp.status_code == 200
    assert FAKE_STREAM_CHUNK_1 in resp.content
    assert FAKE_STREAM_CHUNK_2 in resp.content


@pytest.mark.asyncio
async def test_stream_backend_error_returns_502(client):
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            status=400,
            body=b"bad request",
        )
        resp = await client.post(
            "/v1/chat/completions", headers=AUTH, json=_STREAM_BODY
        )
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_stream_requires_auth(client):
    resp = await client.post(
        "/v1/chat/completions",
        json=_STREAM_BODY,
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stream_wrong_key_returns_401(client):
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer wrong"},
        json=_STREAM_BODY,
    )
    assert resp.status_code == 401


# ── Router-level streaming test ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forward_stream_no_retry(_providers_file):
    """forward_stream must not retry — one 500 → immediate RuntimeError."""
    from plugboard.router import forward_stream

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            status=500,
            body=b"server error",
        )
        with pytest.raises(RuntimeError, match="HTTP 500"):
            async for _ in forward_stream(
                {"model": _MODEL, "messages": [], "stream": True}
            ):
                pass
