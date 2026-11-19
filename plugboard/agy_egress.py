"""The only way out of agy's cage (BUG-156, decision D-85).

agy's cage has no network of its own (``--unshare-net``, see
:mod:`plugboard.agy_cage`). Each agy call gets one unix socket, created by
the gateway in the request's private ``TMPDIR`` and bound read-only into the
cage, and served here by a CONNECT proxy: the one of the web tools
(:func:`plugboard.web_egress.serve_connect`: port 443 only, every address
resolved once here and checked public by ``netguard.ip_is_public``, the
tunnel opened to the checked address) with, on top, a list of hosts. Any
other host, an internal address (loopback, private ranges, the tailnet's
CGNAT range, link-local, the cloud metadata address, internal IPv6), even
for a listed name that resolves there, and any port but 443: ``403``,
logged with the host only, once per host and window (a summary line
then says how many more were refused). At most
``web_egress.MAX_CONNECTIONS`` connections at once per call, ``503``
beyond.

The list holds exact names, each found in the agy 1.4.4 binary the image
installs (its strings, read offline on 19/11; agy was not run). It filters
the NAMES asked in ``CONNECT``, not Google's services, and is no guarantee
that agy only talks to the services named below (known limit, a channel
to Google remains):

- a listed name serves more than what agy uses it for:
  ``www.googleapis.com`` also serves, for one, the Cloud Storage JSON API
  (``/storage/v1``, ``/upload/storage/v1``), which reaches any bucket the
  credentials in the request can write;
- the tunnel is opaque (TLS end to end): the proxy never sees the SNI nor
  the ``Host`` inside it, which may name another Google service than the
  ``CONNECT`` did, Google's front ends serving many names on the same
  addresses.

What the list does rule out: any non-Google host, and the other Google
names by their own (``storage.googleapis.com``...). Each listed name:

- ``oauth2.googleapis.com``: ``https://oauth2.googleapis.com/token``, where
  agy refreshes its sign-in token (``antigravity-oauth-token``).
- ``www.googleapis.com``: ``https://www.googleapis.com/oauth2/v2/userinfo``
  and ``.../oauth2/v3/tokeninfo``, the account behind that token.
- ``cloudcode-pa.googleapis.com`` and ``daily-cloudcode-pa.googleapis.com``:
  the Cloud Code backend (``CLOUD_CODE_URL``; ``LoadCodeAssist``,
  ``FetchAvailableModels``, the model calls).
- ``aicode.googleapis.com``: ``Making Gateway request to: %s`` with
  ``https://aicode.googleapis.com``, beside agy's standalone token
  (``jetski-standalone-oauth-token``).

Left out, though in the binary: the interactive sign-in
(``accounts.google.com``, done in a shell of the gateway user, never by the
gateway), the auto-updater (``*.run.app``; ``AGY_CLI_DISABLE_AUTO_UPDATE=1``),
telemetry (``play.googleapis.com/log``), the API-key endpoint
(``generativelanguage.googleapis.com``; agy takes no key here), Google's own
test environments (``*.sandbox.googleapis.com``, ``autopush-``, ``preprod-``,
``staging-``) and everything agy's IDE features name (Drive, Docs...). A host
agy turns out to need is refused and named in the gateway log
(``agy egress refused: host=...``): it is added here, never by a variable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from typing import Set

from plugboard import web_egress

ALLOWED_HOSTS = frozenset(
    {
        "oauth2.googleapis.com",
        "www.googleapis.com",
        "cloudcode-pa.googleapis.com",
        "daily-cloudcode-pa.googleapis.com",
        "aicode.googleapis.com",
    }
)
_LABEL = "agy egress"


def host_allowed(host: str) -> bool:
    """Whether ``host`` is one of :data:`ALLOWED_HOSTS` (exact name, any
    case, a final dot ignored)."""
    return host.lower().removesuffix(".") in ALLOWED_HOSTS


class Egress:
    """The proxy of one agy call, on its unix socket."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._server: asyncio.AbstractServer | None = None
        self._tasks: Set[asyncio.Task] = set()
        # At most web_egress.MAX_CONNECTIONS at once, refusals logged once
        # per host and window: agy, or a prompt driving it, cannot flood
        # the gateway nor its log through its egress.
        self._cap = web_egress.ConnectionCap(_LABEL)
        self._refusals = web_egress.RefusalLog(_LABEL)

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
            await web_egress.serve_connect(
                reader,
                writer,
                host_allowed=host_allowed,
                label=_LABEL,
                refusals=self._refusals,
            )
        finally:
            self._cap.leave()
            if task is not None:
                self._tasks.discard(task)

    async def close(self) -> None:
        """Stop listening, end the tunnels still open and remove the socket.

        Called once agy's cage is dead: nothing is left to serve.
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


# A unix socket's name holds at most 107 bytes (sun_path).
_SUN_PATH_MAX = 107


def _bind(path: str) -> socket.socket:
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


async def start(path: str) -> Egress:
    """Listen on a new unix socket at ``path`` (raises ``OSError``)."""
    egress = Egress(path)
    sock = _bind(path)
    try:
        egress._server = await asyncio.start_unix_server(
            egress._handle, sock=sock, limit=web_egress._MAX_HEAD * 2
        )
    except BaseException:
        sock.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        raise
    return egress
