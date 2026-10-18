"""TNR — W004 : Gemini CLI non-zero exit recovery.

Gemini CLI v0.41+ occasionally exits with code 1 while still emitting a
perfectly valid JSON response on stdout. This happens when stderr contains
non-fatal warnings such as:
  - "Warning: ripgrep not available"
  - "Warning: EPERM /tmp/TemporaryItems/..."
  - "Warning: Basic terminal detected"

The fix (W004, 2026-05-13) recovers the response instead of raising when:
  - kind == "gemini" (recovery is scoped to Gemini only)
  - stdout is non-empty and parses to a valid response

This test file covers:
  1. Recovery path: exit=1 + valid Gemini JSON stdout → returns completion
  2. No-recovery path: exit=1 + invalid/empty stdout → raises CliSubprocessError
  3. No-recovery for other backends: exit=1 + valid JSON stdout, kind=codex → raises
  4. Successful baseline: exit=0 + valid JSON → returns completion (regression guard)
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugboard.cli_subprocess import (
    CLI_BACKENDS,
    CliSubprocessError,
    _try_parse_stdout_text,
    complete,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GEMINI_VALID_JSON = json.dumps(
    {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "This is the real LLM answer."}],
                    "role": "model",
                }
            }
        ]
    }
)

_GEMINI_VALID_JSON_FLAT = json.dumps({"response": "Flat response text."})

_STDERR_WARNINGS = (
    "Warning: ripgrep not available — file search disabled\n"
    "Warning: EPERM /tmp/com.apple.launchd.XYZ/TemporaryItems\n"
)


def _make_proc(*, returncode: int, stdout: bytes, stderr: bytes) -> MagicMock:
    """Build a minimal mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 12345
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# Unit tests for _try_parse_stdout_text
# ---------------------------------------------------------------------------


def test_try_parse_stdout_text_valid_gemini_json():
    backend = CLI_BACKENDS["gemini"]
    result = _try_parse_stdout_text(_GEMINI_VALID_JSON, backend, "gemini")
    assert result == "This is the real LLM answer."


def test_try_parse_stdout_text_valid_gemini_json_flat():
    backend = CLI_BACKENDS["gemini"]
    result = _try_parse_stdout_text(_GEMINI_VALID_JSON_FLAT, backend, "gemini")
    assert result == "Flat response text."


def test_try_parse_stdout_text_invalid_json_returns_none():
    backend = CLI_BACKENDS["gemini"]
    result = _try_parse_stdout_text("this is not json at all !!!", backend, "gemini")
    assert result is None


def test_try_parse_stdout_text_empty_returns_none():
    backend = CLI_BACKENDS["gemini"]
    result = _try_parse_stdout_text("", backend, "gemini")
    assert result is None


def test_try_parse_stdout_text_json_no_text_field_returns_stringified():
    """JSON without text/candidates/error → fallback to json.dumps(payload).

    A neutral payload (no "error" key, no "candidates", no "text") still falls
    through to json.dumps(payload) in _extract_text_from_gemini_json. This
    documents the existing behaviour: the W004 guard only fires on payloads
    that *also* carry an "error" key.
    """
    backend = CLI_BACKENDS["gemini"]
    payload_str = json.dumps({"some_unrelated_key": 42})
    result = _try_parse_stdout_text(payload_str, backend, "gemini")
    # No "error" key → falls through to json.dumps(payload) — not None.
    assert result is not None
    assert "some_unrelated_key" in result


def test_try_parse_stdout_text_error_payload_returns_none():
    """W004: payload {"error": {...}} with no candidates/text → returns None.

    This is the core of the W004 fix: a Gemini error blob like
    {"error": {"code": 429, "message": "Resource exhausted"}} must NOT be
    accepted as a valid LLM response. _try_parse_stdout_text must return None
    so the caller raises CliSubprocessError.
    """
    backend = CLI_BACKENDS["gemini"]
    error_payload = json.dumps(
        {"error": {"code": 429, "message": "Resource exhausted"}}
    )
    result = _try_parse_stdout_text(error_payload, backend, "gemini")
    assert result is None, (
        f"Expected None for Gemini error payload, got: {result!r}. "
        "The error blob must not be accepted as an LLM response."
    )


