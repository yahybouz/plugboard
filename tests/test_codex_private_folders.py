"""The gateway's private call folders stay out of codex's sandbox (BUG-157).

codex runs its agent in a sandbox confined to the CLIs' launch directory
(``codex_isolation_args``: read-only, or writable in ``workspace-write``).
Up to 4.15.2's first fix, the gateway made the private folders of its other
calls in that same directory: an agy call's ``TMPDIR`` (``agy-tmp-*``, its
egress socket), its private store (``agy-store-*``, a copy of agy's sign-in
token and its conversations) and a web run's folder (``web-run-*``, its
egress socket). A prompt on codex could then list them and read the token of
an agy call in progress (seen with the real ``codex sandbox``, 19/11). They
now sit under a root of the gateway's own (0700, created once per process,
beside the launch directory, never in it).

The real check runs the gateway's codex arguments in ``codex sandbox`` (no
``codex exec``: no model, no sign-in, no request), with an agy call and a web
run open; it is skipped, said so, where codex or its sandbox is missing.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import subprocess
import tempfile

import pytest

from plugboard import agy_cage, agy_store, net_cage
from plugboard import cli_subprocess as cs

TOKEN = "antigravity-oauth-token"
CANARY = "other-call-agy-token"


@pytest.fixture
def gateway_dirs(tmp_path, monkeypatch):
    """The launch directory and the private root as the gateway makes them,
    under ``tmp_path`` (the default temporary directory for the test)."""
    base = tmp_path / "tmp"
    base.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(base))
    monkeypatch.setattr(cs, "_SPAWN_CWD_CACHE", None)
    monkeypatch.setattr(cs, "_CALLS_ROOT_CACHE", None, raising=False)
    monkeypatch.delenv("PLUGBOARD_SUBPROCESS_CWD", raising=False)
    monkeypatch.delenv("PLUGBOARD_CODEX_SANDBOX", raising=False)
    # The cages themselves are not under test: their command lines are
    # stubs, the folders, sockets and stores are the gateway's own.
    monkeypatch.setattr(agy_cage, "cage_argv", lambda binary, args, **kw: ["true"])
    monkeypatch.setattr(agy_cage, "fill_private_store", lambda env, store: None)
    monkeypatch.setattr(agy_cage, "_bwrap", lambda: "/usr/bin/bwrap")
    folders: dict = {}

    def web_argv(bwrap, command, *, env, cwd, folder):
        folders["web_run"] = folder
        return ["true"]

    monkeypatch.setattr(net_cage, "web_cage_argv", web_argv)
    return {"base": base, "folders": folders}


@contextlib.asynccontextmanager
async def calls_in_progress(gateway_dirs):
    """An agy call and a claude web run, open, as ``complete()`` opens them;
    yields the launch directory and the paths a codex prompt would aim at."""
    spawn = cs._clean_spawn_cwd()
    async with contextlib.AsyncExitStack() as stack:
        _argv, env = await stack.enter_async_context(
            cs.agy_caged("agy", ["models"], spawn)
        )
        await stack.enter_async_context(
            cs.web_caged(["claude"], spawn, cs.build_child_env("claude"))
        )
        tmpdir = env["TMPDIR"]
        (store,) = [
            os.path.join(os.path.dirname(tmpdir), name)
            for name in os.listdir(os.path.dirname(tmpdir))
            if name.startswith(agy_store.PRIVATE_PREFIX)
        ]
        # What fill_private_store copies first: the sign-in token.
        with open(os.path.join(store, TOKEN), "w", encoding="utf-8") as f:
            f.write(CANARY)
        web_run = gateway_dirs["folders"]["web_run"]
        yield {
            "spawn": spawn,
            "agy_tmp": tmpdir,
            "agy_store": store,
            "web_run": web_run,
            "token": os.path.join(store, TOKEN),
            "agy_socket": os.path.join(tmpdir, agy_cage.EGRESS_SOCKET),
            "web_socket": os.path.join(web_run, net_cage.EGRESS_SOCKET),
        }


def _within(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


async def test_the_private_call_folders_are_out_of_the_launch_directory(
    gateway_dirs,
):
    async with calls_in_progress(gateway_dirs) as seen:
        spawn = os.path.realpath(seen["spawn"])
        assert os.listdir(spawn) == []
        roots = set()
        for name in ("agy_tmp", "agy_store", "web_run"):
            path = seen[name]
            assert not _within(os.path.realpath(path), spawn), name
            assert not _within(spawn, os.path.realpath(path)), name
            roots.add(os.path.dirname(path))
        # One root for all of them: the gateway's own, a real 0700 folder.
        (root,) = roots
        info = os.lstat(root)
        assert stat.S_ISDIR(info.st_mode)
        assert os.path.realpath(root) == root
        assert info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == 0o700
        assert stat.S_ISSOCK(os.lstat(seen["agy_socket"]).st_mode)
        assert stat.S_ISSOCK(os.lstat(seen["web_socket"]).st_mode)
    # Gone with the calls; the root stays for the next ones.
    assert os.listdir(root) == []


async def test_a_launch_directory_holding_the_private_root_refuses_agy_and_web(
    gateway_dirs, monkeypatch
):
    """An operator's ``PLUGBOARD_SUBPROCESS_CWD`` holding the gateway's
    private root (``/tmp`` itself, say) would show codex the other calls'
    folders again: agy and the web run are refused, nothing made there."""
    root = cs._private_calls_root()
    launch = os.path.dirname(root)
    with pytest.raises(agy_cage.CageUnavailable, match="launch directory"):
        async with cs.agy_caged("agy", ["models"], launch):
            pass
    with pytest.raises(net_cage.CageUnavailable, match="launch directory"):
        async with cs.web_caged(["claude"], launch, cs.build_child_env("claude")):
            pass
    assert os.listdir(root) == []


def test_a_private_root_no_longer_private_is_refused(gateway_dirs):
    """A root opened to others, or swapped for a link, is never used."""
    root = cs._private_calls_root()
    assert cs._private_calls_root() == root
    os.chmod(root, 0o755)
    with pytest.raises(OSError, match="private"):
        cs._private_calls_root()
    os.chmod(root, 0o700)
    os.rmdir(root)
    target = gateway_dirs["base"] / "elsewhere"
    target.mkdir(mode=0o700)
    os.symlink(target, root)
    with pytest.raises(OSError, match="private"):
        cs._private_calls_root()


# ── The real codex sandbox ───────────────────────────────────────────────────

_PROBE = r"""
cat allowed.txt >/dev/null 2>&1 && echo CWD_READ_ALLOWED
for path in "$@"; do
  if test -e "$path" || test -S "$path"; then echo "SEEN $path"; fi
