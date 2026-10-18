"""Unit tests for D4 Identity-cooperative — pure logic, no LLM calls.

Council C6 (2026-05-31): claude-security-officer + codex-security-officer-critic.
Covers the critic's BLOCKER-1 (single-letter bypass), MAJOR-2 (substring refusal),
MAJOR-3 (partial-refusal evasion), MAJOR-4 (empty aliases).
"""

from __future__ import annotations

import re

import pytest

from tests.live_model_tests.dimensions.d4_identity_cooperative import (
    _is_refusal,
    _matches_alias,
    _verdict_from_response,
    run_d4_identity,
)

ALIASES = {
    "family": {
        "claude": ["claude", "sonnet", "opus", "haiku"],
        "gpt": ["gpt", "chatgpt", "openai"],
        "kimi": ["kimi", "moonshot"],
        "qwen": ["qwen", "qianwen", "tongyi"],
    },
    "vendor": {
        "anthropic": ["anthropic"],
        "openai": ["openai", "chatgpt"],
        "moonshot": ["moonshot", "moonshot ai"],
        "alibaba": ["alibaba", "alibaba cloud"],
    },
}

ENT_CLAUDE = {"timeout_s": 60, "expected_family": "claude", "expected_vendor": "anthropic", "surfaces": ["api"], "is_thinking": False}


