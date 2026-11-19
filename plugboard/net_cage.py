"""No network but one egress: what agy's cage and claude's web runs share.

Two cages run a CLI with no network of its own (``bwrap --unshare-net``):
agy's, for every agy call (:mod:`plugboard.agy_cage`, BUG-156), and the cage
of a claude web run (:func:`web_cage_argv`, PLU-162). In both, the only way
out is a unix socket the gateway creates, one per call, in a folder of that
call (:data:`EGRESS_SOCKET`), served by a CONNECT proxy of the gateway
(:class:`UnixEgress`) and bound read-only over itself, so the CLI can
neither remove nor replace it. A CLI speaks HTTP(S) through a proxy, not
through a unix socket: the command the cage runs is a relay (the gateway's
own Python, ``-I -S``, standard library only) that listens on
``127.0.0.1:``:data:`RELAY_PORT` of the cage's own loopback, forwards each
connection to the socket, and becomes the CLI (``execv``); the CLI's
``HTTPS_PROXY`` and ``HTTP_PROXY`` name the relay, ``ALL_PROXY`` and
``NO_PROXY`` are empty (:func:`proxy_env`).

The cage of a claude web run (PLU-162) is a network cage:
``bwrap --unshare-user --unshare-net --die-with-parent``, the whole root
bound as it is (``--bind / /``, then the host's ``/dev`` with its devices:
``/proc``, ``/tmp``, HOME, ``~/.claude``, node are the gateway's, as
before), the same uid, but for the CLIs' launch directory (claude's working
directory) and the gateway's root of call folders (out of the launch
directory, BUG-157): each an empty tmpfs, the root holding the run's own
folder only, so the folders of the other calls (``web-run-*``,
``agy-tmp-*``, ``agy-store-*``) and their egress sockets are out of reach; a
launch directory holding what claude needs is refused (:func:`web_cage_argv`). On the network, a
client that ignores ``HTTPS_PROXY`` (a direct connection to the gateway's
port, the internal network, the cloud metadata address, or any public
address) finds no route, and the egress of the run lets out port 443 of
public addresses only, as the web tools' proxy did (:mod:`plugboard.web_egress`),
with its own connection cap. It is not a file cage: claude still sees and
changes what the gateway's uid can (it has no shell nor file tool on a web
run, only WebSearch and WebFetch).

In both cages, the relay's half that serves connections watches the CLI it
became and ends once it is gone: a web cage has no pid namespace, and agy's,
whose end kills it too, is never its only way to die (BUG-157: a cage built
without one left a relay alive after each call). The web cage's relay
differs from agy's in one way: it hands
the CLI the environment the gateway gave, exactly: Python sets
``LC_CTYPE=C.UTF-8`` when it starts without a locale (PEP 538), whatever
``-I``, and bwrap sets ``PWD``; the gateway passes the values it set for
both, or none, and the relay puts them back before ``execv``.

No cage, no web run: bwrap missing (:func:`plugboard.agy_cage._bwrap`), the
platform not Linux, the socket missing or not a socket, the relay's
interpreter missing: :class:`CageUnavailable`, before anything starts; a
bwrap that started but could not build the cage, or a relay that stopped
before the CLI, is told apart by :func:`setup_failure`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import stat
import sys
import sysconfig
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set

from plugboard import web_egress


class CageUnavailable(Exception):
    """The cage cannot be built, so the CLI must not run.

    ``str()`` is the internal cause, for the gateway log and the admin only.
    """


# The egress of a cage: the unix socket the gateway serves, by this name in
# the call's own folder, bound read-only over itself; the relay listens on
# this port of the cage's own loopback, and the CLI's HTTPS_PROXY names it.
EGRESS_SOCKET = "egress.sock"
RELAY_PORT = 3128
# What the relay writes first on stderr when it stops before the CLI (the
# CLI then never ran): a cage failure, like bwrap's own "bwrap: ".
RELAY_ERROR_PREFIX = "plugboard-relay: "

# The relay, run by the gateway's own Python (-I -S: no site, no variable,
# no folder of the cage on its path), its code using the standard library
# only. It checks the egress is a socket, listens on the cage's loopback,
# forks the relay itself (its standard streams on /dev/null: the CLI's
# output stays the CLI's), then becomes the CLI by execv: the CLI keeps the
# relay's pid and the environment the gateway passed to execve. Built from
# one text: %(args)s reads the command line, %(watch)s ends the serving half
# with the CLI (both cages), %(restore)s is the web cage's only (see the
# module's docstring).
_RELAY_TEMPLATE = r"""
import os, signal, socket, stat, sys, threading%(imports)s
def fail(why):
    os.write(2, (%(prefix)r + why + "\n").encode())
    os._exit(70)
