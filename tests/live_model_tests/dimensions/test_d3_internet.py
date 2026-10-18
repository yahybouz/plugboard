"""Unit tests for D3 Internet — pure logic, no LLM calls.

Council C5 (2026-05-31): claude-security-officer + codex-security-officer-critic
+ gemini-security-reviewer. Deterministic via an injected reference date.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from tests.live_model_tests.dimensions.d3_internet import (
    _adapt_to_probe_shape,
    _normalize_expected_internet,
    _verdict_from_unified,
    run_d3_internet,
)

# 2026-05-31 is a Sunday / dimanche; yesterday = 2026-05-30.
REF = _dt.datetime(2026, 5, 31, 12, 0, tzinfo=_dt.timezone.utc)

_ONLINE_HIGH = (
    "Today is 2026-05-31, dimanche. Hier: 2026-05-30. Source: https://time.gov"
)
_ONLINE_LOW = "Aujourd'hui 2026-05-31, dimanche."  # date + weekday only
_OFFLINE = "NO-WEB-ACCESS"
_DATE_ONLY = "2026-05-31"  # date, no safeguards


def _override_skip(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", "0")


def _unified(content: str, *, reasoning: str = "", finish_reason: str = "stop") -> dict:
    return {
        "content": content,
        "reasoning": reasoning,
        "finish_reason": finish_reason,
        "done_received": True,
        "first_token_latency_ms": 10.0,
    }


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("unknown", None),
        (None, None),
        ("offline", False),
        ("online", True),
    ],
)
def test_normalize_expected_internet(value, expected):
    assert _normalize_expected_internet(value) is expected


def test_adapt_to_probe_shape():
    shaped = _adapt_to_probe_shape({"content": "hi", "reasoning": "because"})
    assert shaped["choices"][0]["message"]["content"] == "hi"
    assert shaped["choices"][0]["message"]["reasoning"] == "because"


# ── _verdict_from_unified ────────────────────────────────────────────────────


def _verdict(content, expected, *, reasoning="", finish_reason="stop"):
    return _verdict_from_unified(
        model_id="m",
        surface="api",
        expected_internet=expected,
        unified=_unified(content, reasoning=reasoning, finish_reason=finish_reason),
        now=REF,
        total_latency_ms=100.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )


def test_pass_online_expected_online():
    r = _verdict(_ONLINE_HIGH, True)
    assert r.passed is True
    assert r.observed_status == "online"


def test_pass_offline_expected_offline():
    r = _verdict(_OFFLINE, False)
    assert r.passed is True
    assert r.observed_status == "offline"


def test_mismatch_expected_online_got_offline_is_blocker():
    r = _verdict(_OFFLINE, True)
    assert r.passed is False
    assert r.error_type == "internet_mismatch:expected_online_got_offline"
    assert r.finding_severity == "BLOCKER"


def test_mismatch_expected_offline_got_online_high_conf_is_blocker():
    r = _verdict(_ONLINE_HIGH, False)
    assert r.passed is False
    assert r.error_type == "internet_mismatch:expected_offline_got_online"
    assert r.finding_severity == "BLOCKER"


def test_mismatch_expected_offline_got_online_low_conf_is_major():
    """Council C5 BLOCKER-2: low-confidence online evidence is MAJOR, not BLOCKER."""
    r = _verdict(_ONLINE_LOW, False)
    assert r.passed is False
    assert r.error_type == "internet_mismatch:expected_offline_got_online"
    assert r.observed_confidence == "low"
    assert r.finding_severity == "MAJOR"


def test_signal_in_reasoning_only_is_major_excluded():
    """Council C5 BLOCKER-1: date only in reasoning, content says NO-WEB-ACCESS."""
    r = _verdict(_OFFLINE, False, reasoning=_ONLINE_HIGH)
    assert r.passed is False
    assert r.error_type == "signal_in_reasoning_only"
    assert r.finding_severity == "MAJOR"
    assert r.excluded_from_threshold is True


def test_offline_expected_offline_ignores_reasoning_date():
    """Council C5 BLOCKER-1 core: a thinking model offline whose reasoning has the
    date but content says NO-WEB-ACCESS must NOT be a false 'online' BLOCKER.
    Here content has no date and no reasoning online-signal beyond it → the
    reasoning DOES carry the date, so it surfaces as signal_in_reasoning_only
    (excluded), never as a mismatch BLOCKER."""
    r = _verdict(
        _OFFLINE, False, reasoning="il doit être 2026-05-31, dimanche, hier 2026-05-30"
    )
    assert r.error_type == "signal_in_reasoning_only"
    assert r.excluded_from_threshold is True
    # The crucial property: it is NOT a false expected_offline_got_online BLOCKER.
    assert r.error_type != "internet_mismatch:expected_offline_got_online"


def test_probe_inconclusive_date_only():
    r = _verdict(_DATE_ONLY, True)
    assert r.passed is False
    assert r.error_type == "probe_inconclusive"


def test_content_filtered():
    r = _verdict("", True, finish_reason="content_filter")
    assert r.passed is False
    assert r.error_type == "content_filtered"


# ── run_d3_internet orchestration ────────────────────────────────────────────


class _FakeSurfaceClient:
    def __init__(self, responses, *, name="api"):
        self.name = name
        self.responses = list(responses)
        self.calls = []

    def chat(self, *, model, messages, stream, timeout, max_tokens, temperature):
        self.calls.append({"model": model, "max_tokens": max_tokens})
        return self.responses.pop(0)


def test_quarantine_short_circuits(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([])
    r = run_d3_internet(
        model_id="bad",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "expected_internet": True, "surfaces": ["api"]},
        quarantine_ids={"bad"},
        now=REF,
    )
    assert r.error_type == "skipped:quarantined"
    assert r.excluded_from_threshold is True
    assert client.calls == []


def test_surface_unsupported_skips(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([])
    r = run_d3_internet(
        model_id="m",
        surface_client=client,
        catalog_entry={
            "timeout_s": 60,
            "expected_internet": True,
            "surfaces": ["mcp", "ui"],
        },
        now=REF,
    )
    assert r.error_type == "skipped:surface_unsupported"
    assert client.calls == []


def test_unknown_ground_truth_observe_only(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([_unified(_ONLINE_HIGH)])
    r = run_d3_internet(
        model_id="gpt-oss:120b-cloud",
        surface_client=client,
        catalog_entry={
            "timeout_s": 180,
            "expected_internet": "unknown",
            "surfaces": ["api"],
        },
        now=REF,
    )
    assert r.error_type == "skipped:unknown_ground_truth"
    assert r.excluded_from_threshold is True
    assert r.observed_status == "online"  # still observed for the scorecard
    assert len(client.calls) == 1


def test_pass_first_attempt(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([_unified(_OFFLINE)])
    r = run_d3_internet(
        model_id="kimi-k2.6:cloud",
        surface_client=client,
        catalog_entry={
            "timeout_s": 180,
            "expected_internet": False,
            "surfaces": ["api"],
        },
        now=REF,
    )
    assert r.passed is True
    assert r.attempts == 1


def test_retry_recovers_from_inconclusive(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([_unified(_DATE_ONLY), _unified(_ONLINE_HIGH)])
    r = run_d3_internet(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "expected_internet": True, "surfaces": ["api"]},
        now=REF,
    )
    assert r.passed is True
    assert r.attempts == 2
    assert len(client.calls) == 2


def test_mismatch_not_retried(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([_unified(_OFFLINE)])
    r = run_d3_internet(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "expected_internet": True, "surfaces": ["api"]},
        now=REF,
    )
    assert r.error_type == "internet_mismatch:expected_online_got_offline"
    assert r.attempts == 1
    assert len(client.calls) == 1


def test_max_tokens_from_probe_body_thinking(monkeypatch):
    """Council C5 MAJOR-3: max_tokens governed by build_internet_probe_body
    (model-id heuristic: 'cloud' → thinking → 2048)."""
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([_unified(_OFFLINE)])
    run_d3_internet(
        model_id="deepseek-v4-pro:cloud",
        surface_client=client,
        catalog_entry={
            "timeout_s": 180,
            "expected_internet": False,
            "surfaces": ["api"],
        },
        now=REF,
    )
    assert client.calls[0]["max_tokens"] == 2048


def test_max_tokens_from_probe_body_non_thinking(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([_unified(_ONLINE_HIGH)])
    run_d3_internet(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry={"timeout_s": 60, "expected_internet": True, "surfaces": ["api"]},
        now=REF,
    )
    assert client.calls[0]["max_tokens"] == 300
