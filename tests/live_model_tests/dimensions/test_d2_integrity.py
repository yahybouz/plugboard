"""Unit tests for D2 Integrity — pure logic, no LLM calls.

Council C4 (2026-05-31): claude-qa-tester (proposer) + codex-qa-tester-critic.
These run on every ``pytest tests/`` so the spec stays locked. The live tests
themselves are skipped unless ``PLUGBOARD_RUN_LIVE_TESTS=1`` (conftest).
"""

from __future__ import annotations

import re
import unicodedata

import pytest

from tests.live_model_tests.dimensions.d2_integrity import (
    _UTF8_PAYLOAD,
    _detect_utf8_corruption,
    _verdict_from_response,
    run_d2_integrity,
)

# NFC form of the payload (collapses the trailing NFD "é" → "é").
_UTF8_PAYLOAD_NFC = unicodedata.normalize("NFC", _UTF8_PAYLOAD)
# Classic mojibake: UTF-8 bytes decoded as latin-1 (covers Latin + CJK + emoji).
_UTF8_PAYLOAD_MOJIBAKE = _UTF8_PAYLOAD.encode("utf-8").decode("latin-1")


def _override_skip(monkeypatch):
    """Pure-logic tests are network-free, safe to run unconditionally."""
    monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", "0")


def _ok_response(nonce: str, *, payload: str = _UTF8_PAYLOAD) -> dict:
    import json

    return {
        "content": json.dumps({"echo": nonce, "payload": payload}, ensure_ascii=False),
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
        "first_token_latency_ms": 12.0,
    }


# ── _detect_utf8_corruption: the BLOCKER-1 / BLOCKER-2 core ──────────────────


def test_detect_exact_match_accepted():
    accepted, err, extra = _detect_utf8_corruption(_UTF8_PAYLOAD, _UTF8_PAYLOAD)
    assert accepted is True
    assert err is None
    assert extra == {}


def test_detect_unknown_difference_is_explicit_mismatch():
    """Council C4 BLOCKER-1: a wrong payload that is NOT mojibake/NFC/replacement
    must FAIL explicitly, never be silently accepted."""
    accepted, err, extra = _detect_utf8_corruption("completely_wrong", _UTF8_PAYLOAD)
    assert accepted is False
    assert err == "payload_mismatch"


def test_detect_mojibake_covers_cjk_and_emoji():
    """Council C4 BLOCKER-2: round-trip detection catches 3/4-byte corruption
    (em-dash, 日本語, 🎯), not just 2-byte Latin."""
    accepted, err, extra = _detect_utf8_corruption(_UTF8_PAYLOAD_MOJIBAKE, _UTF8_PAYLOAD)
    assert accepted is False
    assert err == "utf8_corrupted:mojibake"


def test_detect_replacement_char():
    accepted, err, extra = _detect_utf8_corruption("éàçü — �", _UTF8_PAYLOAD)
    assert accepted is False
    assert err == "utf8_corrupted:replacement_char"


def test_detect_nfc_is_accepted_as_informative():
    accepted, err, extra = _detect_utf8_corruption(_UTF8_PAYLOAD_NFC, _UTF8_PAYLOAD)
    assert accepted is True
    assert err is None
    assert extra.get("informative") == "utf8_corrupted:nfc_mismatch"


# ── _verdict_from_response: happy & sad paths ────────────────────────────────


