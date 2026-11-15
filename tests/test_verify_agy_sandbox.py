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

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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
    assert report["proc_details"]["pid1_is_gateway"] is False
    assert report["proc_details"]["secret_names_in_proc_environ"] == []
    # Writes: only in the private TMPDIR.
    for where in ("HOME", "~/.gemini", "/tmp", "/", "/usr/lib"):
        assert report["writes_denied"][where] != "written", report["writes_denied"]
    assert report["tmpdir_write"] == "pass"
    assert report["env"]["secret_names"] == []
    assert report["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert report["token"] == "readable, rewritable, unchanged"
    # The token is the same file, byte for byte, and no witness is left.
    assert _token_state(world["token"]) == before
    assert not [p for p in world["store"].iterdir() if ".plugboard-verify-" in p.name]
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


_LEAKY_CAGE = """real = agy_cage.cage_argv


def leaky(binary, args, **kw):
    # agy's config bound writable instead of read-only: what the check
    # must catch.
    argv = real(binary, args, **kw)
    gemini = kw["env"]["HOME"] + "/.gemini/"
    for i, arg in enumerate(argv):
        if arg == "--ro-bind" and argv[i + 1].startswith(gemini):
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


@pytest.mark.parametrize(
    ("remove", "missing"),
    [
        (("config",), "~/.gemini/config is missing"),
        (("projects.json",), "~/.gemini/projects.json is missing"),
        (("config", "projects.json"), "~/.gemini/config and ~/.gemini/projects.json"),
    ],
)
def test_agy_not_set_up_fails_clearly(world, real_bwrap, remove, missing):
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
    assert missing in failure
    assert "run agy once in a shell of the gateway user" in failure
    assert "run agy once" in proc.stderr
    assert str(world["home"]) not in proc.stdout + proc.stderr
    for name in remove:
        assert not (world["gemini"] / name).exists()
    assert list(world["tmp"].iterdir()) == []
    _no_secret_in(proc)


def test_a_visible_path_fails_the_check(world, real_bwrap):
    """The check is not vacuous: an app root inside agy's store (which the
    cage shows) is reported readable and fails the run."""
    leaky = world["store"] / "app"
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
    assert report["token"] == "readable, rewritable, unchanged"
    assert report["env"]["secret_names"] == []
    _assert_gemini_config_checked(report, world)
    _no_secret_in(proc)
