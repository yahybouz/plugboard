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
* ``to_thread_to_the_end()`` runs blocking file work in a worker thread and,
  when the caller is cancelled meanwhile, still waits for it to end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional, Tuple, TypeVar

log = logging.getLogger(__name__)

_PROJECT_DIR = Path(__file__).resolve().parent.parent

_T = TypeVar("_T")


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
