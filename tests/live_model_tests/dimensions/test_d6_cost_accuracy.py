"""Unit tests for D6 Cost accuracy — pure logic, no LLM/network calls.

Council C6-bis (2026-05-31): claude-data-engineer + codex-data-engineer-critic.
Covers the critic's BLOCKER-1 (billed-model/month resolution), BLOCKER-2
(supports_billing discriminant), MAJOR-1 (absolute cost floor).
"""

from __future__ import annotations


from tests.live_model_tests.dimensions.d6_cost_accuracy import (
    _compare_costs,
    _verdict_from_billing,
    run_d6_cost_accuracy,
)

# Opus-like pricing: $15 / $75 per 1M tokens.
PRICE = {"input_price": 15.0, "output_price": 75.0}
# 10 prompt + 5 completion → 10/1e6*15 + 5/1e6*75 = 1.5e-4 + 3.75e-4 = 5.25e-4
EXPECTED = round(10 / 1_000_000 * 15.0 + 5 / 1_000_000 * 75.0, 8)


def _override_skip(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", "0")


def _billing(
    cost,
    *,
    pt=10,
    ct=5,
    cache_hit=False,
    billed_model="claude-opus-4-7",
    month="2026_05",
    provider="anthropic",
):
    return {
        "cost_usd": cost,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cache_hit": cache_hit,
        "billed_model": billed_model,
        "pricing_month": month,
        "provider": provider,
    }


def _verdict(billing, price=PRICE, tol=0.05):
    return _verdict_from_billing(
        model_id="claude-opus-4-7",
        surface="api",
        billing=billing,
        price_entry=price,
        tolerance_pct=tol,
        total_latency_ms=100.0,
        attempts=1,
        first_attempt_latency_ms=None,
    )


# ── _compare_costs (Council C6-bis MAJOR-1) ──────────────────────────────────


def test_compare_exact():
    ok, et, sev = _compare_costs(5.0e-4, 5.0e-4, 0.05)
    assert ok and et == "cost_match"


def test_compare_within_tolerance():
    ok, et, _ = _compare_costs(5.0e-4, 5.2e-4, 0.05)  # +4%
    assert ok and et == "cost_match"


def test_compare_over_tolerance():
    ok, et, sev = _compare_costs(5.0e-4, 6.0e-4, 0.05)  # +20%
    assert not ok and et == "cost_mismatch:over" and sev == "MAJOR"


def test_compare_under_tolerance():
    ok, et, _ = _compare_costs(5.0e-4, 4.0e-4, 0.05)
    assert not ok and et == "cost_mismatch:under"


def test_compare_both_zero_pass():
    ok, et, _ = _compare_costs(0.0, 0.0, 0.05)
    assert ok and et == "cost_match"


def test_compare_expected_zero_got_positive_blocker():
    ok, et, sev = _compare_costs(0.0, 1.0e-4, 0.05)
    assert (
        not ok and et == "cost_mismatch:expected_zero_got_positive" and sev == "BLOCKER"
    )


def test_compare_expected_positive_got_zero_blocker():
    ok, et, sev = _compare_costs(1.0e-4, 0.0, 0.05)
    assert (
        not ok and et == "cost_mismatch:expected_positive_got_zero" and sev == "BLOCKER"
    )


def test_compare_micro_cost_uses_absolute_tolerance():
    """MAJOR-1: below COST_FLOOR, a 1-ULP offset must not be a 50% false MAJOR."""
    expected = 1.5e-8  # well below COST_FLOOR 1e-6
    ok, et, _ = _compare_costs(
        expected, expected + 1e-8, 0.05
    )  # +66% relative, but tiny absolute
    assert ok and et == "cost_match"


# ── _verdict_from_billing ────────────────────────────────────────────────────


def test_verdict_cost_match():
    r = _verdict(_billing(EXPECTED))
    assert r.passed is True
    assert r.expected_cost_usd == EXPECTED
    assert r.reported_cost_usd == EXPECTED


def test_verdict_cost_mismatch_over():
    r = _verdict(_billing(EXPECTED * 1.2))
    assert r.passed is False
    assert r.error_type == "cost_mismatch:over"


def test_verdict_billing_none_is_cost_header_missing():
    r = _verdict(None)
    assert r.passed is False
    assert r.error_type == "cost_header_missing"
    assert r.excluded_from_threshold is False


def test_verdict_cache_hit_skipped():
    r = _verdict(_billing(EXPECTED, cache_hit=True))
    assert r.error_type == "skipped:cache_hit"
    assert r.excluded_from_threshold is True
    assert r.finding_severity == "INFO"


def test_verdict_usage_missing():
    b = _billing(EXPECTED)
    b["prompt_tokens"] = None
    r = _verdict(b)
    assert r.passed is False
    assert r.error_type == "usage_missing"


def test_verdict_pricing_not_found_skipped():
    r = _verdict(_billing(EXPECTED), price=None)
    assert r.error_type == "skipped:pricing_not_found"
    assert r.excluded_from_threshold is True
    assert r.finding_severity == "INFO"


def test_verdict_cost_header_unparseable():
    r = _verdict(_billing("not-a-number"))
    assert r.passed is False
    assert r.error_type == "cost_header_missing"


# ── run_d6_cost_accuracy orchestration ───────────────────────────────────────


class _BillingSurface:
    supports_billing = True

    def __init__(self, responses, *, name="api"):
        self.name = name
        self.responses = list(responses)
        self.calls = []

    def chat(self, *, model, messages, stream, timeout, max_tokens, temperature):
        self.calls.append({"model": model, "max_tokens": max_tokens})
        return self.responses.pop(0)


class _NonBillingSurface:
    def __init__(self, name="mcp"):
        self.name = name
        self.calls = []

    def chat(self, *, model, messages, stream, timeout, max_tokens, temperature):
        self.calls.append({"model": model})
        return {"content": "pong", "billing": None}


ENT = {"timeout_s": 60, "surfaces": ["api", "mcp"], "is_thinking": False}


def test_quarantine(monkeypatch):
    _override_skip(monkeypatch)
    client = _BillingSurface([])
    r = run_d6_cost_accuracy(
        model_id="bad",
        surface_client=client,
        catalog_entry=ENT,
        quarantine_ids={"bad"},
        pricing_resolver=lambda m, mo: PRICE,
    )
    assert r.error_type == "skipped:quarantined"
    assert client.calls == []


def test_surface_billing_unavailable_excluded(monkeypatch):
    """BLOCKER-2: a non-billing surface (no supports_billing) is SKIPPED, not MAJOR."""
    _override_skip(monkeypatch)
    client = _NonBillingSurface(name="mcp")
    r = run_d6_cost_accuracy(
        model_id="m",
        surface_client=client,
        catalog_entry=ENT,
        pricing_resolver=lambda mo, mn: PRICE,
    )
    assert r.error_type == "skipped:surface_billing_unavailable"
    assert r.excluded_from_threshold is True
    assert client.calls == []


def test_pass_first_attempt(monkeypatch):
    _override_skip(monkeypatch)
    client = _BillingSurface([{"content": "pong", "billing": _billing(EXPECTED)}])
    r = run_d6_cost_accuracy(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry=ENT,
        pricing_resolver=lambda m, mo: PRICE,
    )
    assert r.passed is True
    assert r.attempts == 1


def test_resolver_uses_billed_model_and_month(monkeypatch):
    """BLOCKER-1: price is resolved on the ROUTED model + proxy pricing month."""
    _override_skip(monkeypatch)
    captured = {}

    def resolver(billed_model, pricing_month):
        captured["model"] = billed_model
        captured["month"] = pricing_month
        return PRICE

    billing = _billing(EXPECTED, billed_model="claude-sonnet-4-6", month="2026_05")
    client = _BillingSurface([{"content": "pong", "billing": billing}])
    run_d6_cost_accuracy(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry=ENT,
        pricing_resolver=resolver,
    )
    assert captured["model"] == "claude-sonnet-4-6"  # routed model, not requested opus
    assert captured["month"] == "2026_05"


def test_mismatch_not_retried(monkeypatch):
    _override_skip(monkeypatch)
    client = _BillingSurface([{"content": "pong", "billing": _billing(EXPECTED * 2)}])
    r = run_d6_cost_accuracy(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry=ENT,
        pricing_resolver=lambda m, mo: PRICE,
    )
    assert r.error_type == "cost_mismatch:over"
    assert r.attempts == 1
    assert len(client.calls) == 1


def test_max_tokens_non_thinking(monkeypatch):
    _override_skip(monkeypatch)
    client = _BillingSurface([{"content": "pong", "billing": _billing(EXPECTED)}])
    run_d6_cost_accuracy(
        model_id="claude-opus-4-7",
        surface_client=client,
        catalog_entry=ENT,
        pricing_resolver=lambda m, mo: PRICE,
    )
    assert client.calls[0]["max_tokens"] == 32