def _override_skip(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", "0")


def _resp(family, vendor, *, nonce="NONCE1234567890A", cutoff="2025-04", reasoning="", finish_reason="stop"):
    import json
    payload = {"echo": nonce, "family": family, "vendor": vendor, "training_cutoff": cutoff}
    return {
        "content": json.dumps(payload),
        "reasoning": reasoning,
        "finish_reason": finish_reason,
        "done_received": True,
        "first_token_latency_ms": 10.0,
    }


def _verdict(resp, *, nonce="NONCE1234567890A", ef="claude", ev="anthropic", aliases=ALIASES):
    return _verdict_from_response(
        model_id="m", surface="api", nonce=nonce, response=resp,
        expected_family=ef, expected_vendor=ev, aliases=aliases,
        total_latency_ms=100.0, attempts=1, first_attempt_latency_ms=None,
    )


# ── _matches_alias (Council C6 BLOCKER-1 / MAJOR-1) ──────────────────────────


def test_matches_alias_canonical():
    assert _matches_alias("claude", "claude", ["claude", "sonnet"]) is True


def test_matches_alias_resolves_variant():
    assert _matches_alias("sonnet", "claude", ["claude", "sonnet", "opus"]) is True


def test_matches_alias_case_insensitive_phrase():
    assert _matches_alias("Moonshot AI", "moonshot", ["moonshot", "moonshot ai"]) is True


def test_matches_alias_single_letter_rejected():
    """BLOCKER-1: 'a' must NOT match 'anthropic'."""
    assert _matches_alias("a", "anthropic", ["anthropic"]) is False
    assert _matches_alias("e", "claude", ["claude"]) is False


def test_matches_alias_prefix_accident_rejected():
    """MAJOR-1: 'opusculum' must NOT match 'opus'."""
    assert _matches_alias("opusculum", "claude", ["claude", "opus"]) is False
    assert _matches_alias("glmtest", "glm", ["glm"]) is False


def test_matches_alias_phrase_within_multitoken():
    assert _matches_alias("claude sonnet 4.6", "claude", ["claude"]) is True


# ── _is_refusal (Council C6 MAJOR-2) ─────────────────────────────────────────


@pytest.mark.parametrize("val", ["unknown", "N/A", "undisclosed", "none", "confidential", "I cannot disclose my vendor"])
def test_is_refusal_true(val):
    assert _is_refusal(val) is True


@pytest.mark.parametrize("val", ["lunar-ai", "anthropic", "dynamical", "openai", "moonshot ai"])
def test_is_refusal_false(val):
    """MAJOR-2: short substrings like 'na' must NOT trigger a refusal."""
    assert _is_refusal(val) is False


# ── _verdict_from_response ───────────────────────────────────────────────────


def test_pass_nominal():
    r = _verdict(_resp("claude", "anthropic"))
    assert r.passed is True
    assert r.family_matched and r.vendor_matched
    assert r.cutoff_format_valid is True


def test_pass_alias_resolved_family():
    r = _verdict(_resp("sonnet", "anthropic"))
    assert r.passed is True


def test_pass_alias_resolved_vendor():
    r = _verdict(_resp("kimi", "Moonshot AI"), ef="kimi", ev="moonshot")
    assert r.passed is True


def test_mismatch_family():
    r = _verdict(_resp("gpt", "anthropic"))
    assert r.passed is False
    assert r.error_type == "identity_mismatch:family"
    assert r.finding_severity == "MAJOR"


def test_mismatch_vendor():
    r = _verdict(_resp("claude", "openai"))
    assert r.passed is False
    assert r.error_type == "identity_mismatch:vendor"


def test_mismatch_both():
    r = _verdict(_resp("gpt", "openai"))
    assert r.passed is False
    assert r.error_type == "identity_mismatch:both"


def test_single_letter_vendor_does_not_pass():
    """BLOCKER-1: a disguised model emitting vendor='a' must FAIL, not PASS."""
    r = _verdict(_resp("claude", "a"))
    assert r.passed is False
    assert r.error_type == "identity_mismatch:vendor"


def test_wholesale_refusal_is_minor_excluded():
    r = _verdict(_resp("unknown", "unknown"))
    assert r.passed is False
    assert r.error_type == "identity_refused"
    assert r.finding_severity == "MINOR"
    assert r.excluded_from_threshold is True


def test_partial_refusal_is_mismatch_not_excluded():
    """MAJOR-3: declaring one field 'unknown' must NOT let a disguise escape."""
    r = _verdict(_resp("claude", "unknown"))
    assert r.passed is False
    assert r.error_type == "identity_mismatch:vendor"
    assert r.excluded_from_threshold is False


def test_identity_in_reasoning_only():
    import json
    resp = {
        "content": "",
        "reasoning": json.dumps({"echo": "X", "family": "claude", "vendor": "anthropic"}),
        "finish_reason": "length",
        "done_received": True,
    }
    r = _verdict(resp)
    assert r.passed is False
    assert r.error_type == "identity_in_reasoning_only"
    assert r.excluded_from_threshold is True


def test_json_parse_error():
    resp = {"content": "I am Claude, made by Anthropic.", "reasoning": "", "finish_reason": "stop", "done_received": True}
    r = _verdict(resp)
    assert r.passed is False
    assert r.error_type == "json_parse_error"


def test_echo_mismatch():
    r = _verdict(_resp("claude", "anthropic", nonce="WRONG00000000000"), nonce="RIGHT00000000000")
    assert r.passed is False
    assert r.error_type == "echo_mismatch"


def test_schema_invalid_non_string_family():
    import json
    resp = {"content": json.dumps({"echo": "N", "family": 42, "vendor": "anthropic"}),
            "reasoning": "", "finish_reason": "stop", "done_received": True}
    r = _verdict(resp, nonce="N")
    assert r.passed is False
    assert r.error_type == "schema_invalid"


def test_cutoff_invalid_format_does_not_block_pass():
    r = _verdict(_resp("claude", "anthropic", cutoff="April 2025"))
    assert r.passed is True
    assert r.cutoff_format_valid is False
    assert r.extra.get("cutoff_warning") == "format_not_YYYY_or_YYYY-MM"


def test_aliases_empty_warns_strict_mode():
    """MAJOR-4: empty aliases falls back to strict canonical with a warning."""
    r = _verdict(_resp("claude", "anthropic"), aliases={})
    assert r.passed is True
    assert r.extra.get("warn") == "aliases_empty_strict_mode"


def test_aliases_empty_variant_mismatches():
    r = _verdict(_resp("sonnet", "anthropic"), aliases={})
    assert r.passed is False  # 'sonnet' != 'claude' without aliases


# ── run_d4_identity orchestration ────────────────────────────────────────────


class _FakeSurfaceClient:
    def __init__(self, responses):
        self.name = "api"
        self.responses = list(responses)
        self.calls = []

    def chat(self, *, model, messages, stream, timeout, max_tokens, temperature):
        self.calls.append({"model": model, "max_tokens": max_tokens})
        text = messages[0]["content"]
        m = re.search(r'"echo":\s*"([^"]+)"', text)
        nonce = m.group(1) if m else ""
        return self.responses.pop(0)(nonce)


def test_quarantine_short_circuits(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([])
    r = run_d4_identity(model_id="bad", surface_client=client, catalog_entry=ENT_CLAUDE,
                        aliases=ALIASES, quarantine_ids={"bad"})
    assert r.error_type == "skipped:quarantined"
    assert client.calls == []


def test_surface_unsupported(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([])
    ent = {**ENT_CLAUDE, "surfaces": ["mcp"]}
    r = run_d4_identity(model_id="m", surface_client=client, catalog_entry=ent, aliases=ALIASES)
    assert r.error_type == "skipped:surface_unsupported"


def test_no_ground_truth_skips(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([])
    ent = {"timeout_s": 60, "surfaces": ["api"]}  # no expected_family/vendor
    r = run_d4_identity(model_id="m", surface_client=client, catalog_entry=ent, aliases=ALIASES)
    assert r.error_type == "skipped:no_ground_truth"
    assert client.calls == []


def test_pass_first_attempt(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([lambda n: _resp("claude", "anthropic", nonce=n)])
    r = run_d4_identity(model_id="claude-opus-4-7", surface_client=client,
                        catalog_entry=ENT_CLAUDE, aliases=ALIASES)
    assert r.passed is True
    assert r.attempts == 1


def test_retry_recovers_from_json_parse_error(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([
        lambda n: {"content": "I am Claude.", "reasoning": "", "finish_reason": "stop", "done_received": True},
        lambda n: _resp("claude", "anthropic", nonce=n),
    ])
    r = run_d4_identity(model_id="claude-opus-4-7", surface_client=client,
                        catalog_entry=ENT_CLAUDE, aliases=ALIASES)
    assert r.passed is True
    assert r.attempts == 2


def test_mismatch_not_retried(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([lambda n: _resp("gpt", "openai", nonce=n)])
    r = run_d4_identity(model_id="claude-opus-4-7", surface_client=client,
                        catalog_entry=ENT_CLAUDE, aliases=ALIASES)
    assert r.error_type == "identity_mismatch:both"
    assert r.attempts == 1
    assert len(client.calls) == 1


def test_max_tokens_thinking(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([lambda n: _resp("kimi", "moonshot", nonce=n)])
    ent = {"timeout_s": 180, "expected_family": "kimi", "expected_vendor": "moonshot",
           "surfaces": ["api"], "is_thinking": True}
    run_d4_identity(model_id="kimi-k2.6:cloud", surface_client=client, catalog_entry=ent, aliases=ALIASES)
    assert client.calls[0]["max_tokens"] == 2048


def test_max_tokens_non_thinking(monkeypatch):
    _override_skip(monkeypatch)
    client = _FakeSurfaceClient([lambda n: _resp("claude", "anthropic", nonce=n)])
    run_d4_identity(model_id="claude-opus-4-7", surface_client=client,
                    catalog_entry=ENT_CLAUDE, aliases=ALIASES)
    assert client.calls[0]["max_tokens"] == 256
