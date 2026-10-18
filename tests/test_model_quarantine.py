"""Tests for the model quarantine store + smoke identification logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugboard.model_quarantine import (
    QUARANTINE_THRESHOLD,
    get_quarantined_suffixes,
    get_state,
    is_account_unsupported_error,
    is_terminal_model_unavailable_error,
    manually_clear,
    record_account_unsupported,
    record_failure,
    record_success,
    record_terminal_unavailable,
)


# ── Quarantine store ────────────────────────────────────────────────────────


def test_empty_state_returns_empty_set(tmp_path: Path):
    """A missing file must be treated as empty, never an error."""
    qfile = tmp_path / "quarantine.json"
    assert get_quarantined_suffixes(qfile) == set()


def test_record_failure_bumps_counter(tmp_path: Path):
    qfile = tmp_path / "quarantine.json"
    entry = record_failure("claude-broken", reason="HTTP 404", path=qfile)
    assert entry["consecutive_fails"] == 1
    entry = record_failure("claude-broken", reason="HTTP 404", path=qfile)
    assert entry["consecutive_fails"] == 2
    # Below threshold → not yet quarantined
    assert get_quarantined_suffixes(qfile) == set()


def test_threshold_quarantines_model(tmp_path: Path):
    """Reaching ``QUARANTINE_THRESHOLD`` consecutive fails must hide the
    model from ``get_quarantined_suffixes``."""
    qfile = tmp_path / "quarantine.json"
    for _ in range(QUARANTINE_THRESHOLD):
        record_failure("claude-dead", path=qfile)
    quarantined = get_quarantined_suffixes(qfile)
    assert "claude-dead" in quarantined


def test_account_unsupported_quarantines_immediately(tmp_path: Path):
    qfile = tmp_path / "quarantine.json"
    entry = record_account_unsupported(
        "gpt-5.4-pro",
        reason=(
            "The 'gpt-5.4-pro' model is not supported when using Codex "
            "with a ChatGPT account."
        ),
        path=qfile,
    )

    assert entry["consecutive_fails"] == QUARANTINE_THRESHOLD
    assert entry["terminal_reason"] == "account_unsupported"
    assert get_quarantined_suffixes(qfile) == {"gpt-5.4-pro"}


def test_detects_codex_chatgpt_account_unsupported_error():
    assert is_account_unsupported_error(
        "The gpt-5.2-codex model is not supported for ChatGPT accounts."
    )


def test_terminal_model_unavailable_quarantines_immediately(tmp_path: Path):
    qfile = tmp_path / "quarantine.json"
    entry = record_terminal_unavailable(
        "claude-3-opus-20240229",
        reason="The model has been deprecated and will reach end-of-life.",
        terminal_reason="model_unavailable",
        path=qfile,
    )

    assert entry["consecutive_fails"] == QUARANTINE_THRESHOLD
    assert entry["terminal_reason"] == "model_unavailable"
    assert get_quarantined_suffixes(qfile) == {"claude-3-opus-20240229"}


def test_detects_deprecated_model_as_terminal():
    assert is_terminal_model_unavailable_error(
        "The model has been deprecated and will reach end-of-life."
    )


def test_ripgrep_warning_is_not_terminal_model_unavailable():
    assert not is_terminal_model_unavailable_error(
        "Ripgrep is not available. Falling back to grep."
    )


def test_record_success_rehabilitates_model(tmp_path: Path):
    """A single green probe must remove a model from quarantine, even
    after many fails — the smoke test is conservative enough that a
    success is a strong signal."""
    qfile = tmp_path / "quarantine.json"
    for _ in range(QUARANTINE_THRESHOLD + 2):
        record_failure("claude-flaky", path=qfile)
    assert "claude-flaky" in get_quarantined_suffixes(qfile)
    record_success("claude-flaky", path=qfile)
    assert "claude-flaky" not in get_quarantined_suffixes(qfile)


def test_manually_clear_targeted(tmp_path: Path):
    qfile = tmp_path / "quarantine.json"
    for _ in range(QUARANTINE_THRESHOLD):
        record_failure("model-a", path=qfile)
        record_failure("model-b", path=qfile)
    cleared = manually_clear("model-a", path=qfile)
    assert cleared == ["model-a"]
    assert get_quarantined_suffixes(qfile) == {"model-b"}


def test_manually_clear_all(tmp_path: Path):
    qfile = tmp_path / "quarantine.json"
    record_failure("model-a", path=qfile)
    record_failure("model-b", path=qfile)
    cleared = manually_clear(None, path=qfile)
    assert set(cleared) == {"model-a", "model-b"}
    assert get_state(qfile).get("models") == {}


def test_corrupt_state_file_does_not_raise(tmp_path: Path):
    """A junk JSON file at the quarantine path must not crash the boot
    of the proxy — degrade to "no quarantine" instead."""
    qfile = tmp_path / "quarantine.json"
    qfile.write_text("{not valid json")
    assert get_quarantined_suffixes(qfile) == set()


def test_state_with_non_dict_entries_is_ignored(tmp_path: Path):
    """If somebody hand-edits the file into an unexpected shape, only
    the well-formed rows should count."""
    qfile = tmp_path / "quarantine.json"
    qfile.write_text(
        json.dumps(
            {
                "models": {
                    "good": {"consecutive_fails": QUARANTINE_THRESHOLD},
                    "bad": "this is not a dict",
                    "below": {"consecutive_fails": 1},
                }
            }
        )
    )
    assert get_quarantined_suffixes(qfile) == {"good"}


# ── Identification analysis (smoke_models.py logic) ─────────────────────────


@pytest.fixture
def _smoke_module():
    """Import the smoke script as a module. Importing at module
    scope would force pytest to resolve the script path before fixtures
    run; doing it inside a fixture sidesteps that."""
    import importlib.util
    import sys

    script = Path(__file__).resolve().parent.parent / "scripts" / "smoke_models.py"
    spec = importlib.util.spec_from_file_location("smoke_models_test", script)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["smoke_models_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def _classify(_smoke_module):
    """Return the script's small test seam for response classification."""
    return _smoke_module._smoke_one_for_tests


