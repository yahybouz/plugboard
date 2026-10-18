"""Tests for the per-provider top-N model ranker."""

from __future__ import annotations

from datetime import datetime, timezone

from plugboard.model_ranker import (
    _bench_score,
    _parse_release_date,
    _recency_bonus,
    score_model,
    top_n_per_provider,
)


# ── Bench score: prefers Arena ELO, falls back to MMLU/HumanEval ────────────


def test_bench_score_prefers_arena_elo():
    """When Arena ELO is present, it wins over MMLU/HumanEval — Arena is
    the strongest cross-provider quality signal we have."""
    s = _bench_score({"Arena_ELO": 1380, "MMLU": 50.0, "HumanEval": 50.0})
    # 1380 / 1000 = 1.38, not the MMLU/HumanEval avg (0.5).
    assert s == 1.38


def test_bench_score_fallback_to_mmlu_humaneval():
    s = _bench_score({"MMLU": 90.0, "HumanEval": 80.0})
    import pytest

    assert s == pytest.approx(0.85)  # (0.9 + 0.8) / 2


def test_bench_score_returns_none_for_empty():
    assert _bench_score({}) is None
    assert _bench_score({"unknown_metric": 100}) is None


# ── Recency bonus: decay over 24 months ─────────────────────────────────────


def test_parse_release_date_handles_short_form():
    assert _parse_release_date("2026-03") == (2026, 3)


def test_parse_release_date_handles_iso():
    assert _parse_release_date("2026-03-15") == (2026, 3)


def test_parse_release_date_rejects_junk():
    assert _parse_release_date("not-a-date") is None
    assert _parse_release_date(None) is None


def test_recency_bonus_full_for_current_month():
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert _recency_bonus("2026-05", now=now) == 1.0


def test_recency_bonus_decays_linearly():
    """Released 12 months ago → halfway through the decay window → 0.5."""
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    bonus = _recency_bonus("2025-05", now=now)
    assert 0.48 < bonus < 0.52


def test_recency_bonus_zero_after_24_months():
    now = datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert _recency_bonus("2023-05", now=now) == 0.0
    assert _recency_bonus("2020-01", now=now) == 0.0


# ── Composite score_model: bench + recency stack ────────────────────────────


def test_score_model_combines_bench_and_recency():
    """Arena ELO 1380 (1.38) + 1.0 recency bonus = 2.38 max."""
    info = {
        "benchmarks": {"Arena_ELO": 1380},
        "release_date": datetime.now(timezone.utc).strftime("%Y-%m"),
    }
    s = score_model("any-id", info=info)
    assert s > 2.0  # bench (1.38) + recency (~1.0) = ~2.38


def test_score_model_unranked_model_still_competitive_when_fresh():
    """A brand-new model with NO benchmark but a flagship tier must
    still rank above an older budget model — this is the lever that
    keeps fresh flagships visible."""
    fresh = {
        "tier": "flagship",
        "release_date": datetime.now(timezone.utc).strftime("%Y-%m"),
        "benchmarks": {},
    }
    old_budget = {
        "tier": "budget",
        "release_date": "2023-01",
        "benchmarks": {"Arena_ELO": 1100},  # mediocre
    }
    s_fresh = score_model("fresh-flagship", info=fresh)
    s_old = score_model("old-budget", info=old_budget)
    assert s_fresh > s_old


def test_score_model_handles_missing_info():
    """No info → still returns a finite score (no exception)."""
    s = score_model("totally-unknown-id", info=None)
    assert isinstance(s, float)
    assert s >= 0.0


# ── top_n_per_provider: groups by owned_by, ranks within group ──────────────


def _make(mid: str, owner: str) -> dict:
    return {"id": mid, "owned_by": owner}


