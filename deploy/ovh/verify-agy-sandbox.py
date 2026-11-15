"""Offline check of agy's cage; run with compose exec -T gateway python - < this file.

No model request is made and agy itself is never run. The script builds the
cage of an agy call with the gateway's own code (``cli_subprocess.agy_caged``:
the same ``build_child_env("agy")`` environment, the same store
``$HOME/.gemini/antigravity-cli``, a private ``TMPDIR`` made by
``agy_private_tmp`` under the CLIs' launch directory, ``agy_cage.cage_argv``),
with one change: the binary is this Python interpreter (by its real path,
like agy's), which runs a standard-library probe (``-I -S -c``) instead of
agy.

The interpreter needs a few read-only paths agy does not: its standard
library, ``lib-dynload`` and, for a shared build, ``libpython``. They are
added through ``cage_argv``'s ``_extra_ro_for_tests`` parameter, reserved for
the test suite; this script is its only other user, and adds nothing else.
No virtualenv is bound: the real interpreter runs with ``-I -S``, so neither
the venv nor any site-packages are imported. In the image (python:3.12-slim)
this exposes, read-only, ``/usr/local/bin/python3.12`` (the binary) and
``/usr/local/lib/python3.12`` (which holds the image's site-packages, code
without secrets) plus ``libpython``: the probe reports what it sees of
``/usr/local/bin``, which must be the interpreter at most (never agy).

The probe checks, from inside the cage: the application's folders
(``/app``, ``/app/data``, ``/app/logs``), ``~/.claude``, ``~/.codex``,
``/usr/local/bin/agy`` and a canary written next to the private ``TMPDIR``
cannot be read; HOME shows ``.gemini`` only; ``/proc`` is either the one of
the cage's own pid namespace (the probe is its pid 1, as agy is: the gateway's
``/proc/<pid>/environ`` is out of sight, and no visible ``/proc/*/environ``
names a server secret) or, where the container forbids a new procfs (a
Docker container masks paths of its ``/proc``), not mounted at all (then no
environ can be read); the report's ``proc`` says which; writing in HOME,
``~/.gemini``, ``/tmp``, ``/`` and ``/usr/lib`` fails while ``TMPDIR`` is
writable; its environment holds no server secret and has
``AGY_CLI_DISABLE_AUTO_UPDATE=1``; agy's sign-in token is readable and
rewritable without being changed (opened ``r+b`` without writing,
``os.access(W_OK)``, a witness file created then renamed in the store, then
removed). The token's content is never printed nor copied anywhere; the
script checks, in memory, that it is unchanged afterwards.

agy 1.4.4 cannot start without ``~/.gemini/config`` and
``~/.gemini/projects.json``, beside its store (UAT, 15/11: every call failed
when the cage did not show them). The probe checks that ``~/.gemini`` shows
exactly ``antigravity-cli``, ``config`` and ``projects.json``
(``gemini_visible``), that ``config/``, ``projects.json`` and
``config/mcp_config.json`` are readable, and that writing there fails with
``EROFS``: a file created in ``config/``, ``config/mcp_config.json`` and
``projects.json`` opened for writing (nothing created nor truncated), a
folder created in ``config/projects/``. It reports how many MCP servers
``mcp_config.json`` declares (``mcp_servers``, 0 when empty or not JSON),
never which: one set up by the operator applies to every client's prompt.
When ``config/`` or ``projects.json`` is missing (agy never run on this
account), no cage is built and the check fails (exit 1, ``agy_setup``:
``missing``), saying to run agy once.

``--app-root`` changes where the application's folders are looked for
(default ``/app``); it exists for the test suite, which runs on a host
without ``/app``.

The report names the ``bwrap`` the cage uses (found through ``agy_cage``:
``/usr/libexec/plugboard/bwrap`` in the image) and says whether one is on
the PATH the CLIs get (``bwrap_on_path``). ``true`` fails the check (INC-4):
codex prefers a bwrap found on PATH to the one it ships, and in a container
Debian's cannot mount ``/proc`` (every codex call failed, 15/11).

Output: one JSON line, ``model_requests: 0``. Exit code 0 when every check
passes, 1 when a check fails (or agy is not signed in, or a bwrap is on the
PATH), 2 when the cage cannot start.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import sysconfig
from typing import Dict, List, Optional

from plugboard import agy_cage, agy_store
from plugboard import cli_subprocess as cs

# Names that must never reach agy (cli_subprocess._NEVER_FORWARD_ENV and its
# prefixes), checked again here from the probe's side.
SECRET_NAMES = ("ADMIN_KEY", "API_KEY", "ENCRYPTION_KEY", "OPENCLAW_KEY")
SECRET_NAMES += ("ALERT_WEBHOOK_URL",)
SECRET_PREFIXES = ("PLUGBOARD_", "LLM_" + "ENABLER_")
TOKEN_NAME = "antigravity-oauth-token"
WITNESS = ".plugboard-verify-"
# What the cage shows of ~/.gemini: agy's store and its config beside it.
GEMINI_VISIBLE = ["antigravity-cli", "config", "projects.json"]

# Runs inside the cage with -I -S: standard library only, prints one JSON
# object. It never prints, writes or returns the token's content.
PROBE = r"""
import errno, json, os, sys

