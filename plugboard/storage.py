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
"""

from __future__ import annotations

import json
import logging
import os
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


def atomic_write_text(path: Path, text: str, mode: Optional[int] = None) -> None:
    """Replace ``path`` with ``text`` in one step (temp file + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    atomic_write_text(
        path, json.dumps(data, indent=2, ensure_ascii=False), mode=mode
    )
