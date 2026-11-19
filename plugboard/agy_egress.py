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
logged with the host and port only, once per host, port and window (a
summary line then says how many more were refused; past 256 of them, one
line for the others, naming at most 8). At most
``web_egress.MAX_CONNECTIONS`` connections at once per call, ``503``
beyond.

The list holds exact names, each found in the agy 1.4.4 binary the image
installs (its strings, read offline on 19/11; agy was not run), but one
named by agy's own error, its URL coming from Google (below). It filters
the NAMES asked in ``CONNECT``, not Google's services, and is no guarantee
that agy only talks to the services named below (known limit, a channel
to Google remains):

- a listed name serves more than what agy uses it for:
  ``www.googleapis.com`` also serves, for one, the Cloud Storage JSON API
  (``/storage/v1``, ``/upload/storage/v1``), which reaches any bucket the
  credentials in the request can write;
- the tunnel is TLS end to end: the proxy reads only its first record, the
  ClientHello, whose server name (SNI) must be the host of the ``CONNECT``
  (any case, a final dot ignored), else the tunnel is closed before a byte
  goes upstream (PLU-171): no fronting by SNI, no ClientHello without a
  name, nor one spread over several records, nor an ambiguous one (two
  names, bytes after the name list), nor an encrypted one (ECH, whose
  outer name is not the one served). The ``Host`` inside stays
  unseen and may name another Google service than the ``CONNECT`` did,
  Google's front ends serving many names on the same addresses.

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
- ``lh3.googleusercontent.com``: the account's profile picture, which agy's
  eligibility check fetches at start-up and fails without
  (``Eligibility check failed: %w``, ``failed to get profile picture: %w``).
  Its URL is not in the binary: it is the ``picture`` of the account's
  user info. Refused, it failed every agy call (502 ``Eligibility check
  failed: failed to get profile picture: Get
  https://lh3.googleusercontent.com/...``). Only that name: another
  ``lhN.`` host would be refused and logged. What it opens, honestly:
  Google's image CDN for user content, where any picture a Google user
  made public is readable by its URL (content anyone can publish, so a
  way in for data a prompt points agy at), behind the same shared front
  ends as the other names (the opaque tunnel above applies). A residual
  channel to Google of the same order as ``www.googleapis.com``.

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

from plugboard import net_cage

ALLOWED_HOSTS = frozenset(
    {
        "oauth2.googleapis.com",
        "www.googleapis.com",
        "cloudcode-pa.googleapis.com",
        "daily-cloudcode-pa.googleapis.com",
        "aicode.googleapis.com",
        "lh3.googleusercontent.com",
    }
)
_LABEL = "agy egress"


def host_allowed(host: str) -> bool:
    """Whether ``host`` is one of :data:`ALLOWED_HOSTS` (exact name, any
    case, a final dot ignored)."""
    return host.lower().removesuffix(".") in ALLOWED_HOSTS


class Egress(net_cage.UnixEgress):
    """The proxy of one agy call, on its unix socket: the list of hosts and
    the TLS name of each tunnel checked (PLU-171), on top of the web tools'
    filter (plugboard.net_cage.UnixEgress)."""

    def __init__(self, path: str) -> None:
        super().__init__(path, label=_LABEL, host_allowed=host_allowed, match_sni=True)


# A unix socket bound at a path of any length (net_cage.bind_unix).
_bind = net_cage.bind_unix


async def start(path: str) -> Egress:
    """Listen on a new unix socket at ``path`` (raises ``OSError``)."""
    egress = Egress(path)
    await egress.listen()
    return egress
