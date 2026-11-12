"""Every CLI receives the tenant prompt on stdin, never in its argv.

An argument is readable by any local user in the process list
(`/proc/<pid>/cmdline`), and a single argument over ~128 KiB makes the exec
fail with "Argument list too long". The fake binaries below are `sh` scripts
that record their own command line and their stdin, then answer like the
real CLI would.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from plugboard import cli_subprocess

KINDS = ("codex", "gemini", "agy", "claude")
MARKER = "PLUGBOARD-STDIN-MARKER-7f3a"
BIG = 200 * 1024

# Valid answers, as the existing CLI tests already use them.
_OK_STDOUT = {
    "claude": '{"type":"result","result":"OK"}',
    "codex": '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}',
    "gemini": '{"response":"OK"}',
    "agy": '{"status":"SUCCESS","response":"OK","usage":{}}',
}


@pytest.fixture
def cli_sandbox(tmp_path, monkeypatch):
    """Private spawn cwd and operator home; no real CLI can be resolved."""
    spawn_cwd = tmp_path / "spawn"
    spawn_cwd.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PLUGBOARD_SUBPROCESS_CWD", str(spawn_cwd))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(home))
    monkeypatch.setattr(cli_subprocess, "_binary_path_cache", {})
    return tmp_path


def _install(tmp_path: Path, monkeypatch, kind: str, body: str) -> Path:
    """Write an executable `sh` script and make `_resolve_binary` return it."""
    script = tmp_path / f"fake-{kind}"
    script.write_text("#!/bin/sh\nPATH=/usr/bin:/bin\n" + body)
    script.chmod(0o755)
    monkeypatch.setattr(cli_subprocess, "_resolve_binary", lambda _cmd: str(script))
    return script


def _recording_cli(tmp_path: Path, monkeypatch, kind: str):
    """Fake CLI that copies its cmdline and stdin to files, then answers OK."""
    args_file = tmp_path / f"{kind}.cmdline"
    stdin_file = tmp_path / f"{kind}.stdin"
    _install(
        tmp_path,
        monkeypatch,
        kind,
        f"cat /proc/$$/cmdline > '{args_file}'\n"
        f"cat > '{stdin_file}'\n"
        f"cat <<'EOF'\n{_OK_STDOUT[kind]}\nEOF\n",
    )
    return args_file, stdin_file


def _body(text: str) -> dict:
    return {"model": "x-model", "messages": [{"role": "user", "content": text}]}


# ── Declared shape ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
def test_every_backend_reads_the_prompt_on_stdin(kind):
    backend = cli_subprocess.get_backend(kind)
    assert backend.input_mode == "stdin"
    assert not any("{prompt}" in arg for arg in backend.args), backend.args


def test_gemini_argv_has_no_prompt_option_and_no_sandbox():
    args = cli_subprocess.get_backend("gemini").args
    for option in ("--prompt", "-p", "--sandbox", "-s"):
        assert option not in args, args
    # Its sandbox would copy stdin back into the arguments.
    assert not any(a.startswith(("--prompt=", "--sandbox=")) for a in args), args


def test_agy_argv_has_no_print_or_prompt_option():
    args = cli_subprocess.get_backend("agy").args
    for option in ("-p", "--print", "--prompt"):
        assert option not in args, args


def test_codex_reads_stdin_through_a_trailing_dash():
    assert cli_subprocess.get_backend("codex").stdin_arg == "-"
    for kind in ("gemini", "agy", "claude"):
        assert cli_subprocess.get_backend(kind).stdin_arg is None


# ── Real child process ───────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", KINDS)
async def test_prompt_reaches_stdin_and_stays_out_of_the_command_line(
    cli_sandbox, monkeypatch, kind
):
    args_file, stdin_file = _recording_cli(cli_sandbox, monkeypatch, kind)
    result = await cli_subprocess.complete(kind, _body(f"say {MARKER} back"))
    assert result["choices"][0]["message"]["content"] == "OK"
    cmdline = args_file.read_bytes()
    assert MARKER.encode() not in cmdline
    assert MARKER.encode() in stdin_file.read_bytes()
    if kind == "codex":
        assert cmdline.rstrip(b"\0").split(b"\0")[-1] == b"-"


@pytest.mark.parametrize("kind", ("gemini", "codex", "agy"))
async def test_a_200_kib_prompt_is_delivered_whole(cli_sandbox, monkeypatch, kind):
    _args_file, stdin_file = _recording_cli(cli_sandbox, monkeypatch, kind)
    result = await cli_subprocess.complete(kind, _body("x" * BIG))
    assert result["choices"][0]["message"]["content"] == "OK"
    received = stdin_file.read_bytes()
    assert len(received) >= BIG
    assert received.count(b"x") >= BIG


@pytest.mark.parametrize("kind", KINDS)
async def test_accents_and_emoji_arrive_unchanged(cli_sandbox, monkeypatch, kind):
    text = "Réponds en français : déjà vu, naïve, Ωmega, 中文, 🚀🎉"
    _args_file, stdin_file = _recording_cli(cli_sandbox, monkeypatch, kind)
    await cli_subprocess.complete(kind, _body(text))
    assert text in stdin_file.read_bytes().decode("utf-8")


@pytest.mark.parametrize("kind", KINDS)
async def test_cli_exiting_without_reading_stdin_fails_cleanly(
    cli_sandbox, monkeypatch, kind
):
    _install(cli_sandbox, monkeypatch, kind, "exit 3\n")
    with pytest.raises(cli_subprocess.CliSubprocessError) as excinfo:
        await asyncio.wait_for(cli_subprocess.complete(kind, _body("y" * BIG)), 20)
    assert excinfo.value.exit_code == 3
    assert not isinstance(
        excinfo.value.__cause__, (BrokenPipeError, ConnectionResetError)
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_empty_messages_spawn_nothing(cli_sandbox, monkeypatch, kind):
    spawned = []

    async def fake_exec(*argv, **kwargs):
        spawned.append(argv)
        raise AssertionError("no process may be started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match="Empty prompt"):
        await cli_subprocess.complete(kind, {"model": "x-model", "messages": []})
    assert spawned == []