try:
    %(args)s
    if not stat.S_ISSOCK(os.lstat(path).st_mode):
        fail("the egress is not a socket")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(64)
except Exception as exc:
    fail("cannot listen on the cage's loopback: " + type(exc).__name__)
def pump(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass
def serve(conn):
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        up.connect(name)
    except OSError:
        conn.close()
        up.close()
        return
    back = threading.Thread(target=pump, args=(up, conn), daemon=True)
    back.start()
    pump(conn, up)
    back.join()
    conn.close()
    up.close()
%(parent)stry:
    pid = os.fork()
except OSError as exc:
    fail("cannot start: " + type(exc).__name__)
if pid == 0:
    try:
        # By its name in its folder: a socket's path holds 107 bytes at most.
        os.chdir(os.path.dirname(path))
        name = os.path.basename(path)
        null = os.open("/dev/null", os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
%(watch)s        while True:
            conn, _ = listener.accept()
            threading.Thread(target=serve, args=(conn,), daemon=True).start()
    finally:
        os._exit(0)
listener.close()
for sig in (signal.SIGPIPE, signal.SIGXFSZ):
    signal.signal(sig, signal.SIG_DFL)
%(restore)stry:
    os.execv(argv[0], argv)
except OSError as exc:
    fail("cannot run %(cli)s: " + type(exc).__name__)
"""


def _relay(cli: str, *, web: bool) -> str:
    # Both relays: the serving half ends once the CLI it became (its
    # parent) is gone. The web cage has no pid namespace; agy's has one,
    # whose end kills the relay too, but the relay never relies on it alone
    # (BUG-157: a cage without one left a relay alive per call).
    watched = {
        "imports": ", time",
        "parent": "parent = os.getpid()\n",
        "watch": (
            "        def orphaned():\n"
            "            while os.getppid() == parent:\n"
            "                time.sleep(0.2)\n"
            "            os._exit(0)\n"
            "        threading.Thread(target=orphaned, daemon=True).start()\n"
        ),
    }
    if not web:
        # agy's: port, socket, agy, its arguments.
        parts = {
            **watched,
            "args": "port, path, argv = int(sys.argv[1]), sys.argv[2], sys.argv[3:]",
            "restore": "",
        }
    else:
        # The web cage's: port, socket, LC_CTYPE and PWD as the gateway
        # gave them ("-" none, else "=" and the value), the CLI, its
        # arguments.
        parts = {
            **watched,
            "args": (
                "port, path, given, argv = (int(sys.argv[1]), sys.argv[2], "
                "sys.argv[3:5], sys.argv[5:])"
            ),
            # The environment the gateway gave: LC_CTYPE (Python's, PEP 538)
            # and PWD (bwrap's) as they were, in the process' own (execv
            # passes it on).
            "restore": (
                'for key, value in zip(("LC_CTYPE", "PWD"), given):\n'
                '    if value == "-":\n'
                "        os.unsetenv(key)\n"
                "    else:\n"
                "        os.putenv(key, value[1:])\n"
            ),
        }
    return _RELAY_TEMPLATE % {"prefix": RELAY_ERROR_PREFIX, "cli": cli, **parts}


AGY_RELAY = _relay("agy", web=False)
WEB_RELAY = _relay("claude", web=True)


def proxy_env() -> Dict[str, str]:
    """The CLI's proxy variables: every connection to the cage's relay,
    never an inherited proxy (the caller passes them through execve)."""
    return web_egress.proxy_env(RELAY_PORT)


def plain_path(path: str) -> bool:
    """Whether ``path`` is absolute and goes through no symbolic link."""
    return os.path.isabs(path) and os.path.realpath(path) == os.path.normpath(path)


def egress_socket(folder: str, who: str) -> str:
    """The egress socket the gateway made in ``folder``, else
    :class:`CageUnavailable` (no egress, no CLI). ``who`` names the CLI in
    the cause."""
    path = os.path.join(folder, EGRESS_SOCKET)
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        raise CageUnavailable(f"{who} egress socket is missing") from None
    if not stat.S_ISSOCK(mode):
        raise CageUnavailable(f"{who} egress socket is not a socket")
    return path


def relay_python() -> List[str]:
    """The gateway's Python, by its real path, then what it needs to start
    with ``-I -S`` (its standard library, ``lib-dynload``, ``libpython``
    when shared). Missing: :class:`CageUnavailable`.

    agy's cage shows them read-only to the relay. The standard library's
    folder is mounted whole: in the image (``/usr/local/lib/python3.12``) it
    holds ``site-packages``, the gateway's third-party dependencies, hidden
    under an empty read-only tmpfs in both cages
    (:func:`relay_site_packages`, PLU-171); ``-S`` keeps them off the
    relay's path anyway."""
    executable = os.path.realpath(sys.executable) if sys.executable else ""
    if not executable or not os.path.isfile(executable):
        raise CageUnavailable("the relay's Python interpreter is missing")
    found = [executable, os.path.realpath(os.path.dirname(os.__file__))]
    for entry in sys.path:
        if entry and os.path.basename(entry) == "lib-dynload" and os.path.isdir(entry):
            found.append(os.path.realpath(entry))
    if sysconfig.get_config_var("Py_ENABLE_SHARED"):
        library = os.path.join(
            sysconfig.get_config_var("LIBDIR") or "",
            sysconfig.get_config_var("INSTSONAME") or "",
        )
        if os.path.isfile(library):
            found.append(os.path.realpath(library))
    paths: List[str] = []
    for path in found:
        if path not in paths and not any(path.startswith(p + os.sep) for p in paths):
            paths.append(path)
    return paths


# The folders of third-party packages an interpreter may hold (PLU-171, I4).
_PACKAGES = ("site-packages", "dist-packages")


def relay_site_packages(within: Sequence[str]) -> List[str]:
    """The relay interpreter's folders of third-party packages that lie in
    ``within`` (what :func:`relay_python` shows), each a real folder
    reached through no link: ``site-packages`` and ``dist-packages`` of
    each folder of ``within``, and the interpreter's own ``purelib`` and
    ``platlib``. A cage hides each under an empty read-only tmpfs
    (PLU-171, I4): the relay (``-S``) never imports them, and the CLI has no
    business reading the gateway's dependencies."""
    candidates = [os.path.join(path, name) for path in within for name in _PACKAGES]
    paths = sysconfig.get_paths()
    candidates += [paths.get("purelib") or "", paths.get("platlib") or ""]
    roots = [os.path.normpath(path) for path in within]
    found: List[str] = []
    for candidate in candidates:
        path = os.path.normpath(candidate) if candidate else ""
        if not path or path in found:
            continue
        if not (plain_path(path) and os.path.isdir(path)):
            continue
        if any(path.startswith(root.rstrip(os.sep) + os.sep) for root in roots):
            found.append(path)
    return found


def setup_failure(
    returncode: Optional[int], stdout: Optional[bytes], stderr: Optional[bytes]
) -> Optional[str]:
    """bwrap's or the relay's first error line when the cage could not be
    built or its relay could not start (the CLI then never ran), else None.

    That is a non-zero exit, no output at all, and stderr starting with
    ``bwrap: `` (bwrap's own prefix) or :data:`RELAY_ERROR_PREFIX` (the
    CLIs' errors come on stdout as JSON).
    """
    if returncode in (0, None) or (stdout or b"").strip():
        return None
    text = (stderr or b"").decode("utf-8", "replace")
    if not text.startswith(("bwrap: ", RELAY_ERROR_PREFIX)):
        return None
    return text.splitlines()[0][:300]


# ── The cage of a claude web run (PLU-162) ──────────────────────────────────

# Its namespaces: its own user (the same uid, mapped onto itself) and its
# own network, nothing else; ended with the gateway. No pid namespace: the
# CLI sees /proc as it did before (the gateway kills its process group on
# timeout, as for any CLI).
WEB_NAMESPACES = ("--unshare-user", "--unshare-net", "--die-with-parent")
# What the relay gives back to the CLI as the gateway set it: Python adds
# LC_CTYPE when it starts without a locale (PEP 538), bwrap sets PWD.
_GIVEN_BACK = ("LC_CTYPE", "PWD")


def _within(path: str, folder: str) -> bool:
    """Whether ``path`` is ``folder`` or lies under it (both real paths)."""
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _hidden_launch_dir(
    launch_dir: str, command: Sequence[str], env: Mapping[str, str], python: List[str]
) -> str:
    """``launch_dir`` by its real path, once checked that hiding it hides
    nothing claude needs: its HOME, ``~/.claude`` (or ``CLAUDE_CONFIG_DIR``),
    its binary and the relay's interpreter stay out of it, else
    :class:`CageUnavailable` (an operator's ``PLUGBOARD_SUBPROCESS_CWD``
    holding them: the run is refused rather than run with the other calls'
    sockets in sight)."""
    hidden = os.path.realpath(launch_dir)
    needed = [os.path.realpath(command[0]), *python]
    home = env.get("HOME")
    if home:
        needed += [
            os.path.realpath(home),
            os.path.realpath(os.path.join(home, ".claude")),
        ]
    if env.get("CLAUDE_CONFIG_DIR"):
        needed.append(os.path.realpath(env["CLAUDE_CONFIG_DIR"]))
    if hidden == os.sep or any(_within(path, hidden) for path in needed):
        raise CageUnavailable(
            "the CLIs' launch directory holds what claude needs (its HOME, "
            "~/.claude, its binary or the relay's Python): it cannot be hidden"
        )
    return hidden


def web_cage_argv(
    bwrap: str,
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str,
    folder: str,
) -> List[str]:
    """The command line that runs ``command`` (claude and its arguments,
    unchanged) with no network but the egress socket of ``folder``.

    ``bwrap`` is the gateway's bwrap (``agy_cage._bwrap()``); ``env`` the
    CLI's environment, passed by the caller to execve (only its
    ``LC_CTYPE`` and ``PWD`` are read, for the relay to hand them back as
    they are, and its ``HOME`` and ``CLAUDE_CONFIG_DIR``, checked out of the
    launch directory); ``cwd`` the
    launch directory, kept; ``folder`` the run's own folder holding the
    socket, in the gateway's root of call folders (out of the launch
    directory, BUG-157). The launch directory and that root are hidden, each
    under an empty tmpfs, but for ``folder`` (PLU-162): the folders of the
    other calls (``web-run-*``, ``agy-tmp-*``, ``agy-store-*``) and their
    egress sockets are out of reach. Raises :class:`CageUnavailable` before
    anything is launched.
    """
    if not sys.platform.startswith("linux"):
        raise CageUnavailable("the web cage needs Linux")
    egress = egress_socket(folder, "web")
    python = relay_python()
    # The launch directory as before, then the root of call folders; the
    # outer one first when one holds the other, once when they are one.
    launch_dir = _hidden_launch_dir(cwd, command, env, python)
    root = _hidden_launch_dir(os.path.dirname(folder), command, env, python)
    emptied = (
        [launch_dir] if root == launch_dir else sorted([launch_dir, root], key=len)
    )
    given = [env.get(name) for name in _GIVEN_BACK]
    # The relay interpreter's third-party packages, under an empty
    # read-only tmpfs (PLU-171, I4).
    hidden: List[str] = []
    for path in relay_site_packages(python):
        hidden += ["--tmpfs", path, "--remount-ro", path]
    for path in emptied:
        hidden += ["--tmpfs", path]
    return [
        bwrap,
        *WEB_NAMESPACES,
        # The root as it is, then the host's /dev with its devices (bwrap
        # binds without them): the CLI's files are the gateway's, as before.
        "--bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        # Then the launch directory and the root of call folders, empty
        # (above), but for the run's own folder: the other calls' folders and
        # sockets are out of sight (PLU-162, BUG-157).
        *hidden,
        "--bind",
        folder,
        folder,
        # The egress socket, over itself: the CLI can neither remove nor
        # replace a mount point (EBUSY).
        "--ro-bind",
        egress,
        egress,
        "--chdir",
        cwd,
        "--",
        python[0],
        "-I",
        "-S",
        "-c",
        WEB_RELAY,
        str(RELAY_PORT),
        egress,
        *("-" if value is None else "=" + value for value in given),
        *command,
    ]


# ── The proxy on a cage's socket ────────────────────────────────────────────

# A unix socket's name holds at most 107 bytes (sun_path).
_SUN_PATH_MAX = 107


def bind_unix(path: str) -> socket.socket:
    """A unix socket bound at ``path``. A path longer than a socket's name
    (a long launch directory) is reached through the folder's descriptor,
    ``/proc/self/fd/<fd>/<name>``: the socket is made in that folder all the
    same."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if len(os.fsencode(path)) <= _SUN_PATH_MAX:
            sock.bind(path)
        else:
            folder = os.open(
                os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
            try:
                sock.bind(f"/proc/self/fd/{folder}/{os.path.basename(path)}")
            finally:
                os.close(folder)
    except BaseException:
        sock.close()
        raise
    return sock


class UnixEgress:
    """The CONNECT proxy of one call, on its unix socket: port 443 and
    public addresses only (:func:`plugboard.web_egress.serve_connect`), with
    ``host_allowed`` and ``match_sni`` on top for agy. At most
    ``web_egress.MAX_CONNECTIONS`` connections at once, ``503`` beyond;
    refusals logged once per host, port and window, all flushed at close.
    ``tunnel_opened`` and ``refused`` count its tunnels and refusals."""

    def __init__(
        self,
        path: str,
        *,
        label: str,
        host_allowed: Optional[Callable[[str], bool]] = None,
        match_sni: bool = False,
        tunnel_opened: Optional[Callable[[], None]] = None,
        refused: Optional[Callable[[], None]] = None,
    ) -> None:
        self.path = path
        self._server: asyncio.AbstractServer | None = None
        self._tasks: Set[asyncio.Task] = set()
        self._label = label
        self._host_allowed = host_allowed
        self._match_sni = match_sni
        self._tunnel_opened = tunnel_opened
        self._refused = refused
        # Its own cap and log: a CLI, or a prompt driving it, cannot flood
        # the gateway nor its log through its egress, nor take another
        # call's connections.
        self._cap = web_egress.ConnectionCap(label)
        self._refusals = web_egress.RefusalLog(label)

    @property
    def active_connections(self) -> int:
        return self._cap.active

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not self._cap.enter():
            await web_egress.reply_busy(writer)
            return
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            outcome = await web_egress.serve_connect(
                reader,
                writer,
                host_allowed=self._host_allowed,
                label=self._label,
                tunnel_opened=self._tunnel_opened,
                refusals=self._refusals,
                match_sni=self._match_sni,
            )
            if outcome is False and self._refused is not None:
                self._refused()
        finally:
            self._cap.leave()
            if task is not None:
                self._tasks.discard(task)

    async def close(self) -> None:
        """Stop listening, end the tunnels still open and remove the socket.

        Called once the cage is dead: nothing is left to serve.
        """
        if self._server is not None:
            self._server.close()
        for task in list(self._tasks):
            task.cancel()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        self._refusals.flush()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def listen(self) -> None:
        """Listen on a new unix socket at :attr:`path` (raises ``OSError``)."""
        sock = bind_unix(self.path)
        try:
            self._server = await asyncio.start_unix_server(
                self._handle, sock=sock, limit=web_egress._MAX_HEAD * 2
            )
        except BaseException:
            sock.close()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)
            raise


async def start_web_egress(path: str) -> UnixEgress:
    """The egress of one claude web run (PLU-162), listening at ``path``:
    the web tools' filter (port 443, public addresses, no list of hosts),
    its own cap, logged as ``web egress`` and counted in the web tools'
    figures (``web_egress.stats``). Raises ``OSError``."""
    egress = UnixEgress(
        path,
        label="web egress",
        tunnel_opened=web_egress.count_tunnel,
        refused=web_egress.count_refused,
    )
    await egress.listen()
    return egress
