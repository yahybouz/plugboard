"""The bubblewrap cage every agy launch runs in (BUG-86).

agy runs a tenant's prompt with the gateway's uid. Its own guards (headless
refusal of tools, ``--sandbox``) are agy's word only; the cage is the
gateway's: whatever agy does, it sees and changes only what is listed here.
This module builds the gateway's only command line that launches agy
(``complete``, ``agy models``, ``agy --version``; the operator's
``scripts/bench_latence.py`` runs agy bare, BUG-110):

- ``bwrap`` with its own user, pid, ipc, uts (and, where possible, cgroup)
  namespaces, ended with the gateway (``--die-with-parent``). The pid
  namespace keeps ``/proc/<gateway pid>/environ`` (the server's keys) out of
  sight. agy is its pid 1 (``--as-pid-1``): bwrap's own init there would be
  left unreaped, a zombie of the gateway when it is its container's PID 1.
  The network is shared: agy needs it. No ``--new-session``: bwrap stays in
  the group the gateway kills on timeout or cancellation.
- An empty root holding, read-only, the system libraries (``/usr/lib``,
  ``/usr/lib64``, the ``/lib*`` links), the few ``/etc`` files the loader,
  name resolution and TLS need, the CA bundles (and the system CA paths
  ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` name) and the time zones; ``/proc`` of
  the new pid namespace (see below) and a minimal ``/dev``.
- Writable: agy's store (``$HOME/.gemini/antigravity-cli``, where its sign-in
  token is refreshed) and the request's private ``TMPDIR``. Nothing else.
- Read-only, right after the store: agy's own config beside it,
  ``$HOME/.gemini/config`` (its MCP servers' ``mcp_config.json`` included)
  and ``$HOME/.gemini/projects.json``, which agy 1.4.4 cannot start without
  (see :func:`_agy_config`).
- The binary, by its real path, read-only, bound after the store; then the
  whole root is remounted read-only (HOME, the binary's folder...).

Not in the cage: ``/usr/bin``, ``/usr/local``, ``/app``, the rest of HOME
(``~/.claude``, ``~/.codex``, the rest of ``~/.gemini``...), ``/tmp``,
``/sys``.

``/proc``: a Docker container masks paths of its ``/proc`` (``/proc/kcore``,
``/proc/keys``...) by mounting over them, and the kernel then refuses a new
procfs in a user namespace (``bwrap: Can't mount proc on /newroot/proc:
Operation not permitted``). Like codex's own sandbox, the cage checks once
per process, running bwrap itself (never agy) with the cage's namespaces and
system binds and ``--proc /proc``: when that is refused, the cage has no
``/proc`` at all. It keeps its pid namespace (what agy leaves running dies
with it) and the gateway's ``/proc/<pid>/environ`` stays out of reach a
fortiori. The host's ``/proc`` is never bound into the cage: it would show
the gateway's environment (same uid).

The environment reaches agy through execve (the caller's ``env=``), never the
command line nor ``--setenv``: a proxy URL can hold a password.

No cage, no agy: there is no variable, option or fallback that launches agy
without it. :class:`CageUnavailable` is raised before anything starts when
bwrap is missing, the platform is not Linux, the store, ``TMPDIR``, agy's
config or ``projects.json`` path goes through a symbolic link, or agy was
never run on the account (:class:`AgyNotSetUp`); :func:`setup_failure` recognises a bwrap that
started but could not build the cage.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from typing import List, Mapping, Optional, Sequence

from plugboard.agy_store import STORE_PARTS

log = logging.getLogger(__name__)


class CageUnavailable(Exception):
    """The cage cannot be built, so agy must not run.

    ``str()`` is the internal cause, for the gateway log and the admin only.
    """


class AgyNotSetUp(CageUnavailable):
    """agy was never run on this server account: its ``~/.gemini/config``
    or ``~/.gemini/projects.json`` is missing (see :func:`_agy_config`)."""


# What agy 1.4.4 reads under ~/.gemini outside its store (real UAT run,
# 15/11): config/ (.migrated, mcp_config.json, projects/) and projects.json.
_CONFIG_DIR = (".gemini", "config")
_PROJECTS_FILE = (".gemini", "projects.json")


# System files agy (a dynamic Go binary: glibc, its loader, DNS, TLS) needs,
# bound read-only when they exist.
_SYSTEM_DIRS = ("/usr/lib", "/usr/lib64")
_LIB_LINKS = ("/lib", "/lib64", "/lib32")
_SYSTEM_FILES = (
    # glibc's loader finds the libraries through it; without it, it only
    # looks in the default folders.
    "/etc/ld.so.cache",
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/host.conf",
    "/etc/gai.conf",
    "/etc/passwd",
    "/etc/group",
    "/etc/localtime",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
    "/usr/share/ca-certificates",
    "/usr/share/zoneinfo",
)
# CA locations the child's environment may name: bound only when absolute,
# through no symbolic link (bwrap would follow it), and under one of the
# system's CA folders (``SSL_CERT_DIR=/app/data`` would show the app's data).
_CA_VARIABLES = ("SSL_CERT_FILE", "SSL_CERT_DIR")
_CA_ROOTS = ("/etc/", "/usr/share/", "/usr/lib/ssl/")
_NAMESPACES = (
    "--unshare-user",
    "--unshare-ipc",
    "--unshare-pid",
    # The command is the pid 1 of the cage, bwrap starts no init of its own:
    # the outer bwrap leaves without reaping that init, which then becomes a
    # zombie of whoever reaps orphans (the gateway, PID 1 of its container on
    # the UAT: one zombie per call and per version probe, 15/11). The cage
    # still ends with agy: when the pid 1 of a pid namespace exits, the kernel
    # kills everything left in it.
    "--as-pid-1",
    "--unshare-uts",
    "--unshare-cgroup-try",
    "--die-with-parent",
)
# bwrap's words when the kernel refuses a new procfs (a Docker container).
_PROC_REFUSED = "Can't mount proc"
_PROC_CHECK_TIMEOUT_S = 5.0

# Whether the cage can mount a procfs of its own: None until checked, then
# kept for the life of the process (see _proc_mountable).
_proc_mount: Optional[bool] = None


# CA variables already reported as ignored (logged once each).
_ca_refused: set = set()
# bwrap's real path, resolved on PATH once (like the CLIs' binaries, TM-001:
# a bwrap put on PATH later is never used).
_bwrap_path: Optional[str] = None


def _reset_for_tests() -> None:
    """Forget what was checked or resolved once per process (tests only)."""
    global _proc_mount, _bwrap_path
    _proc_mount = None
    _bwrap_path = None
    _ca_refused.clear()


def _bwrap() -> str:
    """bwrap's real path, looked up on PATH the first time only."""
    global _bwrap_path
    if _bwrap_path and os.path.isfile(_bwrap_path):
        return _bwrap_path
    found = shutil.which("bwrap")
    if found is None:
        raise CageUnavailable("bwrap not found on PATH")
    _bwrap_path = os.path.realpath(found)
    return _bwrap_path


