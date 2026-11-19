"""agy's store, private to each call and thrown away after it (BUG-157).

agy (Antigravity CLI 1.4.4) keeps everything in its store
``$HOME/.gemini/antigravity-cli``: its sign-in token
(``antigravity-oauth-token``, refreshed and rewritten by agy), its settings
and identity, what it unpacks from its own binary (``bin/``, ``builtin/``),
and every conversation, prompt in clear (``conversations/``, ``brain/``,
``annotations/``, ``presence/``, the shared summaries), plus ``cache/``,
``log/``, ``implicit/``, ``crashes/``, ``mcp/``, ``updater/``...

Up to 4.15.1 the cage bound that store writable, shared by every call (P0-5
replay, 18/11): a prompt could rewrite the token with the one of another
Google account (the next calls of every tenant then ran on it), leave a
program in ``bin/`` for a later call, and read the conversation of another
call in progress; the gateway removed each call's conversation afterwards
(BUG-97), too late for a neighbour still running.

Now each call gets a folder of its own, :func:`create_private_store` (``mkdtemp``,
mode 0700, under the CLIs' launch directory, beside the call's ``TMPDIR``,
never inside the shared store), which the cage binds at the store's path
instead of the store (:mod:`plugboard.agy_cage` fills it). Whatever agy
writes during the call, a refreshed or forged token included, stays there
and is removed with it once the call is over, however it ends (answered,
failed, timed out, cancelled), after the child is dead and reaped. Nothing
is ever copied back to the shared store: agy's writes never reach it, so
there is nothing left to purge there (the BUG-97 purge is gone with it), and
the conversations of other calls are never in sight.

The removal never follows a link (``rmtree``) and gives the owner back its
rights on a folder agy made read-only, so that the copy of the token does not
stay behind. What still cannot be removed is logged with the error type only,
never a path, an id nor a prompt; the answer to the client is not affected.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import tempfile

log = logging.getLogger(__name__)

STORE_PARTS = (".gemini", "antigravity-cli")
PRIVATE_PREFIX = "agy-store-"


def _remove(path: str) -> None:
    """Remove the folder ``path`` whole, links as links (never their
    targets), even where agy took its own rights off a folder; never
    raises."""
    root = path.rstrip(os.sep) + os.sep

    def retry(function, failed, exc):
        if not isinstance(exc, PermissionError) or not failed.startswith(root):
            raise exc
        # A folder of the call made read-only or unreadable by agy: its
        # owner (the gateway) gives itself its rights back, then removes it.
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(failed), stat.S_IRWXU)
        if os.path.isdir(failed) and not os.path.islink(failed):
            os.chmod(failed, stat.S_IRWXU)
            shutil.rmtree(failed, onexc=retry)
        else:
            function(failed)

    for _ in range(2):
        try:
            shutil.rmtree(path, onexc=retry)
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - tried once more, then logged
            pass
        if not os.path.lexists(path):
            return
    log.warning("agy private store left in place after the call")


def create_private_store(parent: str) -> str:
    """A new empty folder (mode 0700) under ``parent`` for one agy call's
    store. Raises :class:`OSError` when it cannot be made: the caller then
    launches nothing. The caller removes it with
    :func:`remove_private_store` once the call is over."""
    return tempfile.mkdtemp(prefix=PRIVATE_PREFIX, dir=parent)


def remove_private_store(path: str) -> None:
    """Remove the call's store ``path`` (see :func:`_remove`); never raises.
    It may take a while (a large ``bin/``): the gateway runs it off its
    event loop."""
    _remove(path)