done
if cat "$1" 2>/dev/null; then echo; echo TOKEN_READ; fi
"""


def _codex() -> str:
    path = shutil.which("codex") or (
        "/usr/local/bin/codex" if os.access("/usr/local/bin/codex", os.X_OK) else ""
    )
    if not path:
        pytest.skip("codex is not installed here: the real sandbox is not checked")
    return path


@pytest.mark.parametrize("mode", ["read-only", "workspace-write"])
async def test_codex_sandbox_cannot_see_another_calls_private_folders(
    gateway_dirs, monkeypatch, mode
):
    codex = _codex()
    monkeypatch.setenv("PLUGBOARD_CODEX_SANDBOX", mode)
    async with calls_in_progress(gateway_dirs) as seen:
        spawn = seen["spawn"]
        with open(os.path.join(spawn, "allowed.txt"), "w") as f:
            f.write("allowed")
        targets = [
            seen["token"],
            seen["agy_store"],
            seen["agy_tmp"],
            seen["agy_socket"],
            seen["web_run"],
            seen["web_socket"],
            os.path.dirname(seen["agy_store"]),
        ]
        result = subprocess.run(
            [
                codex,
                "sandbox",
                *cs.codex_isolation_args(spawn),
                "--",
                "/bin/sh",
                "-c",
                _PROBE,
                "probe",
                *targets,
            ],
            cwd=spawn,
            env=cs.build_child_env("codex"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        os.unlink(os.path.join(spawn, "allowed.txt"))
    if "CWD_READ_ALLOWED" not in result.stdout:
        pytest.skip(
            f"codex sandbox does not start here (exit {result.returncode}): "
            f"{result.stderr[-300:]}"
        )
    leaked = [line for line in result.stdout.splitlines() if line != "CWD_READ_ALLOWED"]
    assert leaked == [], result.stdout
