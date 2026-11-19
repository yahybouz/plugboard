"""``deploy/ovh/verify-agy-sandbox.py``: the operator's offline check of agy's
cage (BUG-86).

The script is run as in production (``python - < file`` from the repository
root, like ``compose exec -T gateway python - < file``), on the host, with a
test HOME: a fake agy store holding a fake sign-in token, fake ``~/.claude``
and ``~/.codex``, and fake server secrets in its environment. ``/app/data``
and ``/app/logs`` do not exist on the host: the script's ``--app-root``
option points at a test folder holding both, with content, so that the check
proves a real folder is hidden (a missing path would prove nothing). The
real agy is never run, and no model is asked.

Tests needing the host's bwrap are skipped, with the reason, where it cannot
create namespaces.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.relay_helpers import kill_relays_under, wait_for_no_relay
from tests.test_agy_cage import (
    _bwrap_unusable_reason,
    _docker_like_unusable_reason,
    _outer,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "ovh" / "verify-agy-sandbox.py"
TOKEN = "fake-token-content-5e1d9a"
SECRETS = {
    "ADMIN_KEY": "adm-secret-0b7c",
    "API_KEY": "api-secret-61fe",
    "ENCRYPTION_KEY": "enc-secret-2a90",
    "OPENCLAW_KEY": "ocl-secret-c3d4",
    "ALERT_WEBHOOK_URL": "https://hook.invalid/whk-secret-8e21",
    "PLUGBOARD_SOMETHING": "pb-secret-44a1",
    "LLM_ENABLER_SOMETHING": "le-secret-9f03",
}


@pytest.fixture
def real_bwrap():
    reason = _bwrap_unusable_reason()
    if reason:
        pytest.skip(f"bwrap unusable: {reason}")


@pytest.fixture
def world(tmp_path):
    """A test HOME, an app root and a private temporary folder."""
    home = tmp_path / "home"
    store = home / ".gemini" / "antigravity-cli"
    store.mkdir(parents=True)
    token = store / "antigravity-oauth-token"
    token.write_text(TOKEN)
    # agy's settings in its store, as after its first runs (BUG-108), and
    # its conversations folder.
    (store / "settings.json").write_text("{}\n")
    (store / "installation_id").write_text("00000000-test-install\n")
    (store / "jetski_state.pbtxt").write_text("state: 1\n")
    for name, inside in (("bin", "agentapi"), ("builtin", ".manifest")):
        (store / name).mkdir()
        (store / name / inside).write_text(f"{name}-content\n")
    (store / "conversations").mkdir()
    # Written by agy at its first run (agy 1.4.4 cannot start without them),
    # a temporary file it left, and gemini's own files next to them.
    gemini = home / ".gemini"
    (gemini / "config" / "projects").mkdir(parents=True)
    (gemini / "config" / ".migrated").write_text("")
    (gemini / "config" / "mcp_config.json").write_text("")
    (gemini / "projects.json").write_text("{}\n")
    (gemini / "projects.json.5b0e7c2a-1d3f-4a6b-8c9d-0e1f2a3b4c5d.tmp").write_text("")
    (gemini / "settings.json").write_text("{}\n")
    (gemini / "oauth_creds.json").write_text("gemini-oauth-canary-7d3e\n")
    for other in (".claude", ".codex"):
        (home / other).mkdir()
        (home / other / "credentials.json").write_text("other-cli-secret")
    app = tmp_path / "app"
    for sub in ("data", "logs"):
        (app / sub).mkdir(parents=True)
        (app / sub / "canary.txt").write_text("app-secret")
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    # The image's PATH holds no bwrap (INC-4): an empty folder stands for it.
    path = tmp_path / "path-without-bwrap"
    path.mkdir()
    return {
        "home": home,
        "gemini": gemini,
        "store": store,
        "token": token,
        "app": app,
        "tmp": tmp,
        "data": data,
        "path": path,
        "no_gateway_bwrap": tmp_path / "no-gateway-bwrap",
    }


# The script runs as in the image: the gateway's bwrap at agy_cage's fixed
# path (INC-4), here the host's, set in the module as the image's path is
# (never through a variable), and none on PATH. ``patch`` runs next, before
# the script, read from stdin as with ``python - < file``.
_LAUNCHER = """import sys
sys.path.insert(0, {root!r})
from plugboard import agy_cage
agy_cage.GATEWAY_BWRAP = {bwrap!r}
{patch}
exec(compile(sys.stdin.read(), "verify-agy-sandbox.py", "exec"), {{"__name__": "__main__"}})
"""


def _host_bwrap() -> str | None:
    found = shutil.which("bwrap")
    return os.path.realpath(found) if found else None


def _run(
    world,
    *,
    path: str | None = None,
    gateway_bwrap: str | None = "host",
    patch: str = "",
    extra_args=(),
    wrap=(),
    port: int | None = None,
) -> subprocess.CompletedProcess:
    """Run the script; ``gateway_bwrap=None``: no bwrap at the fixed path."""
    if gateway_bwrap == "host":
        gateway_bwrap = _host_bwrap()
    launcher = _LAUNCHER.format(
        root=str(ROOT),
        bwrap=gateway_bwrap or str(world["no_gateway_bwrap"]),
        patch=patch,
    )
    env = {
        "HOME": str(world["home"]),
        "PATH": path if path is not None else str(world["path"]),
        "TMPDIR": str(world["tmp"]),
        "LANG": "C.UTF-8",
        "PLUGBOARD_SKIP_DOTENV": "1",
        "PLUGBOARD_DATA": str(world["data"]),
        **SECRETS,
    }
    if port is not None:
        env["PORT"] = str(port)
    with SCRIPT.open("rb") as source:
        return subprocess.run(
            [
                *wrap,
                sys.executable,
                "-c",
                launcher,
                "--app-root",
                str(world["app"]),
                *extra_args,
            ],
            stdin=source,
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


def _report(proc: subprocess.CompletedProcess) -> dict:
    lines = [line for line in proc.stdout.splitlines() if line.startswith("{")]
    assert lines, f"no JSON report: {proc.stdout!r} {proc.stderr!r}"
    return json.loads(lines[-1])


def _no_secret_in(proc: subprocess.CompletedProcess) -> None:
    output = proc.stdout + proc.stderr
    assert TOKEN not in output
    for value in SECRETS.values():
        assert value not in output


def _token_state(token: Path):
    info = token.stat()
    return token.read_bytes(), info.st_mtime_ns, stat.S_IMODE(info.st_mode)


def test_the_cage_passes_every_check(world, real_bwrap):
    before = _token_state(world["token"])
    proc = _run(world)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "pass", report
    assert report["failures"] == []
    assert report["model_requests"] == 0
    assert report["agy_run"] is False
    assert report["uid"] == os.getuid() != 0
    assert report["sandbox_start"] == "pass"
    # The gateway's bwrap, found through agy_cage at its fixed path, and none
    # on the PATH codex gets (INC-4).
    assert report["bwrap"] == _host_bwrap()
    assert report["bwrap_on_path"] is False
    # Reads: the app's folders, the other CLIs' sign-ins and /usr/local/bin.
    reads = report["reads_denied"]
    for path in (
        f"{world['app']}/data",
        f"{world['app']}/logs",
        f"{world['home']}/.claude",
        f"{world['home']}/.codex",
        "/usr/local/bin/agy",
    ):
        assert reads[path] != "readable", (path, reads)
    assert report["home_visible"] == [".gemini"]
    assert report["outside_canary"] == "invisible"
    # /proc: a pid namespace of its own, no server secret in any environ.
    assert report["proc"] == "mounted (own pid namespace)"
    assert report["probe_is_pid1"] is True
    assert report["proc_details"]["pid1_is_gateway"] is False
    assert report["proc_details"]["secret_names_in_proc_environ"] == []
    # Writes: only in the private TMPDIR.
    for where in ("HOME", "~/.gemini", "/tmp", "/", "/usr/lib"):
        assert report["writes_denied"][where] != "written", report["writes_denied"]
    assert report["tmpdir_write"] == "pass"
    # No unix socket file in the cage, and no network of its own: the
    # container's is out of reach (BUG-156).
    assert report["socket_files"] == []
    assert report["network"]["shared_with_container"] is False
    assert report["network"]["gateway_port_from_cage"] != "connected"
    assert report["env"]["secret_names"] == []
    assert report["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert report["token"] == "readable, rewritable, unchanged"
    # The call's store is its own (BUG-157): what the probe left there on
    # purpose never reached the shared store.
    assert report["store_private"] is True
    # The token is the same file, byte for byte, and no witness is left.
    assert _token_state(world["token"]) == before
    assert not [p for p in world["store"].rglob("*") if ".plugboard-verify-" in p.name]
    # The private launch directory and its TMPDIR are gone.
    assert list(world["tmp"].iterdir()) == []
    _assert_gemini_config_checked(report, world)
    _no_secret_in(proc)


def _gemini_snapshot(gemini: Path) -> dict:
    return {
        p.relative_to(gemini).as_posix(): None if p.is_dir() else p.read_bytes()
        for p in sorted(gemini.rglob("*"))
        if not p.relative_to(gemini).as_posix().startswith("antigravity-cli")
    }


def _assert_gemini_config_checked(report: dict, world) -> None:
    """agy's config beside its store: visible, readable, never writable;
    nothing else of ~/.gemini; agy's MCP servers counted."""
    assert report["agy_setup"] == "set up"
    assert report["gemini_visible"] == ["antigravity-cli", "config", "projects.json"]
    assert report["gemini_config_reads"] == {
        "~/.gemini/config": "readable",
        "~/.gemini/projects.json": "readable",
        "~/.gemini/config/mcp_config.json": "readable",
    }
    assert report["gemini_config_writes_denied"] == {
        "~/.gemini/config": "EROFS",
        "~/.gemini/config/mcp_config.json": "EROFS",
        "~/.gemini/projects.json": "EROFS",
        "~/.gemini/config/projects/": "EROFS",
    }
    assert report["mcp_servers"] == 0
    assert not [p for p in world["gemini"].rglob("*") if ".plugboard-verify-" in p.name]
    _assert_store_settings_checked(report)


