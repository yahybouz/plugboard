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
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_agy_cage import _bwrap_unusable_reason

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
    return {
        "home": home,
        "store": store,
        "token": token,
        "app": app,
        "tmp": tmp,
        "data": data,
    }


def _run(
    world, *, path: str | None = None, extra_args=()
) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(world["home"]),
        "PATH": path if path is not None else os.environ.get("PATH", "/usr/bin:/bin"),
        "TMPDIR": str(world["tmp"]),
        "LANG": "C.UTF-8",
        "PLUGBOARD_SKIP_DOTENV": "1",
        "PLUGBOARD_DATA": str(world["data"]),
        **SECRETS,
    }
    with SCRIPT.open("rb") as source:
        return subprocess.run(
            [sys.executable, "-", "--app-root", str(world["app"]), *extra_args],
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
    assert report["proc"]["pid1_is_gateway"] is False
    assert report["proc"]["secret_names_in_proc_environ"] == []
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
    proc = _run(world, path=str(empty))
    assert proc.returncode != 0
    report = _report(proc)
    assert report["sandbox_start"] == "fail"
    assert report["result"] == "fail"
    assert "bwrap not found on PATH" in proc.stderr
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
    proc = _run(world, path=str(fake))
    assert proc.returncode != 0
    report = _report(proc)
    assert report["sandbox_start"] == "fail"
    assert "setting up uid map: Permission denied" in proc.stderr
    _no_secret_in(proc)
