"""codex and claude start exactly as in 4.14.1 (INC-4).

4.14.2 put agy in a bubblewrap cage (BUG-86). Its first image also put
Debian's ``bwrap`` on the image's PATH, and codex, which prefers a bwrap found
on PATH to the one it ships, then failed every call (``bwrap: Can't mount proc
on /proc``, production and UAT, 15/11). The gateway's own launch of codex and
claude did not change; this file pins it so that it never does by accident:

- the command line, environment, working directory and process group that
  ``complete()`` and ``probe_cli_version()`` give codex and claude are those
  of ``release/4.14.1`` (copied from it, not from the current code);
- the program started is the CLI found on PATH, never ``bwrap`` nor any
  argument of agy's cage, and PATH reaches the CLI as it is;
- the image's PATH holds no ``bwrap`` (see tests/test_ci_image_full.py).

4.14.4 puts tini as PID 1 of the container (BUG-113, BUG-123); the gateway's
launch of its CLIs must not change with it. gemini and agy are pinned too,
as ``release/4.14.2`` (v4.14.2, 2b4c5ae) launches them: gemini's command
line (its private home's admin policy), environment and working directory;
agy's command after its cage's ``--``, its environment and working
directory, and that it starts inside bwrap. The binds of agy's cage are
pinned by tests/test_agy_cage.py.

No CLI is run: ``asyncio.create_subprocess_exec`` is replaced by a recorder.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from plugboard import cli_subprocess as cs
from tests.test_ci_image_full import _full_stage, bwrap_files_in_image, image_path_dirs

# What complete() gave codex in 4.14.1 (release/4.14.1, a6ba149:
# CLI_BACKENDS["codex"].args, codex_isolation_args() with the default
# read-only sandbox on Linux, --model, then "-" for stdin). <SPAWN> is the
# private launch directory.
CODEX_4141 = [
    "exec",
    "--json",
    "--color",
    "never",
    "--ignore-user-config",
    "--ignore-rules",
    "--ephemeral",
    "--skip-git-repo-check",
    "-c",
    'default_permissions="plugboard_chat"',
    "-c",
    'permissions.plugboard_chat.filesystem={":root"="deny",":minimal"="read",'
    '"/etc"="deny","<SPAWN>"="read"}',
    "-c",
    "features.apps=false",
    "-c",
    "features.plugins=false",
    "-c",
    "features.remote_plugin=false",
    "-c",
    "features.hooks=false",
    "-c",
    "features.computer_use=false",
    "-c",
    "features.browser_use=false",
    "-c",
    "features.browser_use_external=false",
    "-c",
    "features.in_app_browser=false",
    "-c",
    "features.multi_agent=false",
    "-c",
    "features.skill_mcp_dependency_install=false",
    "-c",
    "features.memories=false",
    "--model",
    "m-1",
    "-",
]
# claude in 4.14.1; streamed, --include-partial-messages before --model.
CLAUDE_4141 = [
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--permission-mode",
    "default",
    "--tools",
    "",
    "--disable-slash-commands",
    "--strict-mcp-config",
    "--setting-sources",
    "",
    "--model",
    "m-1",
]
CLAUDE_STREAM_4141 = [
    *CLAUDE_4141[:-2],
    "--include-partial-messages",
    *CLAUDE_4141[-2:],
]
# The environment names each CLI got in 4.14.1 from the server environment
# set by the ``server`` fixture.
ENV_4141 = {
    "codex": {"CODEX_HOME", "HOME", "LANG", "NO_COLOR", "PATH", "TERM"},
    "claude": {"CLAUDE_CONFIG_DIR", "HOME", "LANG", "NO_COLOR", "PATH", "TERM"},
}
# Words of agy's cage (plugboard/agy_cage.py): none may reach codex or claude.
CAGE_WORDS = {
    "--unshare-user",
    "--unshare-pid",
    "--as-pid-1",
    "--die-with-parent",
    "--ro-bind",
    "--ro-bind-try",
    "--remount-ro",
    "--proc",
    "--chdir",
}
_BODY = {"model": "m-1", "messages": [{"role": "user", "content": "hi"}]}


class _Recorded(Exception):
    """Raised by the recorder instead of starting a process."""


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The server's environment (with secrets, an AGY_ variable and a bwrap
    on PATH next to the CLIs), the private launch directory, and a recorder
    in place of create_subprocess_exec."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("codex", "claude", "gemini", "agy", "bwrap"):
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    spawn = tmp_path / "spawn"
    spawn.mkdir(mode=0o700)
    for name in list(os.environ):
        monkeypatch.delenv(name)
    path = f"{bin_dir}:/usr/local/bin:/usr/bin:/bin"
    for name, value in {
        "PATH": path,
        "HOME": "/home/app",
        "LANG": "C.UTF-8",
        "PLUGBOARD_DATA": str(tmp_path / "data"),
        "PLUGBOARD_SKIP_DOTENV": "1",
        "ADMIN_KEY": "adm-secret-1",
        "API_KEY": "api-secret-2",
        "ENCRYPTION_KEY": "enc-secret-3",
        "AGY_SOMETHING": "agy-4",
        "CODEX_HOME": "/home/app/.codex",
        "CLAUDE_CONFIG_DIR": "/home/app/.claude",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cs, "_SPAWN_CWD_CACHE", str(spawn))
    monkeypatch.setattr(cs, "_binary_path_cache", {})
    calls: list = []

    async def recorder(*argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        raise _Recorded

    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", recorder)
    return {"bin": bin_dir, "spawn": str(spawn), "path": path, "calls": calls}


async def _launch(server, kind: str, stream: bool) -> dict:
    with pytest.raises(_Recorded):
        await cs.complete(
            kind, dict(_BODY), on_delta=(lambda _text: None) if stream else None
        )
    (call,) = server["calls"]
    return call


def _no_cage(argv: list) -> None:
    assert not any(os.path.basename(word) == "bwrap" for word in argv), argv
    assert not CAGE_WORDS & set(argv), argv


def _env_as_in_4141(env: dict, kind: str, server) -> None:
    assert set(env) == ENV_4141[kind]
    assert not [name for name in env if name.startswith("AGY_")]
    assert env["PATH"] == server["path"]
    assert (env["NO_COLOR"], env["TERM"]) == ("1", "dumb")


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_codex_starts_as_in_4141(server, stream):
    call = await _launch(server, "codex", stream)

    expected = [a.replace("<SPAWN>", server["spawn"]) for a in CODEX_4141]
    assert call["argv"] == [str(server["bin"] / "codex"), *expected]
    _no_cage(call["argv"])
    _env_as_in_4141(call["env"], "codex", server)
    assert call["cwd"] == server["spawn"]
    assert call["start_new_session"] is True
    pipe = asyncio.subprocess.PIPE
    assert (call["stdin"], call["stdout"], call["stderr"]) == (pipe, pipe, pipe)
    assert set(call) == {"argv", "stdin", "stdout", "stderr", "env", "cwd"} | {
        "start_new_session"
    }


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_claude_starts_as_in_4141(server, stream):
    call = await _launch(server, "claude", stream)

    args = CLAUDE_STREAM_4141 if stream else CLAUDE_4141
    assert call["argv"] == [str(server["bin"] / "claude"), *args]
    _no_cage(call["argv"])
    _env_as_in_4141(call["env"], "claude", server)
    assert call["cwd"] == server["spawn"]
    assert call["start_new_session"] is True


@pytest.mark.parametrize("kind", ["codex", "claude"])
async def test_version_probe_starts_as_in_4141(server, kind):
    """``probe_cli_version`` (Verify CLI, the watchdog): ``<cli> --version``
    by its name, as in 4.14.1, never through the cage."""
    probe = await cs.probe_cli_version(kind)

    assert probe["ok"] is False  # the recorder raised instead of starting
    (call,) = server["calls"]
    assert call["argv"] == [kind, "--version"]
    _env_as_in_4141(call["env"], kind, server)
    assert call["cwd"] == server["spawn"]
    assert call["start_new_session"] is True
    assert set(call) == {"argv", "stdout", "stderr", "env", "cwd", "start_new_session"}


def test_the_image_path_holds_no_bwrap():
    """codex keeps the bwrap it ships only when none is on PATH (INC-4)."""
    files = bwrap_files_in_image(_full_stage())
    path = image_path_dirs()

    assert [f for f in files if os.path.dirname(f) in path] == []


# ── gemini and agy, as in 4.14.2 (release/4.14.2, 2b4c5ae) ─────────────────
# gemini: CLI_BACKENDS["gemini"].args, --model, then the admin policy of its
# private home (gemini_isolation_args). <GHOME> is that home.
GEMINI_4142 = [
    "--skip-trust",
    "-e",
    "none",
    "--allowed-mcp-server-names",
    "__plugboard_no_mcp__",
    "--approval-mode",
    "default",
    "--output-format",
    "json",
    "--model",
    "m-1",
    "--admin-policy",
    "<GHOME>/deny-all-tools.toml",
]
# agy: what follows the cage's "--" after its binary; streamed, its output
# format is stream-json.
AGY_4142 = [
    "--output-format",
    "json",
    "--disable-slash-commands",
    "--sandbox",
    "--model",
    "m-1",
]
AGY_STREAM_4142 = ["--output-format", "stream-json", *AGY_4142[2:]]
ENV_4142 = {
    "gemini": {
        "COLORTERM",
        "GEMINI_CLI_HOME",
        "GEMINI_CLI_SYSTEM_DEFAULTS_PATH",
        "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
        "GEMINI_CLI_TRUST_WORKSPACE",
        "HOME",
        "LANG",
        "NO_COLOR",
        "PATH",
        "TERM",
    },
    "agy": {
        "AGY_CLI_DISABLE_AUTO_UPDATE",
        "HOME",
        "LANG",
        "NO_COLOR",
        "PATH",
        "TERM",
        "TMPDIR",
    },
}
# What every agy launch keeps of its cage (the binds: tests/test_agy_cage.py).
AGY_CAGE_FLAGS = ("--unshare-user", "--unshare-pid", "--as-pid-1", "--die-with-parent")


@pytest.fixture
def agy_home(server, tmp_path, monkeypatch):
    """agy set up for the server account (its store, ``~/.gemini/config`` and
    ``projects.json``), the fake ``bwrap`` of ``server`` found on PATH."""
    from plugboard import agy_cage

    home = tmp_path / "home"
    store = home / ".gemini" / "antigravity-cli"
    store.mkdir(parents=True)
    (store / "antigravity-oauth-token").write_text("token\n")
    (home / ".gemini" / "config" / "projects").mkdir(parents=True)
    (home / ".gemini" / "projects.json").write_text("{}\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(agy_cage, "GATEWAY_BWRAP", str(tmp_path / "no-gateway-bwrap"))
    agy_cage._reset_for_tests()
    yield home
    agy_cage._reset_for_tests()


def _env_names(env: dict, kind: str, server) -> None:
    assert set(env) == ENV_4142[kind]
    assert not [
        name
        for name in env
        if name.startswith("AGY_") and name != ("AGY_CLI_DISABLE_AUTO_UPDATE")
    ]
    assert env["PATH"] == server["path"]
    assert env["NO_COLOR"] == "1"


async def test_gemini_starts_as_in_4142(server):
    call = await _launch(server, "gemini", stream=False)

    env = call["env"]
    ghome = env["GEMINI_CLI_HOME"]
    expected = [a.replace("<GHOME>", ghome) for a in GEMINI_4142]
    assert call["argv"] == [str(server["bin"] / "gemini"), *expected]
    _no_cage(call["argv"])
    _env_names(env, "gemini", server)
    assert (env["TERM"], env["COLORTERM"]) == ("xterm-256color", "truecolor")
    assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    assert env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] == f"{ghome}/system-settings.json"
    assert env["GEMINI_CLI_SYSTEM_DEFAULTS_PATH"] == f"{ghome}/system-defaults.json"
    assert call["cwd"] == server["spawn"]
    assert call["start_new_session"] is True
    pipe = asyncio.subprocess.PIPE
    assert (call["stdin"], call["stdout"], call["stderr"]) == (pipe, pipe, pipe)


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_agy_starts_in_its_cage_as_in_4142(server, agy_home, stream):
    call = await _launch(server, "agy", stream)

    argv, env = call["argv"], call["env"]
    assert argv[0] == os.path.realpath(server["bin"] / "bwrap")
    for flag in AGY_CAGE_FLAGS:
        assert flag in argv, flag
    sep = argv.index("--")
    args = AGY_STREAM_4142 if stream else AGY_4142
    assert argv[sep + 1 :] == [os.path.realpath(server["bin"] / "agy"), *args]
    _env_names(env, "agy", server)
    assert (env["TERM"], env["AGY_CLI_DISABLE_AUTO_UPDATE"]) == ("dumb", "1")
    assert env["HOME"] == str(agy_home)
    # Its private TMPDIR, under the launch directory, is its cage's cwd.
    assert os.path.dirname(env["TMPDIR"]) == os.path.realpath(server["spawn"])
    assert argv[argv.index("--chdir") + 1] == env["TMPDIR"]
    assert call["cwd"] == server["spawn"]
    assert call["start_new_session"] is True


async def test_gemini_version_probe_starts_as_in_4142(server):
    probe = await cs.probe_cli_version("gemini")

    assert probe["ok"] is False
    (call,) = server["calls"]
    assert call["argv"] == ["gemini", "--version"]
    assert set(call["env"]) == ENV_4142["gemini"] - {
        "GEMINI_CLI_HOME",
        "GEMINI_CLI_SYSTEM_DEFAULTS_PATH",
        "GEMINI_CLI_SYSTEM_SETTINGS_PATH",
    }
    assert call["cwd"] == server["spawn"]
    assert call["start_new_session"] is True


async def test_agy_version_probe_runs_in_its_cage_as_in_4142(server, agy_home):
    probe = await cs.probe_cli_version("agy")

    assert probe["ok"] is False
    (call,) = server["calls"]
    argv = call["argv"]
    assert argv[0] == os.path.realpath(server["bin"] / "bwrap")
    sep = argv.index("--")
    assert argv[sep + 1 :] == [os.path.realpath(server["bin"] / "agy"), "--version"]
    assert set(call["env"]) == ENV_4142["agy"]
    assert call["cwd"] == server["spawn"]
    assert call["start_new_session"] is True