_STORE_SETTINGS_DENIED = {
    "settings.json": "EROFS",
    "installation_id": "EROFS",
    "jetski_state.pbtxt": "EROFS",
}


def _assert_store_settings_checked(report: dict) -> None:
    """BUG-108: agy's settings in its store cannot be changed; it can still
    write its conversations and what it unpacks (review M2)."""
    assert report["store_settings_writes_denied"] == _STORE_SETTINGS_DENIED
    assert report["store_conversation_write"] == "written"
    assert report["store_unpacked_write"] == {"bin": "written", "builtin": "written"}


_LEAKY_CAGE = """real = agy_cage.cage_argv


def leaky(binary, args, **kw):
    # agy's config bound writable instead of read-only: what the check
    # must catch.
    argv = real(binary, args, **kw)
    gemini = kw["env"]["HOME"] + "/.gemini/"
    store = gemini + "antigravity-cli/"
    for i, arg in enumerate(argv):
        if (
            arg == "--ro-bind"
            and argv[i + 1].startswith(gemini)
            and not argv[i + 1].startswith(store)
        ):
            argv[i] = "--bind"
    return argv


agy_cage.cage_argv = leaky
"""


def test_a_writable_agy_config_fails_the_check(world, real_bwrap):
    """Not vacuous: the same cage with agy's config bound writable fails,
    and the check leaves nothing behind."""
    before = _gemini_snapshot(world["gemini"])
    proc = _run(world, patch=_LEAKY_CAGE)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "fail"
    assert report["gemini_config_writes_denied"] == {
        "~/.gemini/config": "written",
        "~/.gemini/config/mcp_config.json": "opened",
        "~/.gemini/projects.json": "opened",
        "~/.gemini/config/projects/": "written",
    }
    assert sorted(report["failures"]) == sorted(
        f"{where} not read-only in the cage ({state})"
        for where, state in report["gemini_config_writes_denied"].items()
    )
    assert _gemini_snapshot(world["gemini"]) == before
    _no_secret_in(proc)


