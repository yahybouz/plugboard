"""Shared helpers for the JSON files under the data directory.

* ``data_dir()`` is the single place that resolves the data directory:
  ``PLUGBOARD_DATA`` when set, otherwise ``<project>/data`` (not the current
  working directory, so the server finds its data wherever it is started).
* ``atomic_write_text()`` never leaves a half-written file: it writes a
  temporary file in the same directory, flushes it to disk and renames it
  over the target.
* ``atomic_write_json()`` also refuses to replace a file that exists but
  cannot be parsed. Readers fall back to an empty default for such a file;
  without this guard the next read-modify-write would silently wipe it.
* ``ensure_private_dir()`` creates directories owner-only (0700) and
  ``private_sqlite_file()`` keeps an SQLite store owner-only (0600): they hold
  cached answers, usage, audit rows, sessions and credentials.
* ``open_sqlite_store()`` opens such a store in WAL mode and sets it up once
  per process, under a lock (BUG-128).
* ``to_thread_to_the_end()`` runs blocking file work in a worker thread and,
  when the caller is cancelled meanwhile, still waits for it to end.
* ``purge_sqlite_rows()`` deletes old rows from an SQLite store and
  ``compact_sqlite()`` gives the freed pages back to the disk once enough of
  the file is free. Both are blocking and never create a missing store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import stat
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple, TypeVar

log = logging.getLogger(__name__)

_PROJECT_DIR = Path(__file__).resolve().parent.parent

_T = TypeVar("_T")


class StoreUnreadableError(RuntimeError):
    """An existing data file cannot be parsed; it is left untouched."""


def data_dir() -> Path:
    """Directory holding runtime data (providers, keys, pricing, logs...)."""
    configured = os.environ.get("PLUGBOARD_DATA", "").strip()
    return Path(configured).expanduser() if configured else _PROJECT_DIR / "data"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` and its missing parents owner-only (0700).

    ``Path.mkdir(parents=True)`` gives missing parents the default mode, not
    ``mode``: each missing level is created here. An existing directory keeps
    its mode.
    """
    path = Path(path)
    missing = []
    current = path
    while not current.exists() and current.parent != current:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass  # created meanwhile by another thread or process
    return path


_SQLITE_SIDE_FILES = ("", "-wal", "-shm", "-journal")
_sqlite_mode_warned: set = set()


def private_sqlite_file(path: Path) -> Path:
    """Make an SQLite store owner-only (0600) before ``sqlite3`` opens it.

    SQLite creates a new database 0644 and its ``-wal`` / ``-shm`` files with
    the database's mode: the file is created 0600 first, and an existing
    store (an install from before) is tightened, with its side files. Its
    directory is created 0700 when missing.
    """
    path = Path(path)
    ensure_private_dir(path.parent)
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        pass
    except OSError as exc:
        log.warning("Cannot create %s owner-only: %s", path, exc)
    for suffix in _SQLITE_SIDE_FILES:
        target = f"{path}{suffix}"
        try:
            if stat.S_IMODE(os.stat(target).st_mode) & 0o077:
                os.chmod(target, 0o600)
        except FileNotFoundError:
            pass
        except OSError as exc:
            # Logged once per file: the store is opened on every call.
            if target not in _sqlite_mode_warned:
                _sqlite_mode_warned.add(target)
                log.warning("Cannot restrict %s to 0600: %s", target, exc)
    return path


# Stores already set up in this process (by path), and one lock per path.
_sqlite_ready: set = set()
_sqlite_setup_locks: Dict[str, threading.Lock] = {}
_sqlite_setup_locks_guard = threading.Lock()


def _sqlite_setup_lock(key: str) -> threading.Lock:
    with _sqlite_setup_locks_guard:
        return _sqlite_setup_locks.setdefault(key, threading.Lock())


def forget_sqlite_store(path: Path) -> None:
    """Have the next ``open_sqlite_store(path)`` set the store up again."""
    _sqlite_ready.discard(str(Path(path)))


def open_sqlite_store(
    path: Path,
    set_up: Optional[Callable[[sqlite3.Connection], None]] = None,
    *,
    foreign_keys: bool = False,
) -> sqlite3.Connection:
    """Open an owner-only SQLite store in WAL mode, set up once per process.

    ``busy_timeout`` is set before anything else. The store's set-up (WAL
    mode, then ``set_up(conn)``: tables, indexes, migrations, committed here)
    runs once per path per process, under a lock: threads that switched a
    brand-new database to WAL at once got "database is locked" (SQLite does
    not call the busy handler on that lock upgrade) and lost their write
    (BUG-122, BUG-128). A store missing on disk (deleted while the gateway
    runs) is set up again. Rows are ``sqlite3.Row``.
    """
    path = Path(path)
    key = str(path)
    if not path.exists():
        forget_sqlite_store(path)  # deleted or not created yet: set it up again
    private_sqlite_file(path)
    conn = sqlite3.connect(key, timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        if key not in _sqlite_ready:
            with _sqlite_setup_lock(key):
                if key not in _sqlite_ready:
                    conn.execute("PRAGMA journal_mode=WAL")
                    if set_up is not None:
                        set_up(conn)
                    conn.commit()
                    _sqlite_ready.add(key)
    except BaseException:
        conn.close()
        raise
    return conn


def _open_existing_sqlite(
    path: Path, busy_timeout_ms: int, isolation_level: Optional[str] = ""
) -> Optional[sqlite3.Connection]:
    """Open an existing store read-write, or return None when it is missing.

    ``mode=rw`` makes SQLite fail rather than create the file, so a store
    removed meanwhile is never recreated empty.
    """
    path = Path(path)
    if not path.is_file():
        return None
    return sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=rw",
        uri=True,
        timeout=busy_timeout_ms / 1000,
        isolation_level=isolation_level,
    )


