"""Unit tests for ApiSurfaceClient using httpx.MockTransport (no server).

Council C7 (2026-05-31): developer + codex-coder.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.live_model_tests.surfaces.api_client import (
    ApiSurfaceClient,
    ApiSurfaceError,
    MalformedResponseError,
)


def _override_skip(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", "0")


def _sse_body(*chunks: dict, done: bool = True) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _delta(content="", reasoning="", finish_reason=None, model="m") -> dict:
    delta: dict = {}
    if content:
        delta["content"] = content
    if reasoning:
        delta["reasoning"] = reasoning
    choice: dict = {"index": 0, "delta": delta}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"model": model, "choices": [choice]}


def _non_stream(content: str, model="m") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"content": content, "reasoning": ""}, "finish_reason": "stop"}],
        "billing": {"model": "routed-m", "pricing_month": "2026_05"},
    }


def _transport(status: int, body: bytes, headers: dict | None = None) -> httpx.MockTransport:
    hdrs = {"Content-Type": "application/json", "X-Request-ID": "req-001", **(headers or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=hdrs)

    return httpx.MockTransport(handler)


def _client(transport):
    return ApiSurfaceClient(api_key="lle_test", transport=transport)


# ── streaming ────────────────────────────────────────────────────────────────


def test_stream_pass_basic(monkeypatch):
    _override_skip(monkeypatch)
    body = _sse_body(_delta("hello "), _delta("world", finish_reason="stop"))
    r = _client(_transport(200, body)).chat("m", [{"role": "user", "content": "hi"}],
                                            stream=True, timeout=10, max_tokens=64, temperature=0.0)
    assert r["content"] == "hello world"
    assert r["done_received"] is True
    assert r["finish_reason"] == "stop"
    assert r["raw_sse_lines"] and all(
        line.startswith("data:") for line in r["raw_sse_lines"]
    )
    assert any("[DONE]" in line for line in r["raw_sse_lines"])
    assert len(r["raw_chunks"]) == 2
    assert r["model_echoed"] == "m"
    assert r["request_id"] == "req-001"
    assert r["billing"] is None  # Council C7 M-2


def test_stream_with_reasoning(monkeypatch):
    _override_skip(monkeypatch)
    body = _sse_body(_delta(reasoning="think..."), _delta("answer", finish_reason="stop"))
    r = _client(_transport(200, body)).chat("m", [{"role": "user", "content": "q"}],
                                            stream=True, timeout=10, max_tokens=64, temperature=0.0)
    assert "think..." in r["reasoning"]
    assert r["content"] == "answer"


def test_stream_done_received_sentinel(monkeypatch):
    _override_skip(monkeypatch)
    body = _sse_body(_delta("hi", finish_reason="stop"))
    r = _client(_transport(200, body)).chat("m", [{"role": "user", "content": "x"}],
                                            stream=True, timeout=10, max_tokens=64, temperature=0.0)
    done = [line for line in r["raw_sse_lines"] if "[DONE]" in line]
    assert done == ["data: [DONE]"]
    assert r["done_received"] is True


def test_stream_http_500_raises(monkeypatch):
    _override_skip(monkeypatch)
    with pytest.raises(ApiSurfaceError) as exc:
        _client(_transport(500, b"upstream error")).chat(
            "m", [{"role": "user", "content": "x"}], stream=True, timeout=10, max_tokens=64, temperature=0.0)
    assert exc.value.status_code == 500
    assert exc.value.error_type == "http_error:500"


def test_stream_real_protocols_isinstance(monkeypatch):
    _override_skip(monkeypatch)
    from tests.live_model_tests.protocols import (
        BillingAwareSurface, StreamingCapableSurface, SurfaceClient,
    )
    c = ApiSurfaceClient()
    assert isinstance(c, SurfaceClient)
    assert isinstance(c, StreamingCapableSurface)
    assert isinstance(c, BillingAwareSurface)
    assert c.supports_billing is True


# ── non-streaming (D6) ───────────────────────────────────────────────────────


def test_non_stream_billing_from_body_and_headers(monkeypatch):
    _override_skip(monkeypatch)
    headers = {"X-Cost-USD": "0.002", "X-Prompt-Tokens": "8", "X-Completion-Tokens": "2",
               "X-Provider": "openai", "X-Cache": "miss"}
    r = _client(_transport(200, json.dumps(_non_stream("pong")).encode(), headers)).chat(
        "m", [{"role": "user", "content": "ping"}], stream=False, timeout=10, max_tokens=32, temperature=0.0)
    assert r["content"] == "pong"
    assert r["raw_sse_lines"] is None
    b = r["billing"]
    assert b["cost_usd"] == pytest.approx(0.002)
    assert b["prompt_tokens"] == 8
    assert b["completion_tokens"] == 2
    assert b["provider"] == "openai"
    assert b["billed_model"] == "routed-m"     # from body billing.model (BLOCKER-1)
    assert b["pricing_month"] == "2026_05"
    assert b["cache_hit"] is False


def test_non_stream_http_500(monkeypatch):
    _override_skip(monkeypatch)
    with pytest.raises(ApiSurfaceError) as exc:
        _client(_transport(500, b'{"error":"boom"}')).chat(
            "m", [{"role": "user", "content": "x"}], stream=False, timeout=10, max_tokens=32, temperature=0.0)
    assert exc.value.status_code == 500


def test_non_stream_malformed_json(monkeypatch):
    _override_skip(monkeypatch)
    with pytest.raises(MalformedResponseError) as exc:
        _client(_transport(200, b"not json")).chat(
            "m", [{"role": "user", "content": "x"}], stream=False, timeout=10, max_tokens=32, temperature=0.0)
    assert exc.value.error_type == "malformed_response"


@pytest.mark.parametrize("cache_val,expected", [
    ("hit", True), ("Hit", True), ("semantic-hit", True), ("1", True), ("true", True),
    ("miss", False), ("", False),
])
def test_cache_hit_header_variants(cache_val, expected, monkeypatch):
    _override_skip(monkeypatch)
    r = _client(_transport(200, json.dumps(_non_stream("ok")).encode(), {"X-Cache": cache_val})).chat(
        "m", [{"role": "user", "content": "x"}], stream=False, timeout=10, max_tokens=32, temperature=0.0)
    assert r["billing"]["cache_hit"] is expected


# ── transport errors ─────────────────────────────────────────────────────────


def test_timeout_raises_timeout_error(monkeypatch):
    _override_skip(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TimeoutError):
        _client(httpx.MockTransport(handler)).chat(
            "m", [{"role": "user", "content": "x"}], stream=False, timeout=1, max_tokens=32, temperature=0.0)
