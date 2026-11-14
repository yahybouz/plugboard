"""What agy keeps of each conversation, removed once the call is over (BUG-97).

agy (Antigravity CLI 1.4.4) records every conversation, prompt in clear, in
its store ``$HOME/.gemini/antigravity-cli``, outside any retention: about 8
files and 450 KB per call (real store of the UAT, 14/11). Per conversation
``<id>``:

- ``conversations/<id>.db`` (and its ``-wal`` / ``-shm`` neighbours);
- ``brain/<id>/`` (transcripts);
- ``annotations/<id>.pbtxt``;
- ``presence/<id>.lock``.

After each agy call launched by ``cli_subprocess.complete()`` (answered,
failed, timed out or cancelled, streamed or not), the gateway removes the
entries of that call: in those four folders only, the entry named ``<id>`` or
``<id>.<something>``.

Where ``<id>`` comes from: agy's own output (``conversation_id`` of its JSON
payload; in ``stream-json`` already in the first event, ``init``). It is
checked against a strict pattern before any path is built. A call cut before
agy said it (timeout, cancellation, crash) falls back on the ids that appeared
in the four folders during its run: removed at once when no other agy call of
this process ran at any time in that window, otherwise set aside until no agy
call is in flight, so a call never removes the files of a neighbour still
running. A call that knows its id takes it out of that set and removes its own.

``conversation_summaries.db`` (``-wal``, ``-shm``) and
``jetbox_summaries_proto.pb`` summarise the conversations of every caller and
cannot be split per call: they are removed whole, only when no agy call of
this process is in flight. The manual purge of 14/11 removed them and agy
answered 200 afterwards, on the UAT and in production.

Never touched: the store itself, the four folders themselves, the sign-in
(``antigravity-oauth-token``), ``installation_id``, ``jetski_state.pbtxt``,
``bin/``, ``builtin/``, ``cache/``, ``implicit/``, ``log/``, ``cli.log`` and
``crashes/``. ``implicit/``, ``log/`` and ``crashes/`` do not carry the call's
id, so their entries cannot be attributed to a call; two known prompts looked
for in the whole store were found only in ``conversations/`` and ``brain/``
(which is no proof for every content); and the logs serve diagnosis.

Paths: the store is the one of the ``HOME`` given to the agy process; when it
does not exist nothing is done. A removed path always sits in one of the four
folders, directly inside the store; a folder that is a link is skipped; a link
is removed as a link, never its target (``rmtree`` does not follow links).

A purge that fails (permission, lock, anything) logs a warning with the error
type and the number of entries left, never a path, the id nor the prompt; the
answer to the client is not affected and no exception leaves this module.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Any, Callable, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

STORE_PARTS = (".gemini", "antigravity-cli")
CONVERSATION_DIRS = ("conversations", "brain", "annotations", "presence")
SHARED_FILES = (
    "conversation_summaries.db",
    "conversation_summaries.db-wal",
    "conversation_summaries.db-shm",
    "jetbox_summaries_proto.pb",
)
# Documented, and checked again before any removal.
NEVER_TOUCHED = frozenset(
    {
        "antigravity-oauth-token",
        "installation_id",
        "jetski_state.pbtxt",
        "bin",
        "builtin",
        "cache",
        "implicit",
        "log",
        "cli.log",
        "crashes",
        *CONVERSATION_DIRS,
    }
)

# agy's ids are UUIDs; any [0-9A-Za-z_-] name up to 128 characters is
# accepted, which can hold neither a separator, nor "..", nor a leading dot.
_ID_RE = re.compile(r"[0-9A-Za-z_-]{1,128}")


def valid_conversation_id(value: Any) -> Optional[str]:
    """``value`` when it is a safe conversation id, else None."""
    if isinstance(value, str) and _ID_RE.fullmatch(value):
        return value
    return None


def _id_of_event(evt: Any) -> Optional[str]:
    """The id of a ``json`` payload or of a ``stream-json`` event (``init``
    carries it at the top, ``step_update`` and ``result`` inside)."""
    if not isinstance(evt, dict):
        return None
    for holder in (evt, evt.get("result"), evt.get("step_update")):
        if isinstance(holder, dict):
            cid = valid_conversation_id(holder.get("conversation_id"))
            if cid:
                return cid
    return None


def conversation_id_from_line(line: bytes) -> Optional[str]:
    """The conversation id of one line of agy's output, else None."""
    if b"conversation_id" not in line:
        return None
    try:
        return _id_of_event(json.loads(line))
    except ValueError:
        return None


def conversation_id_from_output(stdout_text: str) -> Optional[str]:
    """The conversation id of agy's whole output (``json`` or ``stream-json``)."""
    try:
        cid = _id_of_event(json.loads(stdout_text))
    except ValueError:
        cid = None
    if cid:
        return cid
    for line in stdout_text.splitlines():
        cid = conversation_id_from_line(line.encode("utf-8", "replace"))
        if cid:
            return cid
    return None


def store_dir(home: Optional[str]) -> Optional[Path]:
    """agy's store under ``home``, or None without a home."""
    if not home:
        return None
    return Path(home).joinpath(*STORE_PARTS)


def _real_store(store: Optional[Path]) -> Optional[str]:
    if store is None:
        return None
    real = os.path.realpath(store)
    return real if os.path.isdir(real) else None


