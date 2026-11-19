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

The removal follows no link (a link is removed as itself, never its target),
recurses not (agy may leave a tree deeper than Python's stack) and first
gives the gateway, which owns every folder of the store (agy runs with its
uid), read, write and search rights on each folder it opens, the root
included: a ``chmod 000`` by agy does not keep the copy of the token behind.
What still cannot be removed is logged with the error type only, never a
path, an id nor a prompt; the answer to the client is not affected.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile

log = logging.getLogger(__name__)

STORE_PARTS = (".gemini", "antigravity-cli")
PRIVATE_PREFIX = "agy-store-"


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


def _purge_tree(path: str) -> None:
    """Remove the folder ``path`` and everything in it, without recursion
    (agy may leave a tree deeper than Python's stack, or than ``PATH_MAX``:
    one descriptor at a time, by name in its parent) and without following
    any link (a link is unlinked as itself, never its target). Before each
    folder is read, its owner (the gateway, agy's uid) gives itself back
    read, write and search rights on it, the root included: agy may have
    taken them off (``chmod 000``). Raises on the first failure."""
    if stat.S_ISLNK(os.lstat(path).st_mode):
        os.unlink(path)
        return
    os.chmod(path, stat.S_IRWXU)
    fd = os.open(path, _DIR_FLAGS | os.O_NOFOLLOW)
    names = []
    try:
        while True:
            with os.scandir(fd) as entries:
                found = [(e.name, e.is_dir(follow_symlinks=False)) for e in entries]
            sub = None
            for name, is_dir in found:
                if is_dir:
                    sub = name
                    break
                os.unlink(name, dir_fd=fd)
            if sub is not None:
                # A folder (no link: checked just above, agy is gone).
                os.chmod(sub, stat.S_IRWXU, dir_fd=fd)
                child = os.open(sub, _DIR_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
                names.append(sub)
                continue
            if not names:
                break
            parent = os.open("..", _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = parent
            os.rmdir(names.pop(), dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(path)


def _remove(path: str) -> None:
    """Remove the call's store ``path`` whole (:func:`_purge_tree`), tried
    twice; what is left is logged with the error type only; never raises."""
    failure = None
    for _ in range(2):
        try:
            _purge_tree(path)
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 - tried once more, then logged
            failure = exc
        if not os.path.lexists(path):
            return
    log.warning(
        "agy private store left in place after the call (%s)",
        type(failure).__name__ if failure is not None else "unknown",
    )


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
