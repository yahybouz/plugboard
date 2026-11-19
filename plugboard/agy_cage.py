"""The bubblewrap cage every agy launch runs in (BUG-86).

agy runs a tenant's prompt with the gateway's uid. Its own guards (headless
refusal of tools, ``--sandbox``) are agy's word only; the cage is the
gateway's: whatever agy does, it sees and changes only what is listed here.
This module builds the gateway's only command line that launches agy
(``complete``, ``agy models``, ``agy --version``; the operator's
``scripts/bench_latence.py`` runs agy bare, BUG-110):

- ``bwrap`` (the image's at :data:`GATEWAY_BWRAP`, off PATH so that codex
  keeps its own, INC-4; else the one on PATH) with its own user, pid, ipc,
  network, uts (and, where possible, cgroup)
  namespaces, ended with the gateway (``--die-with-parent``). The pid
  namespace keeps ``/proc/<gateway pid>/environ`` (the server's keys) out of
  sight. agy is its pid 1 (``--as-pid-1``): bwrap's own init there would be
  left unreaped, a zombie of the gateway when it is its container's PID 1.
  No ``--new-session``: bwrap stays in the group the gateway kills on
  timeout or cancellation.
- No network but the cage's own loopback (``--unshare-net``, BUG-156,
  decision D-85): the internet, the internal network, the cloud metadata
  address, the host's loopback (the gateway's own port included) and the
  abstract sockets of other sessions are out of reach. The one way out is
  the unix socket :data:`EGRESS_SOCKET` the gateway creates in the request's
  ``TMPDIR`` (one per call, removed with it) and serves with a CONNECT proxy
  limited to the Google hosts agy needs (:mod:`plugboard.agy_egress`); it
  is bound read-only over itself, so agy can neither remove nor replace it.
  agy speaks HTTP(S) through a proxy, not through a unix socket: the
  command the cage runs is a relay (:data:`_RELAY`, the gateway's own
  Python, ``-I -S``, its code using the standard library only) that
  listens on
  ``127.0.0.1:``:data:`RELAY_PORT` of the cage, forwards each connection to
  the socket, and becomes agy (``execv``: agy stays pid 1, the relay goes on
  in a child of it and dies with the cage). agy's ``HTTPS_PROXY`` and
  ``HTTP_PROXY`` name the relay, ``ALL_PROXY`` and ``NO_PROXY`` are empty
  (:func:`proxy_env`, set by the caller through execve). (BUG-109 had kept
  the network because ``--unshare-net`` alone left agy no internet.)
- An empty root holding, read-only, the system libraries (``/usr/lib``,
  ``/usr/lib64``, the ``/lib*`` links), the few ``/etc`` files the loader,
  name resolution and TLS need, the CA bundles (and the system CA paths
  ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` name) and the time zones; ``/proc`` of
  the new pid namespace (see below) and a minimal ``/dev``.
- Writable: a store of the call's own (BUG-157), bound at the path of agy's
  store (``$HOME/.gemini/antigravity-cli``) instead of it, and the request's
  private ``TMPDIR``. Nothing else. The shared store is never bound
  writable: the call's store is a fresh folder
  (:func:`plugboard.agy_store.create_private_store`, 0700, beside the
  ``TMPDIR``, removed with it after the call) which
  :func:`fill_private_store` fills, once the call has its CLI slot and off
  the event loop (``cli_subprocess.agy_caged``), with copies, never links, of the
  sign-in token (:data:`STORE_TOKEN`, which agy may refresh: the copy is
  thrown away, never copied back) and of what agy unpacks from its binary,
  ``bin/`` and ``builtin/`` (:data:`STORE_UNPACKED`, which agy may rewrite:
  the same). Everything else (conversations, summaries, ``cache/``,
  ``log/``, ``mcp/``, ``updater/``, ``plugin_data/``...) starts empty. So a
  prompt can neither put another account's token in place for the next
  calls, nor leave a program in ``bin/`` that a later call runs, nor read
  the conversation of another call in progress: none of it is in its store.
- Read-only, from the shared store, at their path in the call's store, each
  when it exists: agy's settings and identity (:data:`STORE_READ_ONLY`,
  BUG-108).
- Read-only, right after the store: agy's own config beside it,
  ``$HOME/.gemini/config`` (its MCP servers' ``mcp_config.json`` included),
  which agy 1.4.4 cannot start without, and gemini's
  ``$HOME/.gemini/projects.json`` when it exists, never required (BUG-115,
  see :func:`_agy_config`).
- The binary, by its real path, read-only, bound after the store, and the
  relay's interpreter (the gateway's Python by its real path, its standard
  library, ``libpython`` when shared; in the image, under ``/usr/local``),
  its third-party packages (``site-packages`` of that standard library in
  the image) hidden under an empty tmpfs (PLU-171); then the whole root is
  remounted read-only (HOME, the binary's folder...).

Not in the cage: ``/usr/bin`` and ``/usr/local`` but for that interpreter,
``/app``, the rest of HOME (``~/.claude``, ``~/.codex``, the rest of
``~/.gemini``...), ``/tmp``, ``/sys``, any unix socket file but the egress.

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

Limit (R8 of the P0-5 replay): agy must read its sign-in token, so a prompt
can read the call's copy of it. Taking it out of the cage (agy fed through a
TLS-intercepting proxy holding the token) is out of reach of this release;
what the token can reach from the cage is bounded by its egress, the Google
hosts agy needs (BUG-156). A refresh agy makes in its copy is lost with it:
when the stored access token has expired, every call refreshes it again
(one more round trip to Google per call, until the shared token is renewed
outside the cage, by agy run in a shell of the gateway's user).

No cage, no agy: there is no variable, option or fallback that launches agy
without it. :class:`CageUnavailable` is raised before anything starts when
bwrap is missing, the platform is not Linux, the egress socket is missing
or not a socket, the relay's interpreter is missing, the store, ``TMPDIR``, agy's
config or ``projects.json`` path goes through a symbolic link, a read-only
entry of the store is a link, of the wrong type or hard-linked, the token,
``bin/`` or ``builtin/`` is a link or of the wrong type, the call's store
is not an empty private folder or cannot be filled (or made, see
``cli_subprocess.agy_caged``), or agy was never run on the account
(:class:`AgyNotSetUp`); :func:`setup_failure` recognises a bwrap that
started but could not build the cage, and a relay that stopped before agy
(it then never runs).
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
from typing import Dict, List, Mapping, Optional, Sequence

from plugboard import net_cage, web_egress
from plugboard.agy_store import STORE_PARTS

log = logging.getLogger(__name__)


# The cage cannot be built, so agy must not run (``str()``: the internal
# cause, for the gateway log and the admin only). The same class as the web
# cage's (plugboard.net_cage).
CageUnavailable = net_cage.CageUnavailable


class AgyNotSetUp(CageUnavailable):
    """agy was never run on this server account: its ``~/.gemini/config``
    is missing (see :func:`_agy_config`)."""


# What agy 1.4.4 reads under ~/.gemini outside its store (real UAT run,
# 15/11): config/ (.migrated, mcp_config.json, projects/). projects.json
# beside it is gemini's, shown when it exists (BUG-115).
_CONFIG_DIR = (".gemini", "config")
_PROJECTS_FILE = (".gemini", "projects.json")

# What agy keeps in its store and never changes at a call (BUG-108), shown
# read-only from the shared store over the call's own, each file at its own
# path when it exists: its settings (``settings.json``, where permission
# rules would give every later prompt a tool) and its identity
# (``installation_id``, ``jetski_state.pbtxt``: written at its first run,
# unchanged since on the UAT and on a host). What agy writes at every call
# (the conversations and their summaries, ``cache/``, ``log/`` and
# ``cli.log``, ``implicit/``, ``crashes/``, ``mcp/``, ``updater/``) starts
# empty in the call's store and is thrown away with it (BUG-157).
STORE_READ_ONLY = (
    "settings.json",
    "installation_id",
    "jetski_state.pbtxt",
)

# What agy unpacks from its own binary, ``bin/`` (programs it runs) and
# ``builtin/`` (skills it loads, with a ``.checksum``): copied into the
# call's store, writable there, thrown away after it (BUG-157), never bound
# from the shared store. agy writes there after its install (on a real
# store, files dated days after ``installation_id``: at a new version or on
# demand, review M2 of 4.14.4). Read-only, agy 1.4.4 only logs "failed to
# populate builtins" when its ``builtin/.checksum`` differs (offline run,
# 19/11); what it does with a read-only ``bin/`` once signed in could not be
# shown without a real call: a copy keeps both writable, at the cost of
# copying them at each call, and a program left there by a call is gone
# with it.
STORE_UNPACKED = ("bin", "builtin")
# agy's sign-in token: copied into the call's store (agy reads it, may
# refresh it there), never bound from the shared store, never copied back.
STORE_TOKEN = "antigravity-oauth-token"


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
    # No network but the cage's own loopback (BUG-156): agy goes out through
    # the relay and the egress socket only (see EGRESS_SOCKET).
    "--unshare-net",
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
# The egress of the cage (BUG-156, D-85): the unix socket the gateway serves
# (plugboard.agy_egress), by this name in the request's TMPDIR, bound
# read-only over itself; the relay listens on this port of the cage's own
# loopback, and agy's HTTPS_PROXY names it (proxy_env). Shared with the cage
# of claude's web runs (plugboard.net_cage, PLU-162).
EGRESS_SOCKET = net_cage.EGRESS_SOCKET
RELAY_PORT = net_cage.RELAY_PORT
# What the relay writes first on stderr when it stops before agy (agy then
# never ran): a cage failure, like bwrap's own "bwrap: " (setup_failure).
RELAY_ERROR_PREFIX = net_cage.RELAY_ERROR_PREFIX
# The relay (net_cage.AGY_RELAY), run by the gateway's own Python (-I -S: no
# site, no variable, no folder of the cage on its path), its code using the
# standard library only (the site-packages of the folder mounted with it are
# hidden, see _relay_python). It checks the egress is a socket,
# listens on the cage's loopback, forks the relay itself (its standard
# streams on /dev/null: agy's output stays agy's), then becomes agy by
# execv: agy keeps the cage's pid 1 and the environment the gateway passed
# to execve. The relay dies with agy: it watches agy, its parent, and ends
# once agy is gone (BUG-157), and the kernel ends the cage's pid namespace
# with its pid 1 anyway. Argument order: port, socket, agy, args.
_RELAY = net_cage.AGY_RELAY

# bwrap's words when the kernel refuses a new procfs (a Docker container).
_PROC_REFUSED = "Can't mount proc"
_PROC_CHECK_TIMEOUT_S = 5.0

# Whether the cage can mount a procfs of its own: None until checked, then
# kept for the life of the process (see _proc_mountable).
_proc_mount: Optional[bool] = None


# CA variables already reported as ignored (logged once each).
_ca_refused: set = set()
# The gateway's bwrap in the image: a fixed path, off PATH (INC-4). codex
# prefers a bwrap found on PATH to the one it ships (codex-resources/bwrap);
# with Debian's at /usr/bin/bwrap, codex took it and every codex call failed
# (``bwrap: Can't mount proc on /proc``, production and UAT, 15/11). Named
# here, never by a variable. Outside the image (a workstation, a host), bwrap
# is looked up on PATH.
GATEWAY_BWRAP = "/usr/libexec/plugboard/bwrap"
# bwrap's real path, resolved once (like the CLIs' binaries, TM-001: a bwrap
# put on PATH later is never used).
_bwrap_path: Optional[str] = None


def _reset_for_tests() -> None:
    """Forget what was checked or resolved once per process (tests only)."""
    global _proc_mount, _bwrap_path
    _proc_mount = None
    _bwrap_path = None
    _ca_refused.clear()


def _bwrap() -> str:
    """bwrap's real path, resolved the first time only: :data:`GATEWAY_BWRAP`
    (the image's) when it is an executable file, else on PATH."""
    global _bwrap_path
    if _bwrap_path and os.path.isfile(_bwrap_path):
        return _bwrap_path
    if os.path.isfile(GATEWAY_BWRAP) and os.access(GATEWAY_BWRAP, os.X_OK):
        found: Optional[str] = GATEWAY_BWRAP
    else:
        found = shutil.which("bwrap")
    if found is None:
        raise CageUnavailable(f"bwrap not found on PATH nor at {GATEWAY_BWRAP}")
    _bwrap_path = os.path.realpath(found)
    return _bwrap_path


def proxy_env() -> Dict[str, str]:
    """agy's proxy variables: every connection to the cage's relay, never an
    inherited proxy (the caller passes them through execve)."""
    return web_egress.proxy_env(RELAY_PORT)


def _egress_socket(tmpdir: str) -> str:
    """The egress socket the gateway made in ``tmpdir``, else
    :class:`CageUnavailable` (no egress, no agy)."""
    return net_cage.egress_socket(tmpdir, "agy")


# The gateway's Python and what it needs to start with -I -S, shown
# read-only to the relay (see net_cage.relay_python: the standard library's
# folder, site-packages included in the image).
_relay_python = net_cage.relay_python
_plain_path = net_cage.plain_path


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
    """``~/.gemini/config`` (a real folder) and, when it exists,
    ``~/.gemini/projects.json`` (a real regular file) under the child's
    HOME, shown read-only to agy.

    agy 1.4.4 reads ``config/`` outside its store; without it it tries to
    create it and every call fails ("mkdir ~/.gemini/config: read-only file
    system", UAT, 15/11). It holds agy's own settings, its MCP servers
    (``config/mcp_config.json``) included: agy may read it, never change it.
    Missing: :class:`AgyNotSetUp`, and nothing is created. agy writes it
    itself the first time it is run in a shell of the gateway user, which the
    sign-in needs anyway; the gateway does not make up agy's own formats
    (``mcp_config.json``) and never writes in ``~/.gemini`` outside agy's
    store.

    ``projects.json`` is not agy's (BUG-115): the agy 1.4.4 binary names no
    ``~/.gemini/projects.json``, only its own ``cache/projects.json`` in its
    store (its changelog, 1.0.4: workspace-to-project mappings moved
    there); ``~/.gemini/projects.json`` is the project registry of the
    gemini CLI (0.65.0, ``ProjectRegistry`` under its runtime folder
    ``~/.gemini``, written through ``projects.json.<uuid>.tmp``). It is shown
    read-only when it exists (as since 4.14.2), never required: missing, it
    is skipped and nothing is created.

    Through a symbolic link (bwrap would follow it) or of another type:
    :class:`CageUnavailable`.
    """
    home = os.path.normpath(env.get("HOME") or "")
    config = os.path.join(home, *_CONFIG_DIR)
    projects = os.path.join(home, *_PROJECTS_FILE)
    if not os.path.lexists(config):
        raise AgyNotSetUp(
            "agy is not set up for this server account: ~/.gemini/config is "
            "missing (run agy once in a shell of the gateway user, as for its "
            "sign-in)"
        )
    shown = [config]
    checks = [(config, "a directory", stat.S_ISDIR)]
    if os.path.lexists(projects):
        shown.append(projects)
        checks.append((projects, "a regular file", stat.S_ISREG))
    for path, kind, wanted in checks:
        name = "~/" + os.path.relpath(path, home)
        if not _plain_path(path):
            raise CageUnavailable(f"agy's {name} goes through a symbolic link")
        if not wanted(os.lstat(path).st_mode):
            raise CageUnavailable(f"agy's {name} is not {kind}")
    return shown


def _store_read_only(store: str) -> List[str]:
    """The entries of :data:`STORE_READ_ONLY` that exist in ``store``, to
    bind read-only over it (BUG-108).

    Missing: skipped, never created (agy's own files, in its own format).
    Through a symbolic link (bwrap would follow it), not a regular file, or a
    file with a second name (a hard link in the writable store would change
    it behind its read-only mount): :class:`CageUnavailable`.
    """
    found: List[str] = []
    for name in STORE_READ_ONLY:
        path = os.path.join(store, name)
        if not os.path.lexists(path):
            continue
        if not _plain_path(path):
            raise CageUnavailable(
                f"agy's store entry {name} goes through a symbolic link"
            )
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            raise CageUnavailable(f"agy's store entry {name} is not a regular file")
        elif info.st_nlink != 1:
            raise CageUnavailable(f"agy's store entry {name} has another hard link")
        found.append(path)
    return found


def _private_store(path: str, store: str) -> str:
    """``path``, the call's own store, when it is what the gateway made: an
    empty real folder (no link on its path), owned by the gateway, closed to
    others (0700), neither the shared store nor inside it nor holding it.
    Else :class:`CageUnavailable`."""
    if not _plain_path(path):
        raise CageUnavailable("agy private store path goes through a symbolic link")
    try:
        info = os.lstat(path)
        empty = not os.listdir(path)
    except OSError as exc:
        raise CageUnavailable(
            f"agy private store is missing ({type(exc).__name__})"
        ) from None
    norm = os.path.normpath(path)
    if (
        norm == store
        or norm.startswith(store + os.sep)
        or store.startswith(norm + os.sep)
    ):
        raise CageUnavailable("agy private store is not apart from the shared one")
    if not stat.S_ISDIR(info.st_mode):
        raise CageUnavailable("agy private store is not a directory")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CageUnavailable("agy private store is not private (0700)")
    if not empty:
        raise CageUnavailable("agy private store is not empty")
    return norm


_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _copy_file(src_dir: int, name: str, target: str, mode: int) -> None:
    """Copy the regular file ``name`` of the folder ``src_dir`` (a
    descriptor) to the new file ``target``, never through a link."""
    source = os.open(name, _OPEN_FLAGS, dir_fd=src_dir)
    try:
        if not stat.S_ISREG(os.fstat(source).st_mode):
            return  # replaced meanwhile by something else: skipped
        out = os.open(
            target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode
        )
        try:
            while True:
                chunk = os.read(source, 1 << 20)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(out, view) :]
        finally:
            os.close(out)
    finally:
        os.close(source)