class PartialPurgeError(sqlite3.Error):
    """A purge failed after some of its batches were committed.

    ``deleted`` is the number of rows those batches removed for good; the
    error that stopped the purge is ``__cause__``.
    """

    def __init__(self, deleted: int, cause: sqlite3.Error) -> None:
        super().__init__(
            f"{type(cause).__name__}: {cause} (after {deleted} rows deleted)"
        )
        self.deleted = deleted


def _delete_batch(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    params: Tuple[Any, ...],
    limit: int,
) -> int:
    """Delete at most ``limit`` matching rows in one committed transaction.

    ``table`` and ``where`` are constants of the callers (retention.py,
    session_store.py), never request input; the values go as parameters.
    """
    if not table.isidentifier():
        raise ValueError(f"not a table name: {table!r}")
    cursor = conn.execute(
        f"DELETE FROM {table} WHERE rowid IN "  # nosec B608
        f"(SELECT rowid FROM {table} WHERE {where} LIMIT ?)",
        (*params, limit),
    )
    conn.commit()
    return cursor.rowcount


def purge_sqlite_rows(
    path: Path,
    table: str,
    where: str,
    params: Sequence[Any] = (),
    *,
    busy_timeout_ms: int = 5000,
    batch_size: int = 5000,
    pause_s: float = 0.05,
) -> int:
    """Delete the rows of ``table`` matching ``where``; return how many.

    The rows go in batches of at most ``batch_size``, each in its own
    transaction, with a pause of ``pause_s`` seconds between two batches: the
    write lock is held for one batch at a time, so the gateway's own writers
    (billing, watchdog, sessions) get through between two of them instead of
    waiting for the whole purge. The purge ends with the first batch that is
    not full. It blocks: run it in a worker thread, never on the event loop.

    ``table`` and ``where`` are formatted into the SQL: they must be constants
    of the code, never user input. Values go through ``params``. The table
    must have a ``rowid``. A missing file or table deletes nothing and
    returns 0, without creating either.

    Any other ``sqlite3.Error`` (locked or corrupt store) is raised. When it
    comes after one batch or more was committed, those rows are gone for
    good: the error is then a ``PartialPurgeError`` whose ``deleted`` gives
    their number and whose ``__cause__`` is the original error. Before any
    commit, the original error is raised as it is.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    conn = _open_existing_sqlite(path, busy_timeout_ms)
    if conn is None:
        return 0
    deleted = 0
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if found is None:
            return 0
        values = tuple(params)
        while True:
            batch = _delete_batch(conn, table, where, values, batch_size)
            deleted += batch
            if batch < batch_size:
                return deleted
            if pause_s > 0:
                time.sleep(pause_s)
    except sqlite3.Error as exc:
        if deleted == 0:
            raise
        raise PartialPurgeError(deleted, exc) from exc
    finally:
        conn.close()


def compact_sqlite(
    path: Path, min_free_ratio: float = 0.2, *, busy_timeout_ms: int = 5000
) -> bool:
    """``VACUUM`` the store when at least ``min_free_ratio`` of its pages are free.

    Returns True when the store was compacted, False when it is missing or
    has too little free space. ``VACUUM`` rewrites the file in place, so its
    mode is kept; a WAL store is then checkpointed and its log truncated.
    """
    conn = _open_existing_sqlite(path, busy_timeout_ms, isolation_level=None)
    if conn is None:
        return False
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        free_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if page_count == 0 or free_count / page_count < min_free_ratio:
            return False
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    finally:
        conn.close()


def atomic_write_text(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Replace ``path`` with ``text`` in one step (temp file + rename)."""
    path = Path(path)
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_replaceable_json(path: Path) -> None:
    """Raise ``StoreUnreadableError`` if ``path`` exists but is not valid JSON."""
    path = Path(path)
    if not path.exists():
        return
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error(
            "Refusing to overwrite %s: the current file is unreadable (%s). "
            "Fix or move it, then retry.",
            path,
            exc,
        )
        raise StoreUnreadableError(f"{path} is unreadable: {exc}") from exc


def atomic_write_json(path: Path, data: Any, mode: Optional[int] = None) -> None:
    """Write ``data`` as indented JSON atomically, never over an unreadable file."""
    ensure_replaceable_json(path)
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False), mode=mode)


async def to_thread_to_the_end(
    func: Callable[..., _T], *args: Any
) -> Tuple[_T, Optional[asyncio.CancelledError]]:
    """Run ``func(*args)`` in a worker thread, and wait for it even if cancelled.

    A thread cannot be stopped. Cancelled during ``asyncio.to_thread()`` (the
    pricing scheduler's timeout), a caller would stop waiting while ``func``
    goes on and writes its files, and skip what it does after them (journal,
    webhooks, meta). Here the caller keeps waiting until ``func`` ends.

    Returns ``(result, cancellation)``: ``cancellation`` is the
    ``CancelledError`` received meanwhile, or None. The caller finishes its
    follow-up, then raises it. When ``func`` fails after a cancellation, the
    cancellation is raised, so it is never lost.
    """
    work = asyncio.ensure_future(asyncio.to_thread(func, *args))
    cancelled: Optional[asyncio.CancelledError] = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as exc:
            cancelled = exc  # func is still running: keep waiting for it
        except Exception:  # noqa: BLE001 - func failed: raised below
            break
    if cancelled is not None and work.exception() is not None:
        raise cancelled from work.exception()
    return work.result(), cancelled