def _folders(real_store: str) -> List[str]:
    """The four conversation folders that really are folders of the store."""
    out = []
    for sub in CONVERSATION_DIRS:
        folder = os.path.join(real_store, sub)
        if os.path.islink(folder) or not os.path.isdir(folder):
            continue  # a link may lead out of the store: never followed
        if os.path.dirname(os.path.realpath(folder)) != real_store:
            continue
        out.append(folder)
    return out


def _ids_in(real_store: Optional[str]) -> Set[str]:
    """Ids that have an entry in one of the four folders."""
    ids: Set[str] = set()
    if real_store is None:
        return ids
    for folder in _folders(real_store):
        for name in os.listdir(folder):
            cid = valid_conversation_id(name.split(".", 1)[0])
            if cid:
                ids.add(cid)
    return ids


def _remove(path: str) -> None:
    """Remove one entry: a folder with what it holds, anything else (a link
    included) as itself."""
    if stat.S_ISDIR(os.lstat(path).st_mode):
        shutil.rmtree(path)  # unlinks the links inside, never follows them
    else:
        os.unlink(path)


def _removable(real_store: str, path: str) -> bool:
    folder, name = os.path.split(path)
    return (
        os.path.dirname(folder) == real_store
        and os.path.basename(folder) in CONVERSATION_DIRS
        and name not in ("", ".", "..")
        and name not in NEVER_TOUCHED
        and not name.startswith(".")
    )


def _attempt(paths: List[str], failures: List[BaseException]) -> None:
    for path in paths:
        try:
            _remove(path)
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001 - logged by the caller
            failures.append(exc)


def _entries(real_store: str, cid: str) -> List[str]:
    out = []
    for folder in _folders(real_store):
        for name in os.listdir(folder):
            if name == cid or name.startswith(cid + "."):
                path = os.path.join(folder, name)
                if _removable(real_store, path):
                    out.append(path)
    return out


def _purge_conversation(
    real_store: str, cid: str, failures: List[BaseException]
) -> None:
    if valid_conversation_id(cid) is None:
        return
    _attempt(_entries(real_store, cid), failures)


def _purge_shared(real_store: str, failures: List[BaseException]) -> None:
    paths = []
    for name in SHARED_FILES:
        path = os.path.join(real_store, name)
        if os.path.lexists(path) and not os.path.isdir(path):
            paths.append(path)
    _attempt(paths, failures)


def _report(failures: List[BaseException]) -> None:
    if failures:
        log.warning(
            "agy store purge: %d entr%s left in place (%s)",
            len(failures),
            "y" if len(failures) == 1 else "ies",
            ", ".join(sorted({type(exc).__name__ for exc in failures})),
        )


# Bookkeeping of this process's agy calls. Every change happens without an
# await in between, so the event loop sees each one whole.
_active: Set["AgyCall"] = set()
# Ids seen appearing during the run of a call cut without its id while
# another call was in flight: (real store, id), removed once none is.
_orphans: Set[Tuple[str, str]] = set()


class AgyCall:
    """One agy run: what the store held before it, and its id once known."""

    def __init__(self, home: Optional[str]):
        self.alone = True
        self.conversation_id: Optional[str] = None
        self._store = store_dir(home)
        self._before: Optional[Set[str]] = None
        try:
            self._before = _ids_in(_real_store(self._store))
        except Exception as exc:  # noqa: BLE001 - the call goes on
            log.warning("agy store snapshot failed (%s)", type(exc).__name__)

    def see_line(self, line: bytes) -> None:
        if self.conversation_id is None:
            self.conversation_id = conversation_id_from_line(line)

    def see_output(self, stdout: Optional[bytes]) -> None:
        if self.conversation_id is None and stdout:
            self.conversation_id = conversation_id_from_output(
                stdout.decode("utf-8", "replace")
            )

    def watching(self, read_delta: Callable[[bytes], str]) -> Callable[[bytes], str]:
        """``read_delta`` that also notes the id from agy's first event."""

        def read(line: bytes) -> str:
            self.see_line(line)
            return read_delta(line)

        return read

    def end(self) -> None:
        """Remove what this call left; never raises."""
        _active.discard(self)
        failures: List[BaseException] = []
        try:
            self._purge(failures)
        except Exception as exc:  # noqa: BLE001 - never fails the answer
            failures.append(exc)
        _report(failures)

    def _purge(self, failures: List[BaseException]) -> None:
        real_store = _real_store(self._store)
        if real_store is not None:
            if self.conversation_id is not None:
                _orphans.discard((real_store, self.conversation_id))
                _purge_conversation(real_store, self.conversation_id, failures)
            elif self._before is not None:
                appeared = _ids_in(real_store) - self._before
                if self.alone:
                    for cid in appeared:
                        _purge_conversation(real_store, cid, failures)
                else:
                    _orphans.update((real_store, cid) for cid in appeared)
        if _active:
            return
        stores = {store for store, _ in _orphans}
        while _orphans:
            store, cid = _orphans.pop()
            _purge_conversation(store, cid, failures)
        if real_store is not None:
            stores.add(real_store)
        for store in stores:
            _purge_shared(store, failures)


def begin(env: dict) -> AgyCall:
    """Register an agy call about to be launched with ``env``."""
    call = AgyCall(env.get("HOME"))
    if _active:
        call.alone = False
        for other in _active:
            other.alone = False
    _active.add(call)
    return call
