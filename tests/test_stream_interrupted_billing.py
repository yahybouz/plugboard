"""A stream cut short still records its usage and closes the upstream (BUG-2).

Before the fix, `_stream_and_bill` billed only after the upstream stream
ended normally: a client that disconnected, or an upstream read error,
left no usage row, so the call was free and the monthly cap was bypassed.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from tests.factories import FAKE_STREAM_CHUNK_1, FAKE_STREAM_CHUNK_2

_BODY = {
    "model": "test-provider:fake:model-v1",
    "messages": [{"role": "user", "content": "Hi"}],
    "stream": True,
}


@pytest.fixture
def usage_store(tmp_path, monkeypatch):
    import plugboard.usage as usage

    monkeypatch.setattr(usage, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    return usage


def _rows(usage):
    from plugboard.billing import current_pricing_month

    return usage.get_usage(current_pricing_month())


async def _run_until_disconnect(app, body: dict) -> list:
    """Drive the ASGI app like uvicorn (spec 2.3): the client leaves after the
    first body chunk, so Starlette cancels the response while the upstream
    is still running."""
    raw = json.dumps(body).encode()
    first_chunk_sent = asyncio.Event()
    sent: list = []
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await first_chunk_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer testkey"),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return sent


@pytest.mark.asyncio
async def test_client_disconnect_mid_stream_records_usage_and_closes_upstream(
    _providers_file, _api_key, usage_store, monkeypatch
):
    import plugboard.ratelimit as rl

    # Same setup as the `client` fixture, without its buffering transport.
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "testkey")
    monkeypatch.setattr(rl, "_LIMIT", 0)
    upstream_closed = asyncio.Event()
    never = asyncio.Event()

    async def slow_upstream(body, **_kwargs):
        try:
            yield FAKE_STREAM_CHUNK_1
            await never.wait()  # the client leaves while we wait here
            yield b"data: [DONE]\n\n"
        finally:
            upstream_closed.set()

    monkeypatch.setattr("plugboard.app.forward_stream", slow_upstream)
    from plugboard.app import app

    sent = await _run_until_disconnect(app, _BODY)

    assert any(
        m["type"] == "http.response.body" and FAKE_STREAM_CHUNK_1 in m.get("body", b"")
        for m in sent
    )
    await asyncio.wait_for(upstream_closed.wait(), timeout=5)
    rows = _rows(usage_store)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] > 0
    assert rows[0]["completion_tokens"] > 0  # "Hel" reached the client


@pytest.mark.asyncio
async def test_upstream_error_mid_stream_records_usage(
    client, usage_store, monkeypatch
):
    async def broken_upstream(body, **_kwargs):
        yield FAKE_STREAM_CHUNK_1
        yield FAKE_STREAM_CHUNK_2
        raise aiohttp.ClientPayloadError("connection reset mid-stream")

    monkeypatch.setattr("plugboard.app.forward_stream", broken_upstream)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json=_BODY,
    )

    # PLU-26: the stream ends with a readable error, not a bare cut.
    assert resp.status_code == 200
    assert b'"code": "stream_interrupted"' in resp.content
    assert resp.content.rstrip().endswith(b"data: [DONE]")
    rows = _rows(usage_store)
    assert len(rows) == 1
    assert rows[0]["completion_tokens"] > 0


@pytest.mark.asyncio
async def test_complete_stream_is_billed_once(client, usage_store, monkeypatch):
    async def normal_upstream(body, **_kwargs):
        yield FAKE_STREAM_CHUNK_1
        yield FAKE_STREAM_CHUNK_2
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr("plugboard.app.forward_stream", normal_upstream)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json=_BODY,
    )

    assert resp.status_code == 200
    assert resp.content.rstrip().endswith(b"data: [DONE]")
    assert len(_rows(usage_store)) == 1
