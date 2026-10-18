"""Unit tests for D5 Streaming integrity — pure logic, no LLM calls.

Council C5 (2026-05-31): claude-security-officer + codex-security-officer-critic
+ gemini-security-reviewer.
"""

from __future__ import annotations

import json
import re


from tests.live_model_tests.dimensions.d5_streaming import (
    _verdict_from_stream,
    run_d5_streaming_integrity,
)


def _override_skip(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", "0")


def _delta_line(content, finish_reason=None):
    return "data: " + json.dumps(
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": content},
                    "finish_reason": finish_reason,
                }
            ]
        }
    )


def _final_line(finish_reason="stop"):
    return "data: " + json.dumps(
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
    )


def _good_stream(finish_reason="stop"):
    lines = [_delta_line(f"{i}\n") for i in range(1, 11)]
    lines.append(_final_line(finish_reason))
    lines.append("data: [DONE]")
    return lines


def _unified(raw_sse_lines, *, content=None, reasoning="", finish_reason="stop"):
    if content is None:
        content = "".join(str(i) + "\n" for i in range(1, 11))
    return {
        "raw_sse_lines": raw_sse_lines,
        "content": content,
        "reasoning": reasoning,
        "finish_reason": finish_reason,
        "done_received": raw_sse_lines is not None and "data: [DONE]" in raw_sse_lines,
        "first_token_latency_ms": 8.0,
    }


def _verdict(unified, *, nonce="DEADBEEF12345678", is_thinking=False):
    return _verdict_from_stream(
        model_id="m",
        surface="api",
        nonce=nonce,
        unified=unified,
        is_thinking=is_thinking,
        total_latency_ms=100.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )


# ── pure verdict: structural happy & sad paths ──────────────────────────────


def test_pass_nominal():
    r = _verdict(_unified(_good_stream()))
    assert r.passed is True
    assert r.error_type is None
    assert r.done_received is True
    assert r.finish_reason == "stop"
    assert r.chunk_count == 11  # 10 deltas + 1 final


def test_skip_when_raw_sse_lines_none():
    r = _verdict(_unified(None))
    assert r.passed is False
    assert r.error_type == "skipped:surface_non_streaming"
    assert r.excluded_from_threshold is True


def test_empty_response():
    r = _verdict(_unified([]))
    assert r.passed is False
    assert r.error_type == "empty_response"


def test_bad_prefix():
    lines = _good_stream()
    lines.insert(0, "event: message")  # not a data: line, not a comment
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "sse_malformed:bad_prefix"


def test_json_parse_error():
    lines = ["data: {not valid json", _final_line(), "data: [DONE]"]
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "sse_malformed:json_parse_error"


def test_missing_choices():
    lines = ['data: {"foo": "bar"}', _final_line(), "data: [DONE]"]
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "sse_malformed:missing_choices"


def test_missing_delta():
    lines = [
        'data: {"choices": [{"index": 0, "finish_reason": null}]}',
        _final_line(),
        "data: [DONE]",
    ]
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "sse_malformed:missing_delta"


def test_done_missing():
    lines = [_delta_line("1\n"), _final_line()]
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "done_missing"


def test_done_duplicated():
    lines = [_delta_line("1\n"), _final_line(), "data: [DONE]", "data: [DONE]"]
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "done_duplicated"


def test_done_not_terminal():
    lines = [_delta_line("1\n"), "data: [DONE]", _final_line()]
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "done_not_terminal"


def test_bad_finish_reason():
    lines = [_delta_line("1\n"), _final_line("content_filter"), "data: [DONE]"]
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "bad_finish_reason"


def test_finish_reason_missing():
    lines = [_delta_line("1\n"), _delta_line("2\n"), "data: [DONE]"]
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "finish_reason_missing"


def test_chunk_duplicated():
    dup = _delta_line("5\n")
    lines = [_delta_line("1\n"), dup, dup, _final_line(), "data: [DONE]"]
    r = _verdict(_unified(lines))
    assert r.passed is False
    assert r.error_type == "chunk_duplicated"


def test_length_truncation_passes_with_flag():
    r = _verdict(_unified(_good_stream("length"), finish_reason="length"))
    assert r.passed is True
    assert r.truncated is True
    assert r.extra.get("truncated") is True