def test_try_parse_stdout_text_error_payload_with_candidates_returns_text():
    """W004: payload with both "error" AND valid "candidates" → recovery accepted.

    If Gemini ever returns a hybrid payload (error metadata alongside a real
    response), we should still extract the text from candidates rather than
    discarding the whole response.
    """
    backend = CLI_BACKENDS["gemini"]
    hybrid_payload = json.dumps(
        {
            "error": {"code": 200, "message": "partial warning"},
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Valid answer despite error field."}],
                        "role": "model",
                    }
                }
            ],
        }
    )
    result = _try_parse_stdout_text(hybrid_payload, backend, "gemini")
    assert result == "Valid answer despite error field.", (
        f"Expected candidate text to be extracted when present alongside error key. Got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Integration tests for complete() — mocked subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gemini_exit1_valid_json_recovers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """exit=1 + valid Gemini JSON stdout → completion returned, WARNING logged."""
    import plugboard.cli_subprocess as mod

    monkeypatch.setattr(mod, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    proc = _make_proc(
        returncode=1,
        stdout=_GEMINI_VALID_JSON.encode(),
        stderr=_STDERR_WARNINGS.encode(),
    )
    monkeypatch.setattr(
        mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    with caplog.at_level(logging.WARNING, logger="plugboard.cli_subprocess"):
        result = await complete(
            "gemini",
            {
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert result["choices"][0]["message"]["content"] == "This is the real LLM answer."
    assert result["object"] == "chat.completion"

    # A WARNING must have been emitted mentioning the recovery.
    recovery_logs = [
        r
        for r in caplog.records
        if "W004" in r.message or "recovering" in r.message.lower()
    ]
    assert recovery_logs, (
        "Expected a WARNING log mentioning W004 recovery. "
        f"Got records: {[r.message for r in caplog.records]}"
    )
    assert caplog.records  # at least one log entry
    warn_msg = recovery_logs[0].message
    assert "exited 1" in warn_msg or "exited" in warn_msg


@pytest.mark.asyncio
async def test_gemini_exit1_invalid_json_raises(monkeypatch: pytest.MonkeyPatch):
    """exit=1 + unparseable stdout → CliSubprocessError raised (no recovery)."""
    import plugboard.cli_subprocess as mod

    monkeypatch.setattr(mod, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    proc = _make_proc(
        returncode=1,
        stdout=b"this is not json",
        stderr=_STDERR_WARNINGS.encode(),
    )
    monkeypatch.setattr(
        mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    with pytest.raises(CliSubprocessError) as exc_info:
        await complete(
            "gemini",
            {
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    err = exc_info.value
    assert err.exit_code == 1
    assert err.provider_name == "gemini"


@pytest.mark.asyncio
async def test_gemini_exit1_empty_stdout_raises(monkeypatch: pytest.MonkeyPatch):
    """exit=1 + empty stdout → CliSubprocessError raised (no recovery)."""
    import plugboard.cli_subprocess as mod

    monkeypatch.setattr(mod, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    proc = _make_proc(
        returncode=1,
        stdout=b"",
        stderr=b"something went very wrong\n",
    )
    monkeypatch.setattr(
        mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    with pytest.raises(CliSubprocessError) as exc_info:
        await complete(
            "gemini",
            {
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert exc_info.value.exit_code == 1


@pytest.mark.asyncio
async def test_codex_exit1_valid_json_raises_not_recoverable(
    monkeypatch: pytest.MonkeyPatch,
):
    """exit=1 for codex (non-Gemini) → CliSubprocessError even if stdout looks valid.

    Recovery is intentionally scoped to Gemini.  Codex exit != 0 always means
    a real failure; we must not swallow those errors.
    """
    import plugboard.cli_subprocess as mod

    monkeypatch.setattr(mod, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    # Craft a codex JSONL that looks like a successful response
    codex_jsonl = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "codex answer"},
        }
    )
    proc = _make_proc(
        returncode=1,
        stdout=codex_jsonl.encode(),
        stderr=b"Some codex warning\n",
    )
    monkeypatch.setattr(
        mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    with pytest.raises(CliSubprocessError) as exc_info:
        await complete(
            "codex",
            {"model": "gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert exc_info.value.exit_code == 1
    assert exc_info.value.provider_name == "codex"


@pytest.mark.asyncio
async def test_gemini_exit0_valid_json_baseline(monkeypatch: pytest.MonkeyPatch):
    """Regression guard: exit=0 + valid JSON still works after refactor."""
    import plugboard.cli_subprocess as mod

    monkeypatch.setattr(mod, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    proc = _make_proc(
        returncode=0,
        stdout=_GEMINI_VALID_JSON.encode(),
        stderr=b"",
    )
    monkeypatch.setattr(
        mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    result = await complete(
        "gemini",
        {"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert result["choices"][0]["message"]["content"] == "This is the real LLM answer."
    assert result["object"] == "chat.completion"
    assert "_cli_elapsed_s" in result


@pytest.mark.asyncio
async def test_gemini_exit1_error_payload_raises_not_recoverable(
    monkeypatch: pytest.MonkeyPatch,
):
    """W004: exit=1 + Gemini error JSON payload → CliSubprocessError, no recovery.

    A payload like {"error": {"code": 429, "message": "Resource exhausted"}}
    must NOT be treated as a valid response. _try_parse_stdout_text returns None
    for such payloads so complete() falls through to raise CliSubprocessError.
    """
    import plugboard.cli_subprocess as mod

    monkeypatch.setattr(mod, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    error_payload = json.dumps(
        {"error": {"code": 429, "message": "Resource exhausted"}}
    )
    proc = _make_proc(
        returncode=1,
        stdout=error_payload.encode(),
        stderr=b"429 Resource exhausted\n",
    )
    monkeypatch.setattr(
        mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    with pytest.raises(CliSubprocessError) as exc_info:
        await complete(
            "gemini",
            {
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    err = exc_info.value
    assert err.exit_code == 1
    assert err.provider_name == "gemini"


@pytest.mark.asyncio
async def test_gemini_exit1_truly_empty_stdout_raises(monkeypatch: pytest.MonkeyPatch):
    """exit=1 + whitespace-only stdout → CliSubprocessError raised (no recovery).

    When stdout is blank, _try_parse_stdout_text returns None (json.JSONDecodeError
    on empty string) and the code falls through to raise as before.
    """
    import plugboard.cli_subprocess as mod

    monkeypatch.setattr(mod, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    proc = _make_proc(
        returncode=1,
        stdout=b"   \n  ",  # only whitespace
        stderr=_STDERR_WARNINGS.encode(),
    )
    monkeypatch.setattr(
        mod.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )

    with pytest.raises(CliSubprocessError) as exc_info:
        await complete(
            "gemini",
            {
                "model": "gemini-2.5-flash",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert exc_info.value.exit_code == 1
