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
  sight. The network is shared: agy needs it. No ``--new-session``: bwrap
  stays in the group the gateway kills on timeout or cancellation.
- An empty root holding, read-only, the system libraries (``/usr/lib``,
  ``/usr/lib64``, the ``/lib*`` links), the few ``/etc`` files the loader,
  name resolution and TLS need, the CA bundles (and the system CA paths
  ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` name) and the time zones; ``/proc`` of
  the new pid namespace (see below) and a minimal ``/dev``.
- Writable: agy's store (``$HOME/.gemini/antigravity-cli``, where its sign-in
  token is refreshed) and the request's private ``TMPDIR``. Nothing else.
- The binary, by its real path, read-only, bound after the store; then the
  whole root is remounted read-only (HOME, the binary's folder...).

Not in the cage: ``/usr/bin``, ``/usr/local``, ``/app``, the rest of HOME
(``~/.claude``, ``~/.codex``...), ``/tmp``, ``/sys``.

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
bwrap is missing, the platform is not Linux, or the store or ``TMPDIR`` path
goes through a symbolic link; :func:`setup_failure` recognises a bwrap that
started but could not build the cage.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import List, Mapping, Optional, Sequence

from plugboard.agy_store import STORE_PARTS

log = logging.getLogger(__name__)


class CageUnavailable(Exception):
    """The cage cannot be built, so agy must not run.

    ``str()`` is the internal cause, for the gateway log and the admin only.
    """


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
    "--unshare-uts",
    "--unshare-cgroup-try",
    "--die-with-parent",
)
# bwrap's words when the kernel refuses a new procfs (a Docker container).
_PROC_REFUSED = "Can't mount proc"
_PROC_CHECK_TIMEOUT_S = 10.0

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
    and is reported as usual) and the next call checks again.
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
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={},
            timeout=_PROC_CHECK_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if proc.returncode == 0:
        _proc_mount = True
    elif _PROC_REFUSED in (proc.stderr or b"").decode("utf-8", "replace"):
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
