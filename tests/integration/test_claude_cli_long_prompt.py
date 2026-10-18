"""Integration test — BENCH-001 : claude-cli must not return HTTP 500 on long prompts.

Simulates a 12 000-character prompt and verifies that the JSONL parser
correctly extracts assistant text (including ``thinking`` blocks) instead
of raising "returned empty content".
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugboard import cli_subprocess
from plugboard.cli_subprocess import complete


LONG_PROMPT = "A" * 12000


def _fake_claude_jsonl_response(final_text: str):
    """Build stdout bytes that mimic Claude CLI ``--output-format stream-json``."""
    events = [
        {
            "type": "system",
            "subtype": "init",
            "cwd": "/tmp",
            "session_id": "test-session",
        },
        {
            "type": "assistant",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-20250514",
                "content": [{"type": "text", "text": final_text}],
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
        },
    ]
    return "\n".join(json.dumps(e) for e in events).encode("utf-8")


@pytest.fixture(autouse=True)
def _mock_claude_cli(monkeypatch: pytest.MonkeyPatch):
    """Make ``claude`` appear installed and wire a fake subprocess."""
    monkeypatch.setattr(
        cli_subprocess.shutil,
        "which",
        lambda cmd: "/fake/bin/claude" if cmd == "claude" else None,
    )

    async def fake_probe(kind, timeout_s=5.0):
        return {"ok": True, "version": "0.99.0-test", "error": None}

    monkeypatch.setattr(cli_subprocess, "probe_cli_version", fake_probe)

    async def fake_exec(*argv, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 12345
        stdout = _fake_claude_jsonl_response("Acknowledged.")
        proc.communicate = AsyncMock(return_value=(stdout, b""))
        proc.kill = MagicMock()
        return proc

    monkeypatch.setattr(
        cli_subprocess.asyncio,
        "create_subprocess_exec",
        fake_exec,
    )


@pytest.mark.asyncio
async def test_claude_complete_long_prompt():
    """A 12 k-char prompt must be accepted and yield non-empty text."""
    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": LONG_PROMPT}],
    }
    result = await complete("claude", body)
    assert result["choices"][0]["message"]["content"] == "Acknowledged."
    assert result["model"] == "claude-cli:claude-sonnet-4-20250514"


@pytest.mark.asyncio
async def test_claude_complete_thinking_only_response():
    """If the CLI returns only ``thinking`` blocks we must still surface them
    rather than a cryptic "returned empty content" error (BENCH-001)."""
    # Override the fixture for this test only
    thinking_stdout = (
        '{"type":"system","subtype":"init"}\n'
        '{"type":"assistant","message":{"content":['
        '{"type":"thinking","thinking":"Only thinking."}]}}\n'
        '{"type":"result","subtype":"success","is_error":false,"result":"Only thinking."}\n'
    ).encode("utf-8")

    async def fake_exec(*argv, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 12346
        proc.communicate = AsyncMock(return_value=(thinking_stdout, b""))
        proc.kill = MagicMock()
        return proc

    # Patch the global asyncio reference used by cli_subprocess
    import plugboard.cli_subprocess as _csp

    _csp.asyncio.create_subprocess_exec = fake_exec

    body = {
        "model": "claude-sonnet-4-20250514",
        "messages": [{"role": "user", "content": "think hard"}],
    }
    result = await complete("claude", body)
    assert result["choices"][0]["message"]["content"] == "Only thinking."
