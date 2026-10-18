"""Unit tests for D1 Liveness — pure logic, no LLM calls.

The live tests themselves (skipped unless ``PLUGBOARD_RUN_LIVE_TESTS=1``)
live elsewhere; these unit tests run on every ``pytest tests/`` so the
spec stays locked.
"""

from __future__ import annotations

import pytest

from tests.live_model_tests.dimensions.d1_liveness import (
    _verdict_from_response,
    run_d1_liveness,
)


def _override_skip(monkeypatch):
    """Force-load the live-test module despite the auto-skip in conftest.

    The unit tests below test pure logic (no network) so they're safe to
    run unconditionally.
    """
    monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", "0")


# ── _verdict_from_response: pure-logic happy & sad paths ────────────────────


class _FakeClock:
    def __init__(self, sequence):
        self.sequence = list(sequence)

    def __call__(self):
        return self.sequence.pop(0)


def _ok_response(nonce: str, *, content_extra: str = "") -> dict:
    return {
        "content": f"ALIVE:{nonce}{content_extra}",
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
        "first_token_latency_ms": 12.0,
        "raw_chunks": [],
    }


def test_pass_when_nonce_echoed():
    r = _verdict_from_response(
        model_id="claude-opus-4-7",
        surface="api",
        nonce="ABC123",
        response=_ok_response("ABC123"),
        is_thinking=False,
        total_latency_ms=120.0,
        time_to_finish_reason_ms=120.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is True
    assert r.error_type is None
    assert r.nonce_echoed is True


def test_fail_nonce_missing_is_cache_or_fallback_suspect():
    """Codex BLOCKER #2 — generic content without nonce = cache hit, NOT alive."""
    resp = {
        "content": "Hello! I'm an AI assistant. How can I help you today?",
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
        "first_token_latency_ms": 10.0,
    }
    r = _verdict_from_response(
        model_id="kimi-k2.6:cloud",
        surface="api",
        nonce="XYZ789",
        response=resp,
        is_thinking=True,
        total_latency_ms=300.0,
        time_to_finish_reason_ms=300.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "nonce_missing"
    assert r.nonce_echoed is False


def test_fail_alive_internal_only_for_thinking_with_empty_content():
    """Codex BLOCKER #3 — reasoning produced but no content = user-invisible."""
    resp = {
        "content": "",
        "reasoning": "Let me think about this... The user asks for a token...",
        "finish_reason": "length",
        "done_received": True,
        "first_token_latency_ms": None,
    }
    r = _verdict_from_response(
        model_id="qwen3.5:397b-cloud",
        surface="api",
        nonce="XYZ789",
        response=resp,
        is_thinking=True,
        total_latency_ms=50000.0,
        time_to_finish_reason_ms=50000.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "alive_internal_only"
    assert r.extra.get("is_thinking") is True


def test_fail_content_filtered():
    resp = {
        "content": "",
        "reasoning": "",
        "finish_reason": "content_filter",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="claude-opus-4-7",
        surface="api",
        nonce="ABC",
        response=resp,
        is_thinking=False,
        total_latency_ms=100.0,
        time_to_finish_reason_ms=100.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "content_filtered"


def test_fail_refusal_detected_over_nonce_missing():
    """Refusal classification takes precedence over plain nonce_missing."""
    resp = {
        "content": "I cannot complete that request.",
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="claude-opus-4-7",
        surface="api",
        nonce="ABC",
        response=resp,
        is_thinking=False,
        total_latency_ms=100.0,
        time_to_finish_reason_ms=100.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "refusal"


def test_fail_stream_truncated_without_done():
    resp = {
        "content": "",
        "reasoning": "",
        "finish_reason": None,
        "done_received": False,
    }
    r = _verdict_from_response(
        model_id="claude-opus-4-7",
        surface="api",
        nonce="ABC",
        response=resp,
        is_thinking=False,
        total_latency_ms=2000.0,
        time_to_finish_reason_ms=2000.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "stream_truncated"


# ── run_d1_liveness: orchestration with a fake surface client ───────────────


class _FakeSurfaceClient:
    """Test double: pre-canned chat() responses keyed by call order."""

    def __init__(self, responses):
        self.name = "api"
        self.responses = list(responses)
        self.calls = []

    def chat(self, *, model, messages, stream, timeout, max_tokens, temperature):
        # Capture the nonce the orchestrator generated so the response can
        # echo it back when needed.
        self.calls.append({"model": model, "messages": messages})
        nonce = ""
        text = messages[0]["content"] if messages else ""
        if "ALIVE:" in text:
            nonce = text.split("ALIVE:", 1)[1].strip()
        return self.responses.pop(0)(nonce)


def test_quarantine_short_circuits(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient(responses=[])
    r = run_d1_liveness(
        model_id="bad-model",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False},
        quarantine_ids={"bad-model"},
    )
    assert r.passed is False
    assert r.error_type == "skipped:quarantined"
    assert client.calls == []


def test_first_attempt_passes_no_retry(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient(responses=[lambda n: _ok_response(n)])
    r = run_d1_liveness(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert r.passed is True
    assert r.attempts == 1
    assert len(client.calls) == 1


def test_retry_recovers_from_nonce_missing(monkeypatch):
    """First attempt returns cached fallback → retry recovers."""
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient(
        responses=[
            lambda n: {
                "content": "Hello, how can I help?",
                "reasoning": "",
                "finish_reason": "stop",
                "done_received": True,
            },
            lambda n: _ok_response(n),
        ]
    )
    r = run_d1_liveness(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert r.passed is True
    assert r.attempts == 2
    assert r.first_attempt_latency_ms is not None
    assert len(client.calls) == 2


def test_refusal_no_retry(monkeypatch):
    """Refusal is a hard fail — no retry, return immediately."""
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient(
        responses=[
            lambda n: {
                "content": "I cannot do that.",
                "reasoning": "",
                "finish_reason": "stop",
                "done_received": True,
            }
        ]
    )
    r = run_d1_liveness(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert r.passed is False
    assert r.error_type == "refusal"
    assert r.attempts == 1
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "surface,expected_mult",
    [("api", 1.0), ("mcp", 1.5), ("ui", 2.0), ("unknown", 1.0)],
)
def test_timeout_multipliers(surface, expected_mult, monkeypatch):
    """Catalog timeout is multiplied by surface factor before being sent."""
    _override_skip(monkeypatch)
    captured_timeout = []

    class _CaptureClient:
        name = surface

        def chat(self, *, timeout, **kw):
            captured_timeout.append(timeout)
            nonce = kw["messages"][0]["content"].split("ALIVE:")[1].strip()
            return _ok_response(nonce)

    run_d1_liveness(
        model_id="m",
        surface_client=_CaptureClient(),
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert captured_timeout == [60 * expected_mult]