p = json.loads(sys.argv[1])
names, prefixes = tuple(p["secret_names"]), tuple(p["secret_prefixes"])


def secret(name):
    return name in names or name.startswith(prefixes)


def why(exc):
    return errno.errorcode.get(exc.errno, str(exc.errno))


def read_state(path):
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            with open(path, "rb") as handle:
                handle.read(1)
        return "readable"
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return why(exc)


def listing(path):
    try:
        return sorted(os.listdir(path))
    except OSError as exc:
        return "absent" if exc.errno == errno.ENOENT else why(exc)


def write_state(folder):
    path = os.path.join(folder, p["witness"] + "w")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        return why(exc)
    os.close(fd)
    try:
        os.unlink(path)
    except OSError:
        pass
    return "written"


home = os.environ.get("HOME", "")
tmpdir = os.environ.get("TMPDIR", "")
gemini = os.path.join(home, ".gemini")
store = os.path.join(gemini, "antigravity-cli")
out = {"home": home, "tmpdir": tmpdir}

out["reads"] = {path: read_state(path) for path in p["reads"]}
out["home_visible"] = listing(home)
out["usr_local_bin_visible"] = listing("/usr/local/bin")

try:
    pids = [d for d in os.listdir("/proc") if d.isdigit()]
    mounted = True
except FileNotFoundError:
    # The cage goes without /proc where the container forbids a new procfs.
    pids, mounted = [], False
try:
    with open("/proc/1/cmdline", "rb") as handle:
        argv0 = handle.read().split(b"\0")[0].decode("utf-8", "replace")
except OSError as exc:
    argv0 = "unreadable: " + why(exc)
found = set()
for pid in pids:
    try:
        with open("/proc/%s/environ" % pid, "rb") as handle:
            data = handle.read()
    except OSError:
        continue
    for item in data.split(b"\0"):
        name = item.split(b"=", 1)[0].decode("latin-1")
        if secret(name):
            found.add(name)
out["proc"] = {
    "mounted": mounted,
    "pid1": os.path.basename(argv0),
    "self_pid": os.getpid(),
    "visible_pids": len(pids),
    "secret_names_in_proc_environ": sorted(found),
}

out["writes"] = {
    "HOME": write_state(home),
    "~/.gemini": write_state(gemini),
    "/tmp": write_state("/tmp"),
    "/": write_state("/"),
    "/usr/lib": write_state("/usr/lib"),
}

# agy's config beside its store (agy 1.4.4 cannot start without it): shown
# read-only. Opened for writing without creating nor truncating anything.
def open_for_writing(path):
    try:
        fd = os.open(path, os.O_WRONLY)
    except OSError as exc:
        return "absent" if exc.errno == errno.ENOENT else why(exc)
    os.close(fd)
    return "opened"


def mkdir_state(folder):
    # Inside it when it exists, else the folder itself (agy creates both).
    path = os.path.join(folder, p["witness"] + "d") if os.path.isdir(folder) else folder
    try:
        os.mkdir(path, 0o700)
    except OSError as exc:
        return why(exc)
    os.rmdir(path)
    return "written"


config = os.path.join(gemini, "config")
projects = os.path.join(gemini, "projects.json")
mcp = os.path.join(config, "mcp_config.json")
out["gemini_visible"] = listing(gemini)
out["gemini_config_reads"] = {
    "~/.gemini/config": read_state(config),
    "~/.gemini/projects.json": read_state(projects),
    "~/.gemini/config/mcp_config.json": read_state(mcp),
}
out["gemini_config_writes"] = {
    "~/.gemini/config": write_state(config),
    "~/.gemini/config/mcp_config.json": open_for_writing(mcp),
    "~/.gemini/projects.json": open_for_writing(projects),
    "~/.gemini/config/projects/": mkdir_state(os.path.join(config, "projects")),
}
# How many MCP servers agy would use, never which: 0 when empty or not JSON.
servers = 0
try:
    with open(mcp, "rb") as handle:
        declared = json.loads(handle.read() or b"null")
    if isinstance(declared, dict) and isinstance(declared.get("mcpServers"), dict):
        servers = len(declared["mcpServers"])