_WRITABLE_SETTINGS = """real = agy_cage.cage_argv


def leaky(binary, args, **kw):
    # agy's shared store bound writable and its settings in it left
    # writable (as before 4.14.4): what the check must catch.
    argv = real(binary, args, **kw)
    store = kw["env"]["HOME"] + "/.gemini/antigravity-cli/"
    first = argv.index("--bind")
    argv[first + 1] = argv[first + 2]
    out = []
    i = 0
    while i < len(argv):
        if argv[i] == "--ro-bind" and argv[i + 1].startswith(store):
            i += 3
            continue
        out.append(argv[i])
        i += 1
    return out


agy_cage.cage_argv = leaky
"""


def _store_snapshot(store: Path) -> dict:
    return {
        p.relative_to(store).as_posix(): None if p.is_dir() else p.read_bytes()
        for p in sorted(store.rglob("*"))
    }


def test_writable_agy_settings_fail_the_check(world, real_bwrap):
    """BUG-108, not vacuous: the cage without the read-only settings fails,
    and the check changes nothing in the store."""
    before = _store_snapshot(world["store"])
    proc = _run(world, patch=_WRITABLE_SETTINGS)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "fail"
    assert report["store_settings_writes_denied"] == {
        "settings.json": "opened",
        "installation_id": "opened",
        "jetski_state.pbtxt": "opened",
    }
    assert sorted(report["failures"]) == sorted(
        [
            f"agy's {name} not read-only in the cage ({state})"
            for name, state in report["store_settings_writes_denied"].items()
        ]
        + ["agy's writes reach the shared store (BUG-157)"]
    )
    assert _store_snapshot(world["store"]) == before
    _no_secret_in(proc)


