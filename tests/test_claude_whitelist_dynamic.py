"""C1 (2026-06-01) — dynamic Claude whitelist: N latest (major,minor) per family,
parsed from discovered ids, replacing the old hardcoded version pins so a new
Opus/Sonnet/Haiku release surfaces with zero code edit."""
from __future__ import annotations

from plugboard.router import (
    _allowed_claude_suffixes,
    _is_whitelisted_model,
    _strip_date_tail,
)

_DISCOVERED = [
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
    "claude-opus-4-1", "claude-opus-4-0", "claude-opus-4-20250514",
    "claude-sonnet-4-6", "claude-sonnet-4-5", "claude-3-7-sonnet",
    "claude-haiku-4-5", "claude-haiku-4-5-20251001",
]


def test_strip_date_tail():
    assert _strip_date_tail("claude-opus-4-5-20251101") == "claude-opus-4-5"
    assert _strip_date_tail("claude-opus-4-8") == "claude-opus-4-8"
    assert _strip_date_tail("minimax-m3:cloud") == "minimax-m3:cloud"


def test_two_latest_opus_includes_new_release_no_code_edit():
    allowed = _allowed_claude_suffixes(_DISCOVERED, 2)
    # opus: the 2 latest by version → 4-8 (new) + 4-7; 4-6/4-5/4-1/4-0 drop
    assert "claude-opus-4-8" in allowed
    assert "claude-opus-4-7" in allowed
    assert "claude-opus-4-6" not in allowed
    assert "claude-opus-4-1" not in allowed


def test_two_latest_sonnet_and_haiku():
    allowed = _allowed_claude_suffixes(_DISCOVERED, 2)
    assert "claude-sonnet-4-6" in allowed and "claude-sonnet-4-5" in allowed
    assert "claude-3-7-sonnet" not in allowed  # 3rd latest sonnet → dropped
    assert "claude-haiku-4-5" in allowed


def test_dated_snapshot_of_top_version_is_allowed():
    """A YYYYMMDD snapshot of a top-N version stays allowed (dedupe collapses it)."""
    allowed = _allowed_claude_suffixes(_DISCOVERED, 2)
    assert "claude-haiku-4-5-20251001" in allowed  # same (4,5) as the top haiku


def test_dated_snapshot_without_minor_does_not_pollute():
    """C1 BLOCKER-1: stripping the date off 'claude-opus-4-20250514' leaves the
    incomplete 'claude-opus-4' (version (4,)) — it must be skipped, never become
    a family's ranked version nor get whitelisted itself."""
    allowed = _allowed_claude_suffixes(_DISCOVERED, 2)
    assert "claude-opus-4-20250514" not in allowed
    # the real top-2 opus is unaffected
    assert "claude-opus-4-8" in allowed and "claude-opus-4-7" in allowed


def test_n_configurable():
    allowed3 = _allowed_claude_suffixes(_DISCOVERED, 3)
    assert "claude-opus-4-6" in allowed3  # 3rd latest now included


def test_future_opus_4_9_would_surface_automatically():
    """The whole point: a hypothetical future release needs no code change."""
    discovered = _DISCOVERED + ["claude-opus-4-9"]
    allowed = _allowed_claude_suffixes(discovered, 2)
    assert "claude-opus-4-9" in allowed and "claude-opus-4-8" in allowed
    assert "claude-opus-4-7" not in allowed  # rotated out


def test_is_whitelisted_uses_precomputed_set():
    allowed = {"claude-opus-4-8", "claude-opus-4-7"}
    assert _is_whitelisted_model({"id": "claude-code:claude-opus-4-8"}, allowed) is True
    assert _is_whitelisted_model({"id": "claude-code:claude-opus-4-6"}, allowed) is False
    # non-claude provider untouched by the claude set
    assert _is_whitelisted_model({"id": "ollama:minimax-m3:cloud"}, allowed) is True


def test_is_whitelisted_fallback_when_no_set():
    """Direct call without precomputed set must not hide a Claude model."""
    assert _is_whitelisted_model({"id": "claude-code:claude-opus-4-8"}, None) is True


def test_model_filter_defaults_to_every_discovered_model(monkeypatch):
    from plugboard import router

    monkeypatch.delenv("PLUGBOARD_MODEL_FILTER", raising=False)
    assert router._model_filter_mode() == "all"
    monkeypatch.setenv("PLUGBOARD_MODEL_FILTER", "curated")
    assert router._model_filter_mode() == "curated"
    monkeypatch.setenv("PLUGBOARD_MODEL_FILTER", "something-else")
    assert router._model_filter_mode() == "all"
