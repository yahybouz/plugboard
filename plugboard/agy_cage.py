"""The bubblewrap cage every agy launch runs in (BUG-86).

agy runs a tenant's prompt with the gateway's uid. Its own guards (headless
refusal of tools, ``--sandbox``) are agy's word only; the cage is the
gateway's: whatever agy does, it sees and changes only what is listed here.
This module builds the only command line that launches agy (``complete``,
``agy models``, ``agy --version``):

- ``bwrap`` with its own user, pid, ipc, uts (and, where possible, cgroup)
  namespaces, ended with the gateway (``--die-with-parent``). The pid
  namespace keeps ``/proc/<gateway pid>/environ`` (the server's keys) out of
  sight. The network is shared: agy needs it. No ``--new-session``: bwrap
  stays in the group the gateway kills on timeout or cancellation.
- An empty root holding, read-only, the system libraries (``/usr/lib``,
  ``/usr/lib64``, the ``/lib*`` links), the few ``/etc`` files the loader,
  name resolution and TLS need, the CA bundles and the time zones; ``/proc`` of
  the new pid namespace and a minimal ``/dev``.
- Writable: agy's store (``$HOME/.gemini/antigravity-cli``, where its sign-in
  token is refreshed) and the request's private ``TMPDIR``. Nothing else.
- The binary, by its real path, read-only, bound after the store; then the
  whole root is remounted read-only (HOME, the binary's folder...).

Not in the cage: ``/usr/bin``, ``/usr/local``, ``/app``, the rest of HOME
(``~/.claude``, ``~/.codex``...), ``/tmp``, ``/sys``.

The environment reaches agy through execve (the caller's ``env=``), never the
command line nor ``--setenv``: a proxy URL can hold a password.

No cage, no agy: there is no variable, option or fallback that launches agy
without it. :class:`CageUnavailable` is raised before anything starts when
bwrap is missing, the platform is not Linux, or the store or ``TMPDIR`` path
goes through a symbolic link; :func:`setup_failure` recognises a bwrap that
started but could not build the cage.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import List, Mapping, Optional, Sequence

from plugboard.agy_store import STORE_PARTS


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
# CA locations the child's environment may name.
_CA_VARIABLES = ("SSL_CERT_FILE", "SSL_CERT_DIR")


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
        path = env.get(name) or ""
        if os.path.isabs(path) and os.path.exists(path):
            args += ["--ro-bind-try", path, path]
    return args


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
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise CageUnavailable("bwrap not found on PATH")
    store = _store(env)
    if not _plain_path(tmpdir) or not os.path.isdir(tmpdir):
        raise CageUnavailable("agy TMPDIR path goes through a symbolic link")
    real_binary = os.path.realpath(binary)
    if not os.path.isfile(real_binary):
        raise CageUnavailable("agy binary not found")

    argv = [
        bwrap,
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        *_system_args(env),
    ]
    for path in _extra_ro_for_tests:
        if os.path.islink(path):
            argv += ["--symlink", os.readlink(path), path]
        else:
            argv += ["--ro-bind", path, path]
    argv += [
        "--proc",
        "/proc",
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
