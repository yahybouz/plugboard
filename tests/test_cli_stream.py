"""Real streaming of a CLI answer: deltas leave while the process still runs (INC-2)."""

from __future__ import annotations

import json
import stat
import time

import pytest

from plugboard import cli_subprocess
from plugboard.cli_subprocess import CliSubprocessError

BODY = {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}]}


def _delta(text: str) -> str:
    return json.dumps(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        }
    )


FINAL = [
    json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Bonjour"}]},
        }
    ),
    json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": "Bonjour"}
    ),
]


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    """Install a script as the CLI binary; it logs its argv and plays ``lines``."""

    def install(lines, *, pause=0.0, exit_code=0):
        script = tmp_path / "fakecli"
        argv_log = tmp_path / "argv.txt"
        body = "\n".join(
            f"printf '%s\\n' '{ln}'" + (f"; sleep {pause}" if i == 0 else "")
            for i, ln in enumerate(lines)
        )
        script.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_log}\ncat >/dev/null\n{body}\nexit {exit_code}\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setattr(
            cli_subprocess, "_resolve_binary", lambda command: str(script)
        )
        return argv_log

    return install


async def test_claude_deltas_arrive_before_the_process_ends(fake_cli):
    fake_cli([_delta("Bon"), _delta("jour"), *FINAL], pause=0.8)
    seen: list[tuple[float, str]] = []
    t0 = time.perf_counter()
    result = await cli_subprocess.complete(
        "claude", BODY, on_delta=lambda t: seen.append((time.perf_counter() - t0, t))
    )
    total = time.perf_counter() - t0
    assert [t for _, t in seen] == ["Bon", "jour"]
    assert seen[0][0] < total - 0.5  # first word long before the end
    assert result["choices"][0]["message"]["content"] == "Bonjour"


async def test_partial_messages_flag_only_when_streaming(fake_cli):
    argv_log = fake_cli([*FINAL])
    await cli_subprocess.complete("claude", BODY)
    assert "--include-partial-messages" not in argv_log.read_text()
    await cli_subprocess.complete("claude", BODY, on_delta=lambda t: None)
    assert "--include-partial-messages" in argv_log.read_text()


async def test_cli_without_incremental_output_never_calls_back(fake_cli):
    fake_cli(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "42"},
                }
            )
        ]
    )
    seen: list[str] = []
    result = await cli_subprocess.complete(
        "codex",
        {"model": "gpt-5.5", "messages": BODY["messages"]},
        on_delta=seen.append,
    )
    assert seen == []
    assert result["choices"][0]["message"]["content"] == "42"


async def test_error_ending_keeps_the_usual_exception(fake_cli):
    err = json.dumps(
        {"type": "result", "subtype": "error", "is_error": True, "result": "boom"}
    )
    fake_cli([_delta("Bon"), err], exit_code=1)
    seen: list[str] = []
    with pytest.raises(CliSubprocessError, match="boom"):
        await cli_subprocess.complete("claude", BODY, on_delta=seen.append)
    assert seen == ["Bon"]


async def test_a_failing_callback_does_not_break_the_answer(fake_cli):
    fake_cli([_delta("Bon"), *FINAL])

    def bad(_t):
        raise RuntimeError("consumer gone")

    result = await cli_subprocess.complete("claude", BODY, on_delta=bad)
    assert result["choices"][0]["message"]["content"] == "Bonjour"


# ── agy: stream-json (shape captured on the UAT, agy 1.4.4) ─────────────────


def _agy_step(
    text: str, state: str = "ACTIVE", step_type: str = "agent_response"
) -> str:
    return json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "c",
                "step_index": 1,
                "state": state,
                "step_type": step_type,
                "text_delta": text,
            },
        }
    )


AGY_RESULT = json.dumps(
    {
        "event": "result",
        "result": {
            "conversation_id": "c",
            "status": "SUCCESS",
            "response": "Bonjour !\n",
            "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
        },
    }
)
AGY_LINES = [
    json.dumps({"event": "init", "init": {"model": "m"}}),
    _agy_step("ignored", "DONE", "user_input"),
    _agy_step("Bonjour"),
    _agy_step(" !\n", "DONE"),
    AGY_RESULT,
]


async def test_agy_streams_agent_response_deltas(fake_cli):
    argv_log = fake_cli(AGY_LINES, pause=0.6)
    seen: list[str] = []
    result = await cli_subprocess.complete(
        "agy",
        {"model": "gemini-3.8-flash-low", "messages": BODY["messages"]},
        on_delta=seen.append,
    )
    assert seen == ["Bonjour", " !\n"]
    assert result["choices"][0]["message"]["content"] == "Bonjour !\n"
    assert result["usage"]["total_tokens"] == 9  # reported counts survive
    argv = argv_log.read_text().split("\n")
    assert argv[argv.index("--output-format") + 1] == "stream-json"


async def test_agy_without_callback_keeps_json_mode(fake_cli):
    argv_log = fake_cli([json.dumps(json.loads(AGY_RESULT)["result"])])
    result = await cli_subprocess.complete(
        "agy", {"model": "gemini-3.8-flash-low", "messages": BODY["messages"]}
    )
    argv = argv_log.read_text().split("\n")
    assert argv[argv.index("--output-format") + 1] == "json"
    assert result["choices"][0]["message"]["content"] == "Bonjour !\n"


async def test_agy_error_result_is_classified_as_before(fake_cli):
    err = json.dumps(
        {
            "event": "result",
            "result": {"status": "ERROR", "error": "Resource has been exhausted"},
        }
    )
    fake_cli([json.dumps({"event": "init", "init": {}}), err], exit_code=3)
    with pytest.raises(CliSubprocessError) as info:
        await cli_subprocess.complete(
            "agy", {"model": "m", "messages": BODY["messages"]}, on_delta=lambda t: None
        )
    assert info.value.quota is True


async def test_agy_non_json_line_with_exit_0_is_an_error(fake_cli):
    # Captured on the UAT: an empty stdin made agy print this and exit 0.
    fake_cli(["CLI error: bubbletea: error opening TTY"], exit_code=0)
    with pytest.raises(CliSubprocessError, match="invalid JSON"):
        await cli_subprocess.complete(
            "agy", {"model": "m", "messages": BODY["messages"]}, on_delta=lambda t: None
        )


async def test_agy_is_never_launched_with_an_empty_prompt(fake_cli):
    argv_log = fake_cli([AGY_RESULT])
    with pytest.raises(RuntimeError, match="Empty prompt"):
        await cli_subprocess.complete(
            "agy", {"model": "m", "messages": [{"role": "user", "content": "  "}]}
        )
    assert not argv_log.exists()


async def test_agy_invalid_model_result_is_an_error(fake_cli):
    err = json.dumps(
        {
            "event": "result",
            "result": {
                "conversation_id": "",
                "status": "ERROR",
                "response": "",
                "error": "invalid model selection x",
            },
        }
    )
    fake_cli([err], exit_code=1)
    with pytest.raises(CliSubprocessError, match="invalid model selection"):
        await cli_subprocess.complete(
            "agy", {"model": "x", "messages": BODY["messages"]}, on_delta=lambda t: None
        )