_SHARED_STORE = """real = agy_cage.cage_argv


def shared(binary, args, **kw):
    # agy's shared store bound writable instead of the call's own (as up to
    # 4.15.1): what the check must catch.
    argv = real(binary, args, **kw)
    first = argv.index("--bind")
    argv[first + 1] = argv[first + 2]
    return argv


agy_cage.cage_argv = shared
"""


def test_a_shared_store_fails_the_check(world, real_bwrap):
    """BUG-157, not vacuous: the cage with the shared store bound writable
    fails, and the check leaves nothing behind in it."""
    before = _store_snapshot(world["store"])
    proc = _run(world, patch=_SHARED_STORE)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "fail"
    assert report["store_private"] is False
    assert report["failures"] == ["agy's writes reach the shared store (BUG-157)"]
    assert _store_snapshot(world["store"]) == before
    _no_secret_in(proc)


_UNPACKED_READ_ONLY = """real = agy_cage.cage_argv


def frozen(binary, args, **kw):
    # bin/ and builtin/ bound read-only over the store (as in 4.14.4 before
    # review M2): agy could not rewrite them, and would fail.
    argv = real(binary, args, **kw)
    store = kw["env"]["HOME"] + "/.gemini/antigravity-cli/"
    i = argv.index("--bind", argv.index("--bind") + 1)
    extra = []
    for name in ("bin", "builtin"):
        extra += ["--ro-bind", store + name, store + name]
    return argv[:i] + extra + argv[i:]


agy_cage.cage_argv = frozen
"""


def test_read_only_unpacked_folders_fail_the_check(world, real_bwrap):
    """Review M2, not vacuous: agy rewrites bin/ and builtin/ after its
    install; a cage that shows them read-only fails the check."""
    before = _store_snapshot(world["store"])
    proc = _run(world, patch=_UNPACKED_READ_ONLY)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["store_unpacked_write"] == {"bin": "EROFS", "builtin": "EROFS"}
    assert sorted(report["failures"]) == [
        "agy cannot write its bin/ in the cage (EROFS)",
        "agy cannot write its builtin/ in the cage (EROFS)",
    ]
    assert _store_snapshot(world["store"]) == before
    _no_secret_in(proc)


def test_missing_unpacked_folders_are_reported_not_failed(world, real_bwrap):
    for name in ("bin", "builtin"):
        shutil.rmtree(world["store"] / name)
    proc = _run(world)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["store_unpacked_write"] == {"bin": "absent", "builtin": "absent"}
    assert not (world["store"] / "bin").exists()


def test_missing_agy_settings_are_reported_not_failed(world, real_bwrap):
    """A store where agy never wrote a settings.json (the usual case): the
    check says so and passes; nothing is created."""
    (world["store"] / "settings.json").unlink()
    proc = _run(world)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["store_settings_writes_denied"] == {
        **_STORE_SETTINGS_DENIED,
        "settings.json": "absent",
    }
    assert not (world["store"] / "settings.json").exists()


