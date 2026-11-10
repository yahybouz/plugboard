"""The data directory is created owner-only (0700), SQLite stores are 0600.

The stores hold cached answers, usage, audit rows, chat sessions and OAuth
credentials. SQLite creates a new database 0644 (and its -wal / -shm files
with the database's mode), and ``Path.mkdir(parents=True)`` gives missing
parents the default mode: other local accounts could read them.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from plugboard import storage

ROOT = Path(__file__).resolve().parents[1]


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def test_private_dir_creates_every_missing_level_0700(tmp_path, umask_022):
    target = tmp_path / "a" / "b" / "data"

    storage.ensure_private_dir(target)

    for level in (tmp_path / "a", tmp_path / "a" / "b", target):
        assert _mode(level) == 0o700, level


def test_private_dir_leaves_an_existing_directory_alone(tmp_path, umask_022):
    existing = tmp_path / "data"
    existing.mkdir(mode=0o755)

    storage.ensure_private_dir(existing)

    assert _mode(existing) == 0o755


def test_atomic_write_creates_its_directory_0700(tmp_path, umask_022):
    target = tmp_path / "data" / "pricing" / "2026_11.json"

    storage.atomic_write_json(target, {})

    assert _mode(tmp_path / "data") == 0o700
    assert _mode(tmp_path / "data" / "pricing") == 0o700


def test_private_sqlite_file_is_created_0600(tmp_path, umask_022):
    db = tmp_path / "data" / "x.db"

    storage.private_sqlite_file(db)

    assert _mode(db) == 0o600
    assert _mode(tmp_path / "data") == 0o700


def test_private_sqlite_file_tightens_an_existing_store(tmp_path, umask_022):
    db = tmp_path / "x.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db}{suffix}").write_bytes(b"")
        os.chmod(f"{db}{suffix}", 0o644)

    storage.private_sqlite_file(db)

    for suffix in ("", "-wal", "-shm"):
        assert _mode(Path(f"{db}{suffix}")) == 0o600, suffix


def _open(module, func: str):
    conn = getattr(module, func)()
    if isinstance(conn, contextlib.AbstractContextManager) and not hasattr(
        conn, "execute"
    ):
        with conn:
            return
    conn.close()


# (module, attribute holding the database path, connection function)
STORES = [
    ("plugboard.cache", "_DB_PATH", "_get_conn"),
    ("plugboard.usage", "_DB_PATH", "_get_conn"),
    ("plugboard.audit", "_DB_PATH", "_get_conn"),
    ("plugboard.monitoring", "_DB_PATH", "_get_conn"),
    ("plugboard.provider_watchdog", "_DB_PATH", "_get_conn"),
    ("plugboard.session_store", "_DB_PATH", "_get_conn"),
    ("plugboard.semantic_cache", "_DB_PATH", "_get_conn"),
    ("plugboard.oauth_store", "_DB_PATH", "_conn"),
    ("plugboard.mcp_server", "_TRACKING_DB", "_tracking_conn"),
]


@pytest.mark.parametrize(
    "module_name, path_attr, func", STORES, ids=[s[0] for s in STORES]
)
def test_sqlite_store_and_its_directory_are_owner_only(
    module_name, path_attr, func, tmp_path, monkeypatch, umask_022
):
    if module_name == "plugboard.mcp_server":
        pytest.importorskip("mcp")
    module = importlib.import_module(module_name)
    data = tmp_path / "data"
    db = data / Path(getattr(module, path_attr)).name
    monkeypatch.setattr(module, "_DATA_DIR", data, raising=False)
    monkeypatch.setattr(module, path_attr, db)
    if module_name == "plugboard.session_store":
        monkeypatch.setattr(module, "_INITIALISED", False, raising=False)

    _open(module, func)

    assert _mode(data) == 0o700
    assert _mode(db) == 0o600


def test_ticket_store_and_its_directory_are_owner_only(
    tmp_path, monkeypatch, umask_022
):
    from plugboard import suivi

    data = tmp_path / "data"
    monkeypatch.setenv("PLUGBOARD_DATA", str(data))

    suivi._connect().close()

    assert _mode(data) == 0o700
    assert _mode(data / "suivi.db") == 0o600


def test_server_creates_the_data_directory_0700(tmp_path):
    # ENCRYPTION_KEY set: the key file is not written, so the directory must
    # be created on its own, before any store.
    data = tmp_path / "fresh" / "data"
    env = {
        **os.environ,
        "PLUGBOARD_SKIP_DOTENV": "1",
        "PLUGBOARD_DATA": str(data),
        "ENCRYPTION_KEY": "test-only-encryption-key-not-a-secret",
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import os; os.umask(0o022); import plugboard.config"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _mode(data) == 0o700
    assert _mode(data.parent) == 0o700