def test_reasoning_leak_in_content_thinking():
    nonce = "CAFEBABE00112233"
    content = f"{nonce}\n1\n2\n3"  # nonce leaked into the visible output
    r = _verdict(
        _unified(_good_stream(), content=content), nonce=nonce, is_thinking=True
    )
    assert r.passed is False
    assert r.error_type == "reasoning_leak_in_content"
    assert r.extra.get("leaked_nonce") == nonce


def test_non_thinking_nonce_echo_is_not_a_leak():
    """Council C5 MAJOR-1: I7 fires only for thinking models. A non-thinking
    model echoing the nonce is not a reasoning leak."""
    nonce = "CAFEBABE00112233"
    content = f"{nonce}\n1\n2\n3"
    r = _verdict(
        _unified(_good_stream(), content=content), nonce=nonce, is_thinking=False
    )
    assert r.passed is True


def test_usage_chunk_tolerated():
    lines = [
        _delta_line("1\n"),
        _final_line(),
        'data: {"usage": {"total_tokens": 5}}',
        "data: [DONE]",
    ]
    r = _verdict(_unified(lines))
    assert r.passed is True


def test_heartbeats_after_done_tolerated():
    lines = _good_stream() + [": keepalive", "data: ", "  "]
    r = _verdict(_unified(lines))
    assert r.passed is True


def test_comment_lines_tolerated_anywhere():
    lines = [
        ": warming up",
        _delta_line("1\n"),
        ": tick",
        _final_line(),
        "data: [DONE]",
    ]
    r = _verdict(_unified(lines))
    assert r.passed is True


# ── run_d5_streaming_integrity orchestration ─────────────────────────────────


class _FakeStreamingSurface:
    def __init__(self, responses, *, name="api"):
        self.name = name
        self.responses = list(responses)
        self.calls = []

    def chat(self, *, model, messages, stream, timeout, max_tokens, temperature):
        self.calls.append({"model": model, "max_tokens": max_tokens, "stream": stream})
        text = messages[0]["content"]
        m = re.search(r"\[([0-9A-F]+)\]", text)
        nonce = m.group(1) if m else ""
        return self.responses.pop(0)(nonce)


def test_quarantine_short_circuits(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeStreamingSurface([])
    r = run_d5_streaming_integrity(
        model_id="bad",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "surfaces": ["api"]},
        quarantine_ids={"bad"},
    )
    assert r.error_type == "skipped:quarantined"
    assert client.calls == []


def test_surface_unsupported_skips(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeStreamingSurface([])
    r = run_d5_streaming_integrity(
        model_id="m",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "surfaces": ["mcp"]},
    )
    assert r.error_type == "skipped:surface_unsupported"
    assert client.calls == []


def test_pass_first_attempt(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeStreamingSurface([lambda n: _unified(_good_stream())])
    r = run_d5_streaming_integrity(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "surfaces": ["api"]},
    )
    assert r.passed is True
    assert r.attempts == 1
    assert client.calls[0]["stream"] is True


def test_no_retry_on_structural_fail(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeStreamingSurface(
        [lambda n: _unified(["data: {not json", "data: [DONE]"], finish_reason="stop")]
    )
    r = run_d5_streaming_integrity(
        model_id="m",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "surfaces": ["api"]},
    )
    assert r.passed is False
    assert r.error_type == "sse_malformed:json_parse_error"
    assert r.attempts == 1
    assert len(client.calls) == 1


def test_skip_non_streaming_no_retry(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeStreamingSurface([lambda n: _unified(None)])
    r = run_d5_streaming_integrity(
        model_id="m",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "surfaces": ["api"]},
    )
    assert r.error_type == "skipped:surface_non_streaming"
    assert len(client.calls) == 1


def test_max_tokens_thinking(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeStreamingSurface([lambda n: _unified(_good_stream())])
    run_d5_streaming_integrity(
        model_id="kimi-k2.6:cloud",
        surface_client=client,
        catalog_entry={"timeout_s": 180, "is_thinking": True, "surfaces": ["api"]},
    )
    assert client.calls[0]["max_tokens"] == 2048


def test_max_tokens_non_thinking(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeStreamingSurface([lambda n: _unified(_good_stream())])
    run_d5_streaming_integrity(
        model_id="claude-haiku-4-5",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False, "surfaces": ["api"]},
    )
    assert client.calls[0]["max_tokens"] == 64