except (OSError, ValueError):
    pass
out["mcp_servers"] = servers

try:
    probe = os.path.join(tmpdir, p["witness"] + "t")
    with open(probe, "xb") as handle:
        handle.write(b"ok")
    with open(probe, "rb") as handle:
        assert handle.read() == b"ok"
    os.unlink(probe)
    out["tmpdir_write"] = "pass"
except (OSError, AssertionError) as exc:
    out["tmpdir_write"] = "fail: " + type(exc).__name__

out["env"] = {
    "secret_names": sorted(n for n in os.environ if secret(n)),
    "AGY_CLI_DISABLE_AUTO_UPDATE": os.environ.get("AGY_CLI_DISABLE_AUTO_UPDATE"),
}

token = os.path.join(store, p["token_name"])
steps = {}
if not os.path.lexists(token):
    steps["present"] = False
else:
    steps["present"] = True
    try:
        with open(token, "rb") as handle:
            steps["non_empty"] = len(handle.read()) > 0
    except OSError as exc:
        steps["non_empty"] = why(exc)
    try:
        # Opened for writing, nothing written: agy may refresh it meanwhile.
        with open(token, "r+b"):
            pass
        steps["open_rw"] = True
    except OSError as exc:
        steps["open_rw"] = why(exc)
    steps["access_w"] = os.access(token, os.W_OK)
    first = os.path.join(store, p["witness"] + "tmp")
    final = os.path.join(store, p["witness"] + "renamed")
    try:
        fd = os.open(first, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b"witness")
        os.close(fd)
        os.replace(first, final)
        os.unlink(final)
        steps["witness_renamed"] = True
    except OSError as exc:
        steps["witness_renamed"] = why(exc)
out["token"] = steps
print(json.dumps(out))
"""


def _interpreter_paths(binary: str) -> List[str]:
    """Read-only paths this interpreter needs on top of agy's cage."""
    found: List[str] = [os.path.realpath(os.path.dirname(os.__file__))]
    for entry in sys.path:
        if entry and os.path.basename(entry) == "lib-dynload" and os.path.isdir(entry):
            found.append(os.path.realpath(entry))
    if sysconfig.get_config_var("Py_ENABLE_SHARED"):
        libdir = sysconfig.get_config_var("LIBDIR") or ""
        soname = sysconfig.get_config_var("INSTSONAME") or ""
        library = os.path.join(libdir, soname)
        if libdir and soname and os.path.isfile(library):
            found.append(os.path.realpath(library))
    paths: List[str] = []
    for path in found:
        if path == binary or any(path.startswith(kept + os.sep) for kept in paths):
            continue
        if path not in paths:
            paths.append(path)
    return paths


@contextlib.contextmanager
def _cage_running_this_interpreter(extra: List[str]):
    """``agy_caged`` as the gateway runs it, plus ``extra`` read-only.

    The gateway's own ``agy_caged`` is used (environment, store, private
    TMPDIR); only ``cage_argv`` gets the interpreter's paths, then is put
    back.
    """
    real = agy_cage.cage_argv

    def with_interpreter(binary, args, **kwargs):
        return real(binary, args, _extra_ro_for_tests=extra, **kwargs)

    agy_cage.cage_argv = with_interpreter
    try:
        yield
    finally:
        agy_cage.cage_argv = real


def _token_fingerprint(path: str) -> Optional[tuple]:
    """In memory only: digest, size and mtime, never the content itself."""
    try:
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).digest()
        info = os.stat(path)
    except FileNotFoundError:
        return None
    return digest, info.st_size, info.st_mtime_ns