def test_agy_config_is_left_as_it_was(world, real_bwrap):
    before = _gemini_snapshot(world["gemini"])
    proc = _run(world)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _gemini_snapshot(world["gemini"]) == before


@pytest.mark.parametrize(
    ("content", "count"),
    [
        ('{"mcpServers": {"one": {"command": "x"}, "two": {"url": "y"}}}', 2),
        ("not json", 0),
        ('{"mcpServers": {}}', 0),
        ("[1, 2]", 0),
    ],
    ids=["two", "unreadable", "none", "no-object"],
)
def test_agy_mcp_servers_are_counted_never_shown(world, real_bwrap, content, count):
    """An MCP server set up by the operator would apply to clients' prompts:
    the check says how many, never what they are."""
    (world["gemini"] / "config" / "mcp_config.json").write_text(content)
    proc = _run(world)
    report = _report(proc)
    assert report["mcp_servers"] == count
    assert proc.returncode == 0, proc.stdout + proc.stderr
    output = proc.stdout + proc.stderr
    for word in ('"one"', '"two"', "command", "not json"):
        assert word not in output, word
    _no_secret_in(proc)


@pytest.mark.parametrize("remove", [("config",), ("config", "projects.json")])
def test_agy_not_set_up_fails_clearly(world, real_bwrap, remove):
    """A server where agy was never run: no cage is built, the check fails
    and says to run agy once; nothing is created."""
    for name in remove:
        path = world["gemini"] / name
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    proc = _run(world)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "fail"
    assert report["agy_setup"] == "missing"
    assert report["model_requests"] == 0 and report["agy_run"] is False
    (failure,) = report["failures"]
    assert "~/.gemini/config is missing" in failure
    assert "projects.json" not in failure
    assert "run agy once in a shell of the gateway user" in failure
    assert "run agy once" in proc.stderr
    assert str(world["home"]) not in proc.stdout + proc.stderr
    for name in remove:
        assert not (world["gemini"] / name).exists()
    assert list(world["tmp"].iterdir()) == []
    _no_secret_in(proc)


def test_without_projects_json_the_check_passes(world, real_bwrap):
    """BUG-115: ~/.gemini/projects.json is gemini's, not agy's: a server
    without it passes; the cage shows agy's store and config only, and
    nothing is created."""
    (world["gemini"] / "projects.json").unlink()
    proc = _run(world)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "pass", report
    assert report["agy_setup"] == "set up"
    assert report["gemini_visible"] == ["antigravity-cli", "config"]
    assert report["gemini_config_reads"]["~/.gemini/projects.json"] == "absent"
    assert report["gemini_config_writes_denied"]["~/.gemini/projects.json"] == (
        "absent"
    )
    assert not (world["gemini"] / "projects.json").exists()
    _no_secret_in(proc)


def test_a_visible_path_fails_the_check(world, real_bwrap):
    """The check is not vacuous: an app root inside agy's bin/ (which the
    cage copies into the call's store, BUG-157) is reported readable and
    fails the run."""
    leaky = world["store"] / "bin" / "app"
    (leaky / "data").mkdir(parents=True)
    (leaky / "data" / "canary.txt").write_text("app-secret")
    (leaky / "logs").mkdir()
    world = {**world, "app": leaky}
    proc = _run(world)
    assert proc.returncode != 0
    report = _report(proc)
    assert report["result"] == "fail"
    assert report["reads_denied"][f"{leaky}/data"] == "readable"
    assert any("readable" in failure for failure in report["failures"])
    _no_secret_in(proc)


def test_a_missing_token_fails(world, real_bwrap):
    world["token"].unlink()
    proc = _run(world)
    assert proc.returncode != 0
    report = _report(proc)
    assert report["token"] == "absent (agy not signed in)"
    assert report["result"] == "fail"
    _no_secret_in(proc)