def _copy_tree(src_dir: int, target: str) -> None:
    """Copy the folder ``src_dir`` (a descriptor) to the new folder
    ``target``: its folders and regular files, the owner's rights only; its
    links and special files are skipped, never followed nor opened."""
    os.mkdir(target, stat.S_IRWXU)
    with os.scandir(src_dir) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            path = os.path.join(target, entry.name)
            if stat.S_ISDIR(info.st_mode):
                sub = os.open(entry.name, _OPEN_FLAGS | os.O_DIRECTORY, dir_fd=src_dir)
                try:
                    _copy_tree(sub, path)
                finally:
                    os.close(sub)
            elif stat.S_ISREG(info.st_mode):
                mode = stat.S_IMODE(info.st_mode) & stat.S_IRWXU | 0o600
                _copy_file(src_dir, entry.name, path, mode)


def _copied_entries(store: str) -> List[str]:
    """The entries of the shared store copied into the call's own: the token
    (:data:`STORE_TOKEN`) and what agy unpacks (:data:`STORE_UNPACKED`),
    those that exist.

    The token or one of those folders through a symbolic link, or of the
    wrong type: :class:`CageUnavailable` (as for :data:`STORE_READ_ONLY`).
    A missing token is not refused: agy then says it is not signed in, as
    before.
    """
    wanted = [(STORE_TOKEN, "a regular file", stat.S_ISREG)]
    wanted += [(name, "a directory", stat.S_ISDIR) for name in STORE_UNPACKED]
    found = []
    for name, kind, check in wanted:
        path = os.path.join(store, name)
        if not os.path.lexists(path):
            continue
        if not _plain_path(path):
            raise CageUnavailable(
                f"agy's store entry {name} goes through a symbolic link"
            )
        if not check(os.lstat(path).st_mode):
            raise CageUnavailable(f"agy's store entry {name} is not {kind}")
        found.append(name)
    return found


