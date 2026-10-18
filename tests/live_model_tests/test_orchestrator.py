"""Unit + integration tests for the live-test orchestrator and reports.

Council C10 (2026-05-31): developer + codex-coder. Zero network — fake surface
clients + hand-built Result dataclasses.
"""

from __future__ import annotations

import json
import re


from tests.live_model_tests.dimensions.d1_liveness import LivenessResult
from tests.live_model_tests.dimensions.d3_internet import InternetResult
from tests.live_model_tests.reports.scorecard_renderer import render_scorecard
from tests.live_model_tests.reports.trends_aggregator import detect_regressions
from tests.live_model_tests.run_live_tests import (
    _is_excluded,
    _result_to_dict,
    aggregate,
    check_cross_surface_agreement,
    compute_verdict,
    run,
)


def _override_skip(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_RUN_LIVE_TESTS", "0")


_EVAL_D1 = {"dimensions": {"d1_liveness": {"pass_threshold": 0.95}}}
_EVAL_D3 = {"dimensions": {"d3_internet": {"pass_threshold": 1.0}}}


def _liveness(model_id, passed, error_type=None):
    return LivenessResult(model_id=model_id, surface="api", passed=passed, error_type=error_type)


def _internet(model_id, passed, error_type=None, finding_severity="BLOCKER", excluded=False):
    return InternetResult(model_id=model_id, surface="api", passed=passed, error_type=error_type,
                          finding_severity=finding_severity, excluded_from_threshold=excluded)


# ── aggregate ────────────────────────────────────────────────────────────────


def test_aggregate_all_pass():
    agg = aggregate([_liveness(f"m{i}", True) for i in range(10)], _EVAL_D1, "d1")
    assert agg["pass_rate"] == 1.0
    assert agg["dimension_pass"] is True
    assert agg["status"] == "pass"
    assert agg["denominator"] == 10


def test_aggregate_below_threshold_fails():
    results = [_liveness(f"m{i}", True) for i in range(9)] + [_liveness("m9", False, "timeout")]
    agg = aggregate(results, _EVAL_D1, "d1")
    assert abs(agg["pass_rate"] - 0.9) < 1e-9
    assert agg["status"] == "fail"
    assert agg["dimension_severity"] == "MAJOR"  # LivenessResult has no finding_severity → default


def test_aggregate_all_excluded_is_skipped():
    results = [
        _internet("m1", False, "skipped:quarantined", excluded=True),
        _internet("m2", False, "skipped:unknown_ground_truth", excluded=True),
    ]
    agg = aggregate(results, _EVAL_D3, "d3")
    assert agg["pass_rate"] is None
    assert agg["dimension_pass"] is True
    assert agg["status"] == "skipped"


def test_aggregate_severity_max_blocker_over_major():
    results = [
        _internet("m1", False, "internet_mismatch:expected_online_got_offline", "BLOCKER"),
        _internet("m2", False, "internet_mismatch:expected_offline_got_online", "MAJOR"),
    ]
    agg = aggregate(results, _EVAL_D3, "d3")
    assert agg["status"] == "fail"
    assert agg["dimension_severity"] == "BLOCKER"


def test_aggregate_signal_in_reasoning_only_excluded():
    """D3's signal_in_reasoning_only (excluded_from_threshold) is out of denom."""
    results = [_internet("m1", True), _internet("m2", False, "signal_in_reasoning_only",
                                                "MAJOR", excluded=True)]
    agg = aggregate(results, _EVAL_D3, "d3")
    assert agg["denominator"] == 1
    assert agg["pass_rate"] == 1.0
    assert agg["status"] == "pass"


def test_is_excluded_variants():
    assert _is_excluded(_internet("m", False, "skipped:cache_hit", excluded=True)) is True
    assert _is_excluded(_internet("m", False, "identity_refused", "MINOR", excluded=True)) is True
    assert _is_excluded(_liveness("m", False, "skipped:quarantined")) is True
    assert _is_excluded(_liveness("m", False, "timeout")) is False  # D1 has no excluded field


# ── cross-surface ────────────────────────────────────────────────────────────


def test_cross_surface_no_disagreement():
    struct = {"api": {"d1": {"a": _liveness("a", True)}},
              "mcp": {"d1": {"a": _liveness("a", True)}}}
    assert check_cross_surface_agreement(struct, []) == []


def test_cross_surface_disagreement():
    struct = {"api": {"d1": {"a": _liveness("a", True)}},
              "mcp": {"d1": {"a": _liveness("a", False, "timeout")}}}
    d = check_cross_surface_agreement(struct, [])
    assert len(d) == 1
    assert d[0]["surface_verdicts"] == {"api": True, "mcp": False}


def test_cross_surface_excluded_dim_skipped():
    struct = {"api": {"d5": {"a": _liveness("a", True)}},
              "mcp": {"d5": {"a": _liveness("a", False, "timeout")}}}
    assert check_cross_surface_agreement(struct, ["d5"]) == []


def test_cross_surface_quarantined_does_not_disagree():
    """A model quarantined on one surface but passing on another is not a disagreement."""
    struct = {"api": {"d3": {"a": _internet("a", True)}},
              "mcp": {"d3": {"a": _internet("a", False, "skipped:quarantined", excluded=True)}}}
    assert check_cross_surface_agreement(struct, []) == []


# ── verdict ──────────────────────────────────────────────────────────────────


def test_verdict_go():
    aggs = {"api.d1": {"status": "pass"}, "api.d3": {"status": "pass"}}
    v = compute_verdict(aggs, [])
    assert v["verdict"] == "GO"
    assert v["max_severity"] is None


def test_verdict_nogo_on_fail():
    aggs = {"api.d3": {"status": "fail", "dimension_severity": "BLOCKER"}}
    v = compute_verdict(aggs, [])
    assert v["verdict"] == "NO-GO"
    assert v["max_severity"] == "BLOCKER"


def test_verdict_nogo_on_cross_surface():
    aggs = {"api.d1": {"status": "pass", "dimension_severity": None}}
    v = compute_verdict(aggs, [{"model_id": "m", "dim": "d1", "severity": "MAJOR"}])
    assert v["verdict"] == "NO-GO"
    assert v["max_severity"] == "MAJOR"


def test_verdict_skipped_dim_surfaced_not_blocking():
    aggs = {"api.d3": {"status": "skipped"}}
    v = compute_verdict(aggs, [])
    assert v["verdict"] == "GO"
    assert "api.d3" in v["skipped_dimensions"]


# ── serialization ────────────────────────────────────────────────────────────


def test_result_to_dict():
    d = _result_to_dict(_liveness("a", True), surface="api", dim="d1", run_id="r1")
    assert d["model_id"] == "a" and d["passed"] is True
    assert d["_dim"] == "d1" and d["_surface"] == "api" and d["_run_id"] == "r1"
    assert "_ts" in d


def test_result_to_dict_extra_nested():
    r = InternetResult(model_id="m", surface="api", passed=False,
                       error_type="signal_in_reasoning_only",
                       extra={"reasoning_excerpt": "x", "nested": {"a": 1}})
    d = _result_to_dict(r, surface="api", dim="d3", run_id="r2")
    assert d["extra"]["nested"]["a"] == 1


# ── scorecard ────────────────────────────────────────────────────────────────


def test_scorecard_render_smoke():
    summary = {
        "run_id": "20260531T120000Z", "timestamp": "2026-05-31T12:00:00Z",
        "verdict": "GO", "max_severity": None, "surfaces": ["api"],
        "dimensions": ["d1"], "models": ["claude-haiku-4-5"],
        "cross_surface_disagreements": [], "skipped_dimensions": [],
        "dim_aggregates": {"api.d1": {"status": "pass", "pass_rate": 1.0, "threshold": 0.95,
                                      "dimension_severity": None}},
        "raw_records": [{"model_id": "claude-haiku-4-5", "_dim": "d1", "_surface": "api",
                         "passed": True, "error_type": None}],
    }
    out = render_scorecard(summary)
    assert "<!DOCTYPE html>" in out and "GO" in out and "claude-haiku-4-5" in out


def test_scorecard_nogo_shows_severity():
    summary = {
        "run_id": "r2", "timestamp": "t", "verdict": "NO-GO", "max_severity": "BLOCKER",
        "surfaces": ["api"], "dimensions": ["d3"], "models": ["m1"],
        "cross_surface_disagreements": [], "skipped_dimensions": [],
        "dim_aggregates": {"api.d3": {"status": "fail", "pass_rate": 0.5, "threshold": 1.0,
                                      "dimension_severity": "BLOCKER"}},
        "raw_records": [],
    }
    out = render_scorecard(summary)
    assert "NO-GO" in out and "BLOCKER" in out


# ── trends ───────────────────────────────────────────────────────────────────


def test_trends_regression_detected():
    history = [
        {"run_id": "r1", "dim_aggregates": {"api.d1": {"pass_rate": 1.0}}},
        {"run_id": "r2", "dim_aggregates": {"api.d1": {"pass_rate": 0.8}}},
    ]
    rep = detect_regressions(history, regression_threshold=0.05)
    assert rep["has_regressions"] is True
    assert abs(rep["regressions"][0]["delta"] - (-0.2)) < 1e-9


def test_trends_stable_no_regression():
    history = [
        {"run_id": "r1", "dim_aggregates": {"api.d1": {"pass_rate": 0.95}}},
        {"run_id": "r2", "dim_aggregates": {"api.d1": {"pass_rate": 0.96}}},
    ]
    assert detect_regressions(history)["has_regressions"] is False


def test_trends_single_run():
    rep = detect_regressions([{"run_id": "r1", "dim_aggregates": {}}])
    assert rep["has_regressions"] is False
    assert "at least 2" in rep["note"]


def test_trends_skipped_dim_not_compared():
    history = [
        {"run_id": "r1", "dim_aggregates": {"api.d5": {"pass_rate": None}}},
        {"run_id": "r2", "dim_aggregates": {"api.d5": {"pass_rate": None}}},
    ]
    assert detect_regressions(history)["has_regressions"] is False


# ── integration: run() with a fake surface, no network ───────────────────────


class _FakeD1Surface:
    """Echoes the D1 nonce so D1 Liveness passes; declines everything else."""

    name = "api"
    supports_billing = True

    def chat(self, *, model, messages, stream, timeout, max_tokens, temperature):
        text = messages[0]["content"]
        m = re.search(r"ALIVE:([0-9A-F]+)", text)
        nonce = m.group(1) if m else ""
        return {
            "content": f"ALIVE:{nonce}", "reasoning": "", "finish_reason": "stop",
            "done_received": True, "first_token_latency_ms": 5.0, "raw_chunks": [],
            "raw_sse_lines": [f"data: {nonce}", "data: [DONE]"], "billing": None,
            "model_echoed": model, "request_id": "req-x",
        }


def test_run_integration_d1_go(monkeypatch, tmp_path):
    _override_skip(monkeypatch)
    exit_code, summary = run(
        surfaces=["api"], dimensions=["d1"], model_ids=["claude-haiku-4-5"],
        base_url="http://x", api_key="lle_test", out_dir=tmp_path, run_id="testrun",
        surface_factory=lambda s, b, k: _FakeD1Surface(),
        pricing_resolver=lambda m, mo: None, verbose=False,
    )
    assert exit_code == 0
    assert summary["verdict"] == "GO"
    assert summary["dim_aggregates"]["api.d1"]["status"] == "pass"
    assert summary["raw_records"]  # Council C10 BLOCKER-1 — populated for scorecard
    # JSONL persisted
    jsonl = tmp_path / "testrun.jsonl"
    assert jsonl.exists()
    lines = jsonl.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["model_id"] == "claude-haiku-4-5"
    # scorecard renders from the real summary
    assert "claude-haiku-4-5" in render_scorecard(summary)


def test_run_unimplemented_surface_skipped(monkeypatch, tmp_path):
    _override_skip(monkeypatch)

    def factory(surface, base_url, api_key):
        if surface == "api":
            return _FakeD1Surface()
        raise NotImplementedError(f"{surface} not ready")

    exit_code, summary = run(
        surfaces=["api", "mcp"], dimensions=["d1"], model_ids=["claude-haiku-4-5"],
        base_url="http://x", api_key="lle_test", out_dir=tmp_path, run_id="r-mcp",
        surface_factory=factory, pricing_resolver=lambda m, mo: None, verbose=False,
    )
    # mcp skipped (NotImplementedError) → only api in aggregates, still GO
    assert exit_code == 0
    assert "api.d1" in summary["dim_aggregates"]
    assert not any(k.startswith("mcp.") for k in summary["dim_aggregates"])