def test_smoke_ok_when_reply_mentions_family(_classify):
    """A reply that mentions the model family must classify as ok, even
    if the version number is missing or rephrased."""
    status, suffix = _classify(
        "claude-via-claude-cli:claude-opus-4-7",
        "I am Claude Opus 4.7 from Anthropic.",
    )
    assert status == "ok"
    assert suffix == "claude-opus-4-7"


def test_smoke_ok_with_minimal_family_match(_classify):
    """Even a vague self-id like 'Claude' (no version) is OK — better
    than flagging every Claude model as mismatch when the LLM just
    didn't elaborate."""
    status, _ = _classify(
        "claude-via-claude-cli:claude-sonnet-4-6",
        "Hi! I am Claude, an AI assistant.",
    )
    assert status == "ok"


def test_smoke_mismatch_when_no_family_match(_classify):
    """If the reply doesn't mention the family at all, mark it mismatch
    (logged-only, not quarantined)."""
    status, _ = _classify(
        "claude-via-claude-cli:claude-opus-4-7",
        "I am GPT-4, an AI assistant by OpenAI.",
    )
    assert status == "mismatch"


def test_smoke_mismatch_when_reply_empty(_classify):
    status, _ = _classify("any:gpt-5", "")
    assert status == "mismatch"


def test_smoke_one_treats_empty_completion_as_failure(_smoke_module, monkeypatch):
    """A 200 response with no assistant text is a hard probe failure."""

    def fake_http_json(*_args, **_kwargs):
        return {"choices": [{"message": {"content": "   "}}], "usage": {}}

    monkeypatch.setattr(_smoke_module, "_http_json", fake_http_json)

    row = _smoke_module._one_call("provider:model", timeout=1.0)

    assert row["outcome"] == "fail"
    assert row["error"] == "empty completion"


def test_smoke_handles_dated_suffix(_classify):
    """The dated YYYYMMDD tail must be stripped before token matching —
    so ``claude-opus-4-7-20260416`` still matches a reply about
    ``Opus 4.7`` without the date."""
    status, _ = _classify(
        "owner:claude-opus-4-7-20260416",
        "I am claude opus 4.7, by Anthropic.",
    )
    assert status == "ok"