def test_without_bwrap_the_check_fails_clearly(world, tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    proc = _run(world, path=str(empty), gateway_bwrap=None)
    assert proc.returncode != 0
    report = _report(proc)
    assert report["sandbox_start"] == "fail"
    assert report["result"] == "fail"
    assert "bwrap not found on PATH nor at" in proc.stderr
    assert report["bwrap"] is None
    assert report["bwrap_on_path"] is False
    assert report["model_requests"] == 0
    _no_secret_in(proc)


def test_a_bwrap_that_cannot_build_the_cage_fails_clearly(world, tmp_path):
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    bwrap = fake / "bwrap"
    bwrap.write_text(
        "#!/bin/sh\necho 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n"
    )
    bwrap.chmod(0o755)
    proc = _run(world, path=str(fake), gateway_bwrap=None)
    assert proc.returncode != 0
    report = _report(proc)
    assert report["sandbox_start"] == "fail"
    assert "setting up uid map: Permission denied" in proc.stderr
    _no_secret_in(proc)


def test_a_bwrap_on_path_fails_the_check(world, real_bwrap, tmp_path):
    """INC-4: a bwrap on the PATH the CLIs get is the one codex would take
    instead of its own (Debian's could not mount /proc in the container):
    the cage itself passes, the check fails (exit 1) and says why."""
    on_path = tmp_path / "bin-with-bwrap"
    on_path.mkdir()
    (on_path / "bwrap").symlink_to(_host_bwrap())
    proc = _run(world, path=str(on_path))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "fail"
    assert report["sandbox_start"] == "pass"
    assert report["bwrap"] == _host_bwrap()
    assert report["bwrap_on_path"] is True
    (failure,) = report["failures"]
    assert failure == (
        f"a bwrap is on the CLIs' PATH ({on_path / 'bwrap'}): codex would take "
        "it instead of the one it ships and fail to mount /proc (INC-4)"
    )
    assert failure in proc.stderr
    _no_secret_in(proc)


def test_outside_the_image_bwrap_is_found_on_path(world, real_bwrap, tmp_path):
    """A host install (no bwrap at the fixed path): agy_cage takes the one
    on PATH, which the check then reports for codex's sake."""
    on_path = tmp_path / "bin-with-bwrap"
    on_path.mkdir()
    (on_path / "bwrap").symlink_to(_host_bwrap())
    proc = _run(world, path=str(on_path), gateway_bwrap=None)
    report = _report(proc)
    assert report["sandbox_start"] == "pass"
    assert report["bwrap"] == _host_bwrap()
    assert report["bwrap_on_path"] is True
    assert proc.returncode == 1


_NO_PID_NAMESPACE = """real = agy_cage.cage_argv


def shared_pids(binary, args, **kw):
    # The cage without a pid namespace of its own (and so without
    # --as-pid-1): what the check must catch, with or without /proc.
    drop = ("--unshare-pid", "--as-pid-1")
    return [arg for arg in real(binary, args, **kw) if arg not in drop]


agy_cage.cage_argv = shared_pids
"""

_PID1_FAILURE = (
    "the probe is not pid 1 of the cage: no pid namespace of its own "
    "(--unshare-pid with --as-pid-1)"
)


@pytest.fixture
def no_relay_left(tmp_path):
    """Whatever relay the test's cages leave is ended afterwards (BUG-157):
    a cage without a pid namespace once left one alive per run."""
    marker = os.path.realpath(tmp_path)
    yield marker
    kill_relays_under(marker)


@pytest.mark.parametrize("procfs", ["mounted", "forbidden"])
def test_a_cage_without_its_own_pid_namespace_fails(
    world, real_bwrap, procfs, no_relay_left
):
    """BUG-114: the probe checks that it is pid 1 of the cage (as agy is,
    ``--as-pid-1``) whether or not the cage has a /proc: where the container
    forbids a new procfs (UAT, production), a cage without its own pid
    namespace must fail the check too. Its relays die with the probe even
    so (BUG-157)."""
    wrap = ()
    if procfs == "forbidden":
        reason = _docker_like_unusable_reason()
        if reason:
            pytest.skip(f"Docker-like /proc not reproducible: {reason}")
        wrap = _outer(shutil.which("bwrap"), [])
    proc = _run(world, patch=_NO_PID_NAMESPACE, wrap=wrap)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "fail"
    assert report["probe_is_pid1"] is False
    assert _PID1_FAILURE in report["failures"]
    _no_secret_in(proc)
    assert wait_for_no_relay(no_relay_left) == []


def test_the_check_passes_where_the_container_forbids_a_new_procfs(world):
    """In a Docker-like container (a masked path of /proc), the cage goes
    without /proc: the script says so and every check still passes (no
    /proc, no environ of the gateway to read)."""
    reason = _docker_like_unusable_reason()
    if reason:
        pytest.skip(f"Docker-like /proc not reproducible: {reason}")
    proc = _run(world, wrap=_outer(shutil.which("bwrap"), []))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "pass", report
    assert report["proc"] == "not mounted (the container forbids a new procfs)"
    # Without /proc too, the probe is pid 1 of the cage's pid namespace.
    assert report["probe_is_pid1"] is True
    assert report["token"] == "readable, rewritable, unchanged"
    assert report["env"]["secret_names"] == []
    _assert_gemini_config_checked(report, world)
    _no_secret_in(proc)


def test_the_store_is_left_as_it_was(world, real_bwrap):
    """The check writes in agy's conversations folder and its store only
    witnesses it removes."""
    before = _store_snapshot(world["store"])
    proc = _run(world)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _store_snapshot(world["store"]) == before


# ── What the cage can reach on the network (BUG-109) ────────────────────────
#
# The cage shares the container's network: agy needs the internet, and
# bwrap has no option that keeps the internet while hiding the loopback. The
# check reports, as information, whether the gateway's port answers from
# inside the cage and what listens in that network; it fails only when a
# unix socket file is visible in the cage (the container's /run, /var/run or
# /tmp bound into it).


@contextlib.contextmanager
def _listener():
    """A TCP listener on 127.0.0.1, standing for the gateway's port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    try:
        yield server.getsockname()[1]
    finally:
        server.close()


def test_the_gateway_port_does_not_answer_from_the_cage(world, real_bwrap):
    # Something listens on the gateway's port outside: not reached (BUG-156).
    with _listener() as port:
        proc = _run(world, port=port)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "pass"
    assert report["network"] == {
        "shared_with_container": False,
        "gateway_port": port,
        "gateway_port_from_cage": "refused",
    }
    # The egress socket sits in TMPDIR, which the check skips.
    assert report["socket_files"] == []
    _no_secret_in(proc)


_SHARED_NETWORK = """real = agy_cage.cage_argv


def shared(binary, args, **kw):
    # The cage of 4.15 and before (BUG-109): the container's network.
    return [a for a in real(binary, args, **kw) if a != "--unshare-net"]


agy_cage.cage_argv = shared
"""


def test_a_cage_sharing_the_network_fails(world, real_bwrap):
    with _listener() as port:
        proc = _run(world, port=port, patch=_SHARED_NETWORK)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "fail"
    assert report["network"]["shared_with_container"] is True
    assert report["network"]["gateway_port_from_cage"] == "connected"
    assert report["failures"] == [
        "the cage shares the container's network",
        "the gateway's port answers from the cage",
    ]


_RUN_BOUND = """real = agy_cage.cage_argv


def leaky(binary, args, **kw):
    # The host's /run (unix sockets) bound into the cage: what the check
    # must catch.
    argv = real(binary, args, **kw)
    i = argv.index("--remount-ro")
    return argv[:i] + ["--ro-bind", "/run", "/run"] + argv[i:]


agy_cage.cage_argv = leaky
"""


def _host_sockets_under_run() -> list:
    found = []
    for root, _dirs, files in os.walk("/run"):
        for name in files:
            path = os.path.join(root, name)
            with contextlib.suppress(OSError):
                if stat.S_ISSOCK(os.lstat(path).st_mode):
                    found.append(path)
    return found


def test_a_unix_socket_file_in_the_cage_fails(world, real_bwrap):
    if not _host_sockets_under_run():
        pytest.skip("no unix socket file under the host's /run")
    proc = _run(world, patch=_RUN_BOUND)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    report = _report(proc)
    assert report["result"] == "fail"
    assert report["socket_files"]
    assert all(path.startswith("/run/") for path in report["socket_files"])
    assert report["failures"] == ["a unix socket file is visible in the cage"]