def test_pass_nominal():
    r = _verdict_from_response(
        model_id="claude-opus-4-7",
        surface="api",
        nonce="A3F9C2D1E0B7A6F5",
        response=_ok_response("A3F9C2D1E0B7A6F5"),
        is_thinking=False,
        total_latency_ms=120.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is True
    assert r.error_type is None
    assert r.echo_matched is True
    assert r.payload_matched is True
    assert r.fence_stripped is False


def test_pass_with_fence_stripped():
    nonce = "AABBCCDDEEFF0011"
    import json

    inner = json.dumps({"echo": nonce, "payload": _UTF8_PAYLOAD}, ensure_ascii=False)
    resp = {
        "content": f"```json\n{inner}\n```",
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="gpt-5.5",
        surface="api",
        nonce=nonce,
        response=resp,
        is_thinking=False,
        total_latency_ms=80.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is True
    assert r.fence_stripped is True
    assert r.error_type is None


def test_fail_json_parse_error_on_prose():
    resp = {
        "content": "Sure! echo is XYZ and the payload is the string you gave me.",
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce="00112233AABBCCDD",
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "json_parse_error"


def test_fail_prose_around_fence_is_schema_invalid():
    """Council C4 MAJOR-1: prose wrapping a fence is schema_invalid, not
    json_parse_error."""
    nonce = "1122334455667788"
    import json

    inner = json.dumps({"echo": nonce, "payload": _UTF8_PAYLOAD}, ensure_ascii=False)
    resp = {
        "content": f"Here is the JSON you asked for:\n```json\n{inner}\n```\nHope that helps!",
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce=nonce,
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "schema_invalid"


def test_fail_echo_mismatch():
    resp = {
        "content": '{"echo": "WRONGNONCE000000", "payload": "%s"}' % _UTF8_PAYLOAD,
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce="RIGHTNONCE111111",
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "echo_mismatch"
    assert r.extra["expected_echo"] == "RIGHTNONCE111111"


def test_fail_echo_missing():
    resp = {
        "content": '{"payload": "%s", "other": "stuff"}' % _UTF8_PAYLOAD,
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce="ABCDEF0123456789",
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "echo_missing"


def test_fail_payload_missing():
    resp = {
        "content": '{"echo": "ABCDEF0123456789"}',
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce="ABCDEF0123456789",
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "payload_missing"


def test_fail_echo_not_a_string_is_schema_invalid():
    """Council C4 MAJOR-2: non-string fields are schema_invalid, not coerced."""
    resp = {
        "content": '{"echo": 42, "payload": "%s"}' % _UTF8_PAYLOAD,
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce="ABCDEF0123456789",
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "schema_invalid"


def test_fail_payload_wrong_unknown_reason():
    """Council C4 MAJOR-3 / BLOCKER-1: correct echo but a wrong payload that is
    not mojibake/NFC/replacement must FAIL as payload_mismatch — never a silent
    pass."""
    nonce = "ABCDEF0123456789"
    resp = {
        "content": '{"echo": "%s", "payload": "completely wrong text"}' % nonce,
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce=nonce,
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "payload_mismatch"
    assert r.payload_matched is False
    assert r.echo_matched is True


def test_fail_utf8_mojibake_full_payload():
    """BLOCKER-2: corruption on the WHOLE payload (incl. em-dash/CJK/emoji)."""
    nonce = "ABCDEF0123456789"
    import json

    resp = {
        "content": json.dumps(
            {"echo": nonce, "payload": _UTF8_PAYLOAD_MOJIBAKE}, ensure_ascii=False
        ),
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce=nonce,
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "utf8_corrupted:mojibake"
    assert r.payload_matched is False


def test_fail_utf8_replacement_char():
    nonce = "ABCDEF0123456789"
    import json

    resp = {
        "content": json.dumps(
            {"echo": nonce, "payload": "éàçü � 日本語"}, ensure_ascii=False
        ),
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce=nonce,
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "utf8_corrupted:replacement_char"


def test_pass_nfc_normalized_payload_is_informative():
    """Council C4 D2-DEC-08: NFC normalisation PASSES with a diagnostic, and
    payload_matched is False (strict byte equality)."""
    nonce = "ABCDEF0123456789"
    import json

    resp = {
        "content": json.dumps(
            {"echo": nonce, "payload": _UTF8_PAYLOAD_NFC}, ensure_ascii=False
        ),
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce=nonce,
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is True
    assert r.error_type is None
    assert r.extra.get("informative") == "utf8_corrupted:nfc_mismatch"
    # NFC differs from the NFD source → not a strict byte match.
    assert r.payload_matched is (_UTF8_PAYLOAD_NFC == _UTF8_PAYLOAD)


def test_fail_echo_in_reasoning_only():
    """Content empty, nonce leaked into reasoning → user-invisible (content-only
    matching, Council C4 D2-DEC-10)."""
    nonce = "ABCDEF0123456789"
    resp = {
        "content": "",
        "reasoning": f'Let me build the JSON: {{"echo": "{nonce}", ...}}',
        "finish_reason": "length",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="qwen3.5:397b-cloud",
        surface="api",
        nonce=nonce,
        response=resp,
        is_thinking=True,
        total_latency_ms=50000.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "echo_in_reasoning_only"
    assert r.extra["nonce_in_reasoning"] is True


def test_fail_content_filtered():
    resp = {"content": "", "reasoning": "", "finish_reason": "content_filter", "done_received": True}
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce="ABCDEF0123456789",
        response=resp,
        is_thinking=False,
        total_latency_ms=100.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "content_filtered"


def test_fail_schema_invalid_top_level_list():
    nonce = "ABCDEF0123456789"
    resp = {
        "content": '[{"echo": "%s", "payload": "x"}]' % nonce,
        "reasoning": "",
        "finish_reason": "stop",
        "done_received": True,
    }
    r = _verdict_from_response(
        model_id="m",
        surface="api",
        nonce=nonce,
        response=resp,
        is_thinking=False,
        total_latency_ms=50.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )
    assert r.passed is False
    assert r.error_type == "schema_invalid"
    assert "object" in r.extra["reason"]


# ── run_d2_integrity: orchestration with a fake surface client ───────────────


class _FakeSurfaceClient:
    """Test double: pre-canned chat() responses keyed by call order. Each
    response is a callable receiving the nonce extracted from the prompt."""

    def __init__(self, responses):
        self.name = "api"
        self.responses = list(responses)
        self.calls = []

    def chat(self, *, model, messages, stream, timeout, max_tokens, temperature):
        self.calls.append({"model": model, "messages": messages, "max_tokens": max_tokens})
        text = messages[0]["content"] if messages else ""
        m = re.search(r'"echo":\s*"([^"]+)"', text)
        nonce = m.group(1) if m else ""
        return self.responses.pop(0)(nonce)


def test_quarantine_short_circuits(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient(responses=[])
    r = run_d2_integrity(
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
    r = run_d2_integrity(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert r.passed is True
    assert r.attempts == 1
    assert len(client.calls) == 1


def test_retry_recovers_from_json_parse_error(monkeypatch):
    """Attempt 1 returns prose → retry recovers with valid JSON."""
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient(
        responses=[
            lambda n: {
                "content": "Sorry, here's the answer in prose.",
                "reasoning": "",
                "finish_reason": "stop",
                "done_received": True,
            },
            lambda n: _ok_response(n),
        ]
    )
    r = run_d2_integrity(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert r.passed is True
    assert r.attempts == 2
    assert r.first_attempt_latency_ms is not None
    assert len(client.calls) == 2


def test_no_retry_on_mojibake(monkeypatch):
    """Council C4 MAJOR-4 / D2-DEC-07: UTF-8 corruption is systemic, no retry."""
    _override_skip(monkeypatch)
    import json

    client = _FakeSurfaceClient(
        responses=[
            lambda n: {
                "content": json.dumps(
                    {"echo": n, "payload": _UTF8_PAYLOAD_MOJIBAKE}, ensure_ascii=False
                ),
                "reasoning": "",
                "finish_reason": "stop",
                "done_received": True,
            }
        ]
    )
    r = run_d2_integrity(
        model_id="m",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert r.passed is False
    assert r.error_type == "utf8_corrupted:mojibake"
    assert r.attempts == 1
    assert len(client.calls) == 1


def test_no_retry_on_schema_invalid(monkeypatch):
    """Council C4 MAJOR-4: schema_invalid is a stable behaviour, no retry."""
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient(
        responses=[
            lambda n: {
                "content": '[{"echo": "%s"}]' % n,
                "reasoning": "",
                "finish_reason": "stop",
                "done_received": True,
            }
        ]
    )
    r = run_d2_integrity(
        model_id="m",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert r.passed is False
    assert r.error_type == "schema_invalid"
    assert r.attempts == 1
    assert len(client.calls) == 1


def test_max_tokens_bumped_for_thinking(monkeypatch):
    """Council C4 MINOR-1: thinking models get 2048 (aligned with D1)."""
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient(responses=[lambda n: _ok_response(n)])
    run_d2_integrity(
        model_id="kimi-k2.6:cloud",
        surface_client=client,
        catalog_entry={"timeout_s": 180, "is_thinking": True},
    )
    assert client.calls[0]["max_tokens"] == 2048


def test_max_tokens_small_for_non_thinking(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient(responses=[lambda n: _ok_response(n)])
    run_d2_integrity(
        model_id="claude-haiku-4-5",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert client.calls[0]["max_tokens"] == 256


@pytest.mark.parametrize(
    "surface,expected_mult",
    [("api", 1.0), ("mcp", 1.5), ("ui", 2.0), ("unknown", 1.0)],
)
def test_timeout_multipliers(surface, expected_mult, monkeypatch):
    _override_skip(monkeypatch)
    captured = []

    class _CaptureClient:
        name = surface

        def chat(self, *, timeout, **kw):
            captured.append(timeout)
            text = kw["messages"][0]["content"]
            nonce = re.search(r'"echo":\s*"([^"]+)"', text).group(1)
            return _ok_response(nonce)

    run_d2_integrity(
        model_id="m",
        surface_client=_CaptureClient(),
        catalog_entry={"timeout_s": 60, "is_thinking": False},
    )
    assert captured == [60 * expected_mult]