def _plain_path(path: str) -> bool:
    """Whether ``path`` is absolute and goes through no symbolic link."""
    return os.path.isabs(path) and os.path.realpath(path) == os.path.normpath(path)


def _store(env: Mapping[str, str]) -> str:
    """agy's store under the child's HOME: a real directory, created 0700."""
    home = env.get("HOME") or ""
    if not os.path.isabs(home):
        raise CageUnavailable("agy needs an absolute HOME to find its store")
    store = os.path.join(os.path.normpath(home), *STORE_PARTS)
    if not _plain_path(store):
        raise CageUnavailable("agy store path goes through a symbolic link")
    try:
        os.makedirs(store, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise CageUnavailable(
            f"agy store cannot be created ({type(exc).__name__})"
        ) from None
    # Again once it exists: a link may have been put in place meanwhile.
    if not _plain_path(store) or not os.path.isdir(store):
        raise CageUnavailable("agy store path goes through a symbolic link")
    return store


def _agy_config(env: Mapping[str, str]) -> List[str]:
    """``~/.gemini/config`` (a real folder) and ``~/.gemini/projects.json``
    (a real regular file) under the child's HOME, shown read-only to agy.

    agy 1.4.4 reads them outside its store; without them it tries to create
    ``config/`` and every call fails ("mkdir ~/.gemini/config: read-only
    file system", UAT, 15/11). They hold agy's own settings, its MCP servers
    (``config/mcp_config.json``) included: agy may read them, never change
    them. Through a symbolic link (bwrap would follow it) or of another type:
    :class:`CageUnavailable`.

    Missing: :class:`AgyNotSetUp`, and nothing is created. agy writes them
    itself the first time it is run in a shell of the gateway user, which the
    sign-in needs anyway; the gateway does not make up agy's own formats
    (``projects.json``, ``mcp_config.json``) and never writes in
    ``~/.gemini`` outside agy's store.
    """
    home = os.path.normpath(env.get("HOME") or "")
    config = os.path.join(home, *_CONFIG_DIR)
    projects = os.path.join(home, *_PROJECTS_FILE)
    missing = [p for p in (config, projects) if not os.path.lexists(p)]
    if missing:
        names = " and ".join("~/" + os.path.relpath(p, home) for p in missing)
        raise AgyNotSetUp(
            f"agy is not set up for this server account: {names} "
            f"{'is' if len(missing) == 1 else 'are'} missing; ~/.gemini/config "
            "and ~/.gemini/projects.json must exist (run agy once in a shell of "
            "the gateway user, as for its sign-in)"
        )
    for path, kind, wanted in (
        (config, "a directory", stat.S_ISDIR),
        (projects, "a regular file", stat.S_ISREG),
    ):
        name = "~/" + os.path.relpath(path, home)
        if not _plain_path(path):
            raise CageUnavailable(f"agy's {name} goes through a symbolic link")
        if not wanted(os.lstat(path).st_mode):
            raise CageUnavailable(f"agy's {name} is not {kind}")
    return [config, projects]


def _system_args(env: Mapping[str, str]) -> List[str]:
    args: List[str] = []
    for path in _SYSTEM_DIRS:
        args += ["--ro-bind-try", path, path]
    for path in _LIB_LINKS:
        if os.path.islink(path):
            args += ["--symlink", os.readlink(path), path]
        else:
            args += ["--ro-bind-try", path, path]
    for path in _SYSTEM_FILES:
        args += ["--ro-bind-try", path, path]
    for name in _CA_VARIABLES:
        path = _ca_path(name, env.get(name) or "")
        if path is not None:
            args += ["--ro-bind-try", path, path]
    return args


def _ca_path(name: str, value: str) -> Optional[str]:
    """The system CA path ``value`` names, if it may be bound, else None.

    A value outside the system's CA folders, relative or through a link is
    ignored and logged once, by the variable's name only (never its value).
    """
    if not value:
        return None
    path = os.path.normpath(value)
    if not (_plain_path(value) and path.startswith(_CA_ROOTS)):
        if name not in _ca_refused:
            _ca_refused.add(name)
            log.warning(
                "%s ignored by agy's sandbox: only a system CA path "
                "(under %s, no symbolic link) is bound",
                name,
                ", ".join(_CA_ROOTS),
            )
        return None
    return path if os.path.exists(path) else None


def _proc_mountable(bwrap: str, env: Mapping[str, str]) -> bool:
    """Whether the cage can mount a new ``/proc``, checked once per process.

    bwrap runs with the cage's namespaces, its system binds and ``--proc
    /proc``, and bwrap itself (``--version``) as the command: agy never runs
    here. Exit 0: yes. Refused with ``Can't mount proc``: no (a container
    masking paths of its ``/proc``), logged once. Any other outcome says
    nothing about ``/proc``: the cage keeps it (the call itself then fails
    and is reported as usual) and the next call checks again, except after a
    timeout, which is not repeated (the check holds the caller meanwhile).
    """
    global _proc_mount
    if _proc_mount is not None:
        return _proc_mount
    argv = [
        bwrap,
        *_NAMESPACES,
        *_system_args(env),
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--ro-bind",
        bwrap,
        bwrap,
        "--remount-ro",
        "/",
        "--",
        bwrap,
        "--version",
    ]
    # It runs before agy's call, on the gateway's event loop: bounded, in a
    # session of its own (killed whole on timeout), stderr in a file (a
    # leftover child holding a pipe cannot make it wait).
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                env={},
                start_new_session=True,
            )
        except OSError:
            return True
        try:
            returncode = proc.wait(timeout=_PROC_CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            # Never again in this process: /proc kept, the call reports.
            _proc_mount = True
            log.warning("agy's sandbox /proc check timed out: /proc kept")
            return True
        errors.seek(0)
        stderr = errors.read(4096).decode("utf-8", "replace")
    if returncode == 0:
        _proc_mount = True
    elif _PROC_REFUSED in stderr:
        _proc_mount = False
        log.warning(
            "agy's sandbox runs without /proc: this container forbids a new "
            "procfs (agy keeps its own pid namespace and cannot read the "
            "gateway's environment)"
        )
    else:
        return True
    return _proc_mount


def cage_argv(
    binary: str,
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    tmpdir: str,
    _extra_ro_for_tests: Sequence[str] = (),
) -> List[str]:
    """The command line that runs ``binary args...`` inside the cage.

    ``env`` is the child's environment (its ``HOME`` locates the store, its
    CA variables are bound); the caller passes the same ``env`` to execve.
    ``tmpdir`` is the request's private ``TMPDIR``, writable and the working
    directory. ``_extra_ro_for_tests`` is for the test suite only (a fake agy
    written in shell needs ``/usr/bin`` and ``/bin``): the gateway never
    passes it.

    Raises :class:`CageUnavailable`, before anything is launched, when the
    cage cannot be built.
    """
    if not sys.platform.startswith("linux"):
        raise CageUnavailable("agy sandbox needs Linux")
    bwrap = _bwrap()
    store = _store(env)
    config, projects = _agy_config(env)
    if not _plain_path(tmpdir) or not os.path.isdir(tmpdir):
        raise CageUnavailable("agy TMPDIR path goes through a symbolic link")
    real_binary = os.path.realpath(binary)
    if not os.path.isfile(real_binary):
        raise CageUnavailable("agy binary not found")

    argv = [bwrap, *_NAMESPACES, *_system_args(env)]
    for path in _extra_ro_for_tests:
        if os.path.islink(path):
            argv += ["--symlink", os.readlink(path), path]
        else:
            argv += ["--ro-bind", path, path]
    if _proc_mountable(bwrap, env):
        # The procfs of the cage's own pid namespace, never the host's.
        argv += ["--proc", "/proc"]
    argv += [
        "--dev",
        "/dev",
        "--bind",
        store,
        store,
        # agy's own config, beside its store: read-only, each at its path.
        "--ro-bind",
        config,
        config,
        "--ro-bind",
        projects,
        projects,
        "--bind",
        tmpdir,
        tmpdir,
        # After the store: read-only even if the binary sits inside it.
        "--ro-bind",
        real_binary,
        real_binary,
        "--remount-ro",
        "/",
        "--chdir",
        tmpdir,
        "--",
        real_binary,
        *args,
    ]
    return argv


def setup_failure(
    returncode: Optional[int], stdout: Optional[bytes], stderr: Optional[bytes]
) -> Optional[str]:
    """bwrap's first error line when it started but could not build the cage
    (agy then never ran), else None.

    That is a non-zero exit, no output at all, and stderr starting with
    ``bwrap: `` (bwrap's own prefix; agy's errors come on stdout as JSON).
    """
    if returncode in (0, None) or (stdout or b"").strip():
        return None
    text = (stderr or b"").decode("utf-8", "replace")
    if not text.startswith("bwrap: "):
        return None
    return text.splitlines()[0][:300]