def test_top_n_disabled_returns_input_unchanged(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_TOP_N_PER_PROVIDER", "0")
    models = [_make("a:m1", "a"), _make("b:m1", "b")]
    out = top_n_per_provider(models)
    assert out == models


def test_top_n_keeps_only_n_per_provider(monkeypatch):
    """Two providers × 15 models each, n=3 → 6 models out total."""
    monkeypatch.delenv("PLUGBOARD_TOP_N_PER_PROVIDER", raising=False)
    models = [_make(f"providerA:model-{i:02d}", "providerA") for i in range(15)] + [
        _make(f"providerB:model-{i:02d}", "providerB") for i in range(15)
    ]
    out = top_n_per_provider(models, n=3)
    # Group by owned_by and verify each has at most 3.
    from collections import Counter

    counts = Counter(m["owned_by"] for m in out)
    assert counts["providerA"] == 3
    assert counts["providerB"] == 3
    assert len(out) == 6


def test_top_n_respects_per_provider_grouping(monkeypatch):
    """Two providers must each get their own top-N — the trim is local
    to a provider, not global. Even if providerB's models all score
    lower than providerA's, providerB must still get N entries (or
    fewer if it has fewer)."""
    monkeypatch.delenv("PLUGBOARD_TOP_N_PER_PROVIDER", raising=False)
    models = [
        _make("providerA:a1", "providerA"),
        _make("providerA:a2", "providerA"),
        _make("providerB:b1", "providerB"),
    ]
    out = top_n_per_provider(models, n=10)
    # All three kept — none exceeds the per-provider top-10.
    assert len(out) == 3


def test_top_n_emits_best_first_within_provider(monkeypatch):
    """Within each provider group, the highest-scoring model must come
    first — this is what the dashboard renders to the user, so visual
    ranking and computed score must agree."""
    monkeypatch.delenv("PLUGBOARD_TOP_N_PER_PROVIDER", raising=False)
    # Force scores via fake info: m_high (flagship + version 9.9) wins.
    models = [
        _make("p:m_low", "p"),
        _make("p:m_high-9.9", "p"),
    ]
    out = top_n_per_provider(models, n=2)
    assert out[0]["id"] == "p:m_high-9.9"
    assert out[1]["id"] == "p:m_low"


def test_top_n_provider_order_follows_first_seen(monkeypatch):
    """Provider groups are emitted in the order they first appeared in
    the input — the dashboard sections stay where the user expects."""
    monkeypatch.delenv("PLUGBOARD_TOP_N_PER_PROVIDER", raising=False)
    models = [
        _make("provB:b1", "provB"),
        _make("provA:a1", "provA"),
        _make("provB:b2", "provB"),
    ]
    out = top_n_per_provider(models, n=5)
    owners = [m["owned_by"] for m in out]
    # provB came first, so it must appear before provA in the result.
    assert owners.index("provB") < owners.index("provA")


def test_top_n_env_var_overrides_default(monkeypatch):
    """PLUGBOARD_TOP_N_PER_PROVIDER must override the default (no cap)
    so operators can tune without redeploying."""
    monkeypatch.setenv("PLUGBOARD_TOP_N_PER_PROVIDER", "2")
    models = [_make(f"p:m{i}", "p") for i in range(8)]
    out = top_n_per_provider(models)
    assert len(out) == 2


def test_top_n_invalid_env_falls_back_to_default(monkeypatch):
    """A junk value in the env var must not crash — fall back to default."""
    monkeypatch.setenv("PLUGBOARD_TOP_N_PER_PROVIDER", "not-a-number")
    models = [_make(f"p:m{i}", "p") for i in range(12)]
    out = top_n_per_provider(models)
    # Default: no cap
    assert len(out) == 12


def test_top_n_groups_ollama_by_family(monkeypatch):
    """Ollama provider models must be grouped by family (e.g. Qwen, DeepSeek, Kimi)
    and sorted from best/most recent to oldest/worst within each family."""
    monkeypatch.delenv("PLUGBOARD_TOP_N_PER_PROVIDER", raising=False)

    # We create a list of interleaved Qwen, Kimi, and DeepSeek models
    models = [
        _make("ollama:deepseek-v3.2:cloud", "ollama"),
        _make("ollama:qwen3-coder-next:cloud", "ollama"),
        _make("ollama:kimi-k2.5:cloud", "ollama"),
        _make("ollama:deepseek-v3.1:671b-cloud", "ollama"),
        _make("ollama:qwen3.5:397b-cloud", "ollama"),
        _make("ollama:kimi-k2-thinking:cloud", "ollama"),
    ]

    # Let's run top_n_per_provider
    out = top_n_per_provider(models, n=10)

    # Verify they are perfectly grouped by family!
    out_ids = [m["id"] for m in out]

    # Check that Qwen, DeepSeek, and Kimi models are grouped together.
    assert "qwen" in out_ids[0]
    assert "qwen" in out_ids[1]

    assert "deepseek" in out_ids[2]
    assert "deepseek" in out_ids[3]

    assert "kimi" in out_ids[4]
    assert "kimi" in out_ids[5]

    # Check that within each family, they are sorted from best/most recent to oldest
    assert out_ids[0] == "ollama:qwen3.5:397b-cloud"
    assert out_ids[1] == "ollama:qwen3-coder-next:cloud"

    assert out_ids[2] == "ollama:deepseek-v3.1:671b-cloud"
    assert out_ids[3] == "ollama:deepseek-v3.2:cloud"

    assert out_ids[4] == "ollama:kimi-k2.5:cloud"
    assert out_ids[5] == "ollama:kimi-k2-thinking:cloud"
