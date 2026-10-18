"""Unit tests for McpSurfaceClient — no real subprocess, no network.

Council C8 (2026-05-31): developer + codex-coder. A scriptable FakeTransport
replaces the subprocess.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from tests.live_model_tests.protocols import SurfaceClient
from tests.live_model_tests.surfaces.mcp_client import (
    McpSurfaceClient,
    McpToolError,
    McpTransportError,
    _extract_mcp_args,
    _strip_footer,
)


def _override_skip(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", "0")


class FakeTransport:
    """Scriptable JsonRpcTransport: recv() pops pre-canned responses."""

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.sent: List[Dict[str, Any]] = []
        self.closed = False

    def send(self, obj: Dict[str, Any]) -> None:
        self.sent.append(obj)

    def recv(self, deadline: Optional[float] = None) -> Dict[str, Any]:
        if not self._responses:
            raise McpTransportError("FakeTransport exhausted")
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _init_resp(req_id=1):
    return {"jsonrpc": "2.0", "id": req_id, "result": {"protocolVersion": "2024-11-05",
            "capabilities": {}, "serverInfo": {"name": "lle", "version": "3.0.0"}}}


def _tool_resp(req_id, text, is_error=False):
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error}}


def _client(text, *, is_error=False):
    fake = FakeTransport([_init_resp(1), _tool_resp(2, text, is_error)])
    return McpSurfaceClient(transport=fake), fake


MSGS = [{"role": "user", "content": "hello"}]


def _chat(client):
    return client.chat("m", MSGS, stream=True, timeout=10.0, max_tokens=256, temperature=0.0)


# ── footer + args helpers ────────────────────────────────────────────────────


def test_strip_footer_tokens_and_cost():
    clean, meta = _strip_footer("content\n\n---\n_(tokens: 42, cost: $0.0012)_")
    assert clean == "content"
    assert meta["tokens"] == "42" and meta["cost"] == "$0.0012"


def test_strip_footer_none():
    text = '{"echo": "ABC", "payload": "éàçü"}'
    clean, meta = _strip_footer(text)
    assert clean == text and meta is None


def test_strip_footer_does_not_eat_dashes_in_json():
    text = '{"note": "--- sep ---"}'
    clean, meta = _strip_footer(text)
    assert clean == text and meta is None


def test_extract_args_user_and_system():
    args = _extract_mcp_args([{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}])
    assert args["message"] == "q" and args["system_prompt"] == "sys"


def test_extract_args_last_user_wins():
    args = _extract_mcp_args([{"role": "user", "content": "first"},
                             {"role": "assistant", "content": "r"},
                             {"role": "user", "content": "second"}])
    assert args["message"] == "second"


def test_extract_args_no_user_raises():
    with pytest.raises(ValueError):
        _extract_mcp_args([{"role": "system", "content": "only sys"}])


# ── chat happy paths ─────────────────────────────────────────────────────────


def test_chat_plain_content(monkeypatch):
    _override_skip(monkeypatch)
    expected = '{"echo": "ABCD1234", "payload": "éàçü"}'
    client, _ = _client(expected)
    r = _chat(client)
    assert r["content"] == expected
    assert r["reasoning"] == "" and r["finish_reason"] == "stop"
    assert r["done_received"] is True
    assert r["raw_sse_lines"] is None  # D5 skip
    assert r["billing"] is None        # D6 skip


def test_chat_footer_stripped_gives_pure_json(monkeypatch):
    _override_skip(monkeypatch)
    body = '{"echo": "NONCE1", "payload": "hello"}'
    client, _ = _client(f"{body}\n\n---\n_(tokens: 42, cost: $0.0012)_")
    r = _chat(client)
    assert r["content"] == body
    assert json.loads(r["content"])["echo"] == "NONCE1"  # D2 parses cleanly


# ── error handling ───────────────────────────────────────────────────────────


def test_chat_error_prefix_returned_as_content(monkeypatch):
    """Council C8 M-2: a model literally answering 'Error: ...' is content, not a tool error."""
    _override_skip(monkeypatch)
    client, _ = _client("Error: this is the model's own answer")
    r = _chat(client)
    assert r["content"] == "Error: this is the model's own answer"


def test_chat_empty_sentinel_gives_empty_content(monkeypatch):
    _override_skip(monkeypatch)
    client, _ = _client("Error: Empty content in LLM response")
    r = _chat(client)
    assert r["content"] == ""
    assert r["finish_reason"] is None


def test_chat_is_error_flag_raises(monkeypatch):
    _override_skip(monkeypatch)
    client, _ = _client("boom", is_error=True)
    with pytest.raises(McpToolError):
        _chat(client)


def test_chat_jsonrpc_error_raises(monkeypatch):
    _override_skip(monkeypatch)
    fake = FakeTransport([_init_resp(1),
                          {"jsonrpc": "2.0", "id": 2, "error": {"code": -32601, "message": "no tool"}}])
    client = McpSurfaceClient(transport=fake)
    with pytest.raises(McpToolError):
        _chat(client)


def test_chat_timeout_propagates(monkeypatch):
    _override_skip(monkeypatch)

    class TimeoutTransport:
        def send(self, obj): pass
        def recv(self, deadline=None): raise TimeoutError("t")
        def close(self): pass

    client = McpSurfaceClient(transport=TimeoutTransport())
    client._handshake_done = True
    with pytest.raises(TimeoutError):
        client.chat("m", MSGS, stream=True, timeout=0.01, max_tokens=64, temperature=0.0)


# ── protocol + handshake ─────────────────────────────────────────────────────


def test_satisfies_surface_client_protocol(monkeypatch):
    _override_skip(monkeypatch)
    client = McpSurfaceClient(transport=FakeTransport([]))
    assert isinstance(client, SurfaceClient)


def test_supports_billing_false_value(monkeypatch):
    """D6 discriminates on the VALUE (getattr), not isinstance — must be False."""
    _override_skip(monkeypatch)
    client = McpSurfaceClient(transport=FakeTransport([]))
    assert client.supports_billing is False


def test_handshake_sent_once_across_calls(monkeypatch):
    _override_skip(monkeypatch)
    fake = FakeTransport([_init_resp(1), _tool_resp(2, '{"c": 1}'), _tool_resp(3, '{"c": 2}')])
    client = McpSurfaceClient(transport=fake)
    r1 = _chat(client)
    r2 = _chat(client)
    assert r1["content"] == '{"c": 1}' and r2["content"] == '{"c": 2}'
    inits = [m for m in fake.sent if m.get("method") == "initialize"]
    assert len(inits) == 1


def test_handshake_skips_notification_before_result(monkeypatch):
    _override_skip(monkeypatch)
    notif = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"p": 50}}
    fake = FakeTransport([notif, _init_resp(1), _tool_resp(2, '{"ok": 1}')])
    client = McpSurfaceClient(transport=fake)
    assert _chat(client)["content"] == '{"ok": 1}'


def test_tools_call_arguments_shape(monkeypatch):
    _override_skip(monkeypatch)
    fake = FakeTransport([_init_resp(1), _tool_resp(2, "ok")])
    client = McpSurfaceClient(transport=fake)
    client.chat("my-model", [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
                stream=True, timeout=5.0, max_tokens=128, temperature=0.0)
    call = [m for m in fake.sent if m.get("method") == "tools/call"][0]
    args = call["params"]["arguments"]
    assert call["params"]["name"] == "chat"
    assert args["model"] == "my-model" and args["message"] == "U"
    assert args["system_prompt"] == "S" and args["max_tokens"] == 128