def _fill_private_store(store: str, private: str) -> None:
    """Copy into the call's store what agy needs of the shared one and may
    change (:func:`_copied_entries`, checked again here). Nothing is linked,
    nothing ever goes back the other way; a link or a special file inside a
    folder is skipped."""
    found = _copied_entries(store)
    try:
        folder = os.open(store, _OPEN_FLAGS | os.O_DIRECTORY)
        try:
            for name in found:
                target = os.path.join(private, name)
                if name == STORE_TOKEN:
                    _copy_file(folder, name, target, 0o600)
                    continue
                sub = os.open(name, _OPEN_FLAGS | os.O_DIRECTORY, dir_fd=folder)
                try:
                    _copy_tree(sub, target)
                finally:
                    os.close(sub)
        finally:
            os.close(folder)
    except (OSError, RecursionError) as exc:
        # RecursionError: a tree deeper than Python's stack in bin/ or
        # builtin/ (_copy_tree recurses); never agy's own layout.
        raise CageUnavailable(
            f"agy private store cannot be filled ({type(exc).__name__})"
        ) from None


def fill_private_store(env: Mapping[str, str], private_store: str) -> None:
    """Fill the call's store ``private_store`` (still empty, checked again)
    with copies of the shared store's token, ``bin/`` and ``builtin/``
    (BUG-157). Called after :func:`cage_argv` and before the launch; it
    reads and writes files, possibly megabytes: the gateway runs it in a
    thread, once the call has its CLI slot. Raises :class:`CageUnavailable`
    (agy must then not run)."""
    store = _store(env)
    _fill_private_store(store, _private_store(private_store, store))


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
    private_store: str,
    _extra_ro_for_tests: Sequence[str] = (),
) -> List[str]:
    """The command line that runs ``binary args...`` inside the cage.

    ``env`` is the child's environment (its ``HOME`` locates the store, its
    CA variables are bound); the caller passes the same ``env`` to execve.
    ``tmpdir`` is the request's private ``TMPDIR``, writable and the working
    directory. ``private_store`` is the call's own store, an empty 0700
    folder the caller made and removes after the call
    (:func:`plugboard.agy_store.create_private_store`), bound at the
    store's path, writable, instead of the store (BUG-157); it has no
    default. The caller fills it with :func:`fill_private_store` before the
    launch. ``_extra_ro_for_tests`` is for the test suite only (a fake agy
    written in shell needs ``/usr/bin`` and ``/bin``): the gateway never
    passes it.

    Raises :class:`CageUnavailable`, before anything is launched, when the
    cage cannot be built.
    """
    if not sys.platform.startswith("linux"):
        raise CageUnavailable("agy sandbox needs Linux")
    bwrap = _bwrap()
    store = _store(env)
    config = _agy_config(env)
    settings = _store_read_only(store)
    if not _plain_path(tmpdir) or not os.path.isdir(tmpdir):
        raise CageUnavailable("agy TMPDIR path goes through a symbolic link")
    real_binary = os.path.realpath(binary)
    if not os.path.isfile(real_binary):
        raise CageUnavailable("agy binary not found")
    egress = _egress_socket(tmpdir)
    python = _relay_python()
    private = _private_store(private_store, store)
    # What will be copied into it (fill_private_store): checked now, before
    # the call waits for its slot.
    _copied_entries(store)

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
        # The call's own store at the store's path: the shared store itself
        # is never bound writable (BUG-157).
        "--bind",
        private,
        store,
    ]
    # agy's settings, from the shared store, over the call's own; then its
    # config beside the store (and gemini's projects.json when it exists):
    # read-only, each at its path.
    for path in (*settings, *config):
        argv += ["--ro-bind", path, path]
    argv += [
        "--bind",
        tmpdir,
        tmpdir,
        # The egress socket, over itself in the writable TMPDIR: agy can
        # neither remove nor replace a mount point (EBUSY).
        "--ro-bind",
        egress,
        egress,
        # After the store: read-only even if the binary sits inside it.
        "--ro-bind",
        real_binary,
        real_binary,
    ]
    # The relay's interpreter and its standard library, read-only; its
    # third-party packages (site-packages in the image's standard library
    # folder) hidden under an empty read-only tmpfs (PLU-171; the root's
    # remount below does not reach a tmpfs).
    for path in python:
        argv += ["--ro-bind", path, path]
    for path in net_cage.relay_site_packages(python):
        argv += ["--tmpfs", path, "--remount-ro", path]
    argv += [
        "--remount-ro",
        "/",
        "--chdir",
        tmpdir,
        "--",
        python[0],
        "-I",
        "-S",
        "-c",
        _RELAY,
        str(RELAY_PORT),
        egress,
        real_binary,
        *args,
    ]
    return argv


# bwrap's or the relay's first error line when the cage could not be built
# or its relay could not start (agy then never ran), else None.
setup_failure = net_cage.setup_failure