def _emit(report: Dict, code: int) -> None:
    report["result"] = "pass" if code == 0 else "fail"
    print(json.dumps(report))
    sys.exit(code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline check of agy's cage.")
    parser.add_argument("--app-root", default="/app")
    options = parser.parse_args()
    app = os.path.normpath(options.app_root)

    report: Dict = {"model_requests": 0, "agy_run": False, "uid": os.getuid()}
    failures: List[str] = []
    report["failures"] = failures
    if os.getuid() == 0:
        failures.append("the gateway runs as root")
        _emit(report, 1)

    # INC-4: the PATH every CLI gets is the gateway's (build_child_env); a
    # bwrap there is the one codex takes instead of the one it ships.
    report["bwrap"] = None
    codex_path = cs.build_child_env("codex").get("PATH", "")
    on_path = shutil.which("bwrap", path=codex_path) if codex_path else None
    report["bwrap_on_path"] = on_path is not None
    if on_path is not None:
        failure = (
            f"a bwrap is on the CLIs' PATH ({on_path}): codex would take it "
            "instead of the one it ships and fail to mount /proc (INC-4)"
        )
        print(failure, file=sys.stderr)
        failures.append(failure)

    binary = os.path.realpath(sys.executable)
    extra = _interpreter_paths(binary)
    report["interpreter"] = binary
    report["interpreter_ro"] = extra
    env_home = cs.build_child_env("agy").get("HOME", "")
    home = os.path.normpath(env_home) if env_home else ""
    store = os.path.join(home, *agy_store.STORE_PARTS)
    token = os.path.join(store, TOKEN_NAME)
    reads = [app, f"{app}/data", f"{app}/logs"]
    reads += [f"{home}/.claude", f"{home}/.codex", "/usr/local/bin/agy"]

    # Like complete(): the same launch directory, TMPDIR made under it.
    spawn_cwd = os.environ.get("PLUGBOARD_SUBPROCESS_CWD") or cs._clean_spawn_cwd()
    tag = secrets.token_hex(8)
    canary = os.path.join(os.path.realpath(spawn_cwd), f"plugboard-verify-canary-{tag}")
    params = {
        "reads": reads + [canary],
        "witness": f"{WITNESS}{tag}.",
        "token_name": TOKEN_NAME,
        "secret_names": list(SECRET_NAMES),
        "secret_prefixes": list(SECRET_PREFIXES),
    }
    before = _token_fingerprint(token)
    result = None
    try:
        with open(canary, "x", encoding="utf-8") as handle:
            handle.write("outside-canary")
        with _cage_running_this_interpreter(extra):
            with cs.agy_caged(
                binary, ["-I", "-S", "-c", PROBE, json.dumps(params)], spawn_cwd
            ) as (argv, env):
                # The bwrap agy_cage resolved (its fixed path in the image).
                report["bwrap"] = argv[0]
                expected_tmpdir = env["TMPDIR"]
                result = subprocess.run(
                    argv,
                    env=env,
                    cwd=spawn_cwd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
    except agy_cage.AgyNotSetUp as exc:
        # agy never run on this account: every agy call answers 503.
        report["agy_setup"] = "missing"
        print(f"agy cannot run here: {exc}", file=sys.stderr)
        failures.append(str(exc))
        _emit(report, 1)
    except agy_cage.CageUnavailable as exc:
        report["sandbox_start"] = "fail"
        print(f"agy sandbox (bubblewrap) cannot start: {exc}", file=sys.stderr)
        failures.append("sandbox cannot start")
        _emit(report, 2)
    except subprocess.TimeoutExpired:
        report["sandbox_start"] = "fail"
        print("agy sandbox probe timed out after 30s", file=sys.stderr)
        failures.append("probe timed out")
        _emit(report, 2)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(canary)

    setup = agy_cage.setup_failure(result.returncode, result.stdout, result.stderr)
    if setup is not None or result.returncode != 0:
        report["sandbox_start"] = "fail"
        cause = setup or (result.stderr or b"").decode("utf-8", "replace")[:300]
        print(f"agy sandbox (bubblewrap) could not start: {cause}", file=sys.stderr)
        failures.append(f"sandbox exited {result.returncode}")
        _emit(report, 2)
    try:
        seen = json.loads(result.stdout)
    except ValueError:
        report["sandbox_start"] = "fail"
        print("agy sandbox probe printed no report", file=sys.stderr)
        failures.append("probe printed no report")
        _emit(report, 2)
    report["sandbox_start"] = "pass"
    report["agy_setup"] = "set up"

    if os.path.normpath(seen["home"]) != home or seen["tmpdir"] != expected_tmpdir:
        failures.append("probe ran with another HOME or TMPDIR than agy's")
    canary_state = seen["reads"].pop(canary)
    report["reads_denied"] = seen["reads"]
    for path, state in seen["reads"].items():
        if state == "readable":
            failures.append(f"{path} readable in the cage")
    report["outside_canary"] = "invisible" if canary_state != "readable" else "readable"
    if canary_state == "readable":
        failures.append("a file next to TMPDIR is readable in the cage")
    report["home_visible"] = seen["home_visible"]
    if seen["home_visible"] != [".gemini"]:
        failures.append("HOME shows more than .gemini in the cage")
    allowed = (
        {os.path.basename(binary)} if binary.startswith("/usr/local/bin/") else set()
    )
    local_bin = seen["usr_local_bin_visible"]
    report["usr_local_bin_visible"] = local_bin
    if isinstance(local_bin, list) and not set(local_bin) <= allowed:
        failures.append("/usr/local/bin shows more than the interpreter")
    proc = seen["proc"]
    # The cage has /proc (its own pid namespace's) or, where the container
    # forbids a new procfs, none at all: never the gateway's.
    expected_proc = "--proc" in argv
    if proc["mounted"]:
        report["proc"] = "mounted (own pid namespace)"
        report["proc_details"] = {
            "pid1": proc["pid1"],
            # The probe runs as agy does (``--as-pid-1``): pid 1 of the
            # cage's own pid namespace, or there is no such namespace.
            "pid1_is_gateway": proc["self_pid"] != 1,
            "visible_pids": proc["visible_pids"],
            "secret_names_in_proc_environ": proc["secret_names_in_proc_environ"],
        }
        if not expected_proc:
            failures.append("/proc visible in a cage built without it")
        if report["proc_details"]["pid1_is_gateway"]:
            failures.append("the probe is not pid 1 of the cage: no pid namespace")
        if proc["secret_names_in_proc_environ"]:
            failures.append("a server secret is named in a visible /proc/*/environ")
    else:
        report["proc"] = "not mounted (the container forbids a new procfs)"
        if expected_proc:
            failures.append("/proc missing in a cage built with it")
    report["writes_denied"] = seen["writes"]
    for where, state in seen["writes"].items():
        if state == "written":
            failures.append(f"write allowed in {where}")
    # agy's config: visible and readable, on a read-only mount, nothing else
    # of ~/.gemini.
    report["gemini_visible"] = seen["gemini_visible"]
    if seen["gemini_visible"] != GEMINI_VISIBLE:
        failures.append(
            "~/.gemini shows other than antigravity-cli, config and "
            "projects.json in the cage"
        )
    report["gemini_config_reads"] = seen["gemini_config_reads"]
    for where, state in seen["gemini_config_reads"].items():
        if state != "readable" and not (
            where.endswith("mcp_config.json") and state == "absent"
        ):
            failures.append(f"{where} not readable in the cage ({state})")
    report["gemini_config_writes_denied"] = seen["gemini_config_writes"]
    for where, state in seen["gemini_config_writes"].items():
        if state != "EROFS" and not (
            where.endswith("mcp_config.json") and state == "absent"
        ):
            failures.append(f"{where} not read-only in the cage ({state})")
    report["mcp_servers"] = seen["mcp_servers"]
    report["tmpdir_write"] = seen["tmpdir_write"]
    if seen["tmpdir_write"] != "pass":
        failures.append("TMPDIR not writable in the cage")
    report["env"] = seen["env"]
    if seen["env"]["secret_names"]:
        failures.append("a server secret reached the cage's environment")
    if seen["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] != "1":
        failures.append("AGY_CLI_DISABLE_AUTO_UPDATE=1 missing")

    steps = seen["token"]
    after = _token_fingerprint(token)
    leftovers = [n for n in os.listdir(store) if n.startswith(WITNESS)]
    if not steps["present"] or before is None:
        report["token"] = "absent (agy not signed in)"
        failures.append("agy is not signed in: no token to check")
    elif before != after:
        report["token"] = "changed during the check"
        failures.append("token changed during the check (agy may have refreshed it)")
    elif all(steps[k] is True for k in ("non_empty", "open_rw", "access_w")) and (
        steps["witness_renamed"] is True
    ):
        report["token"] = "readable, rewritable, unchanged"
    else:
        report["token"] = "not rewritable: " + json.dumps(
            {k: v for k, v in steps.items() if v is not True}
        )
        failures.append("token not readable or not rewritable in the cage")
    if leftovers:
        failures.append("a witness file was left in agy's store")
    _emit(report, 1 if failures else 0)


if __name__ == "__main__":
    main()
