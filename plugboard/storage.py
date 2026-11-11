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
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_PROJECT_DIR = Path(__file__).resolve().parent.parent


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
