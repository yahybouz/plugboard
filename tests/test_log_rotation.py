"""Tests for SEC-4 — rotating file handler on app logs.

The rotating sink is opt-in (PLUGBOARD_LOG_FILE=...) to keep tests
and ephemeral contexts clean; when configured, it caps disk usage via
maxBytes + backupCount.
"""

from __future__ import annotations

import logging
from pathlib import Path  # noqa: F401 — used by assertions in-tree

import pytest


@pytest.fixture
def reset_root_logger():
    """Snapshot + restore root handlers so tests don't leak sinks.

    We intentionally DO NOT reload plugboard.logging_config in these
    tests: reloading creates a new _request_id_var ContextVar, which
    silently breaks every other test that imported it before. Instead we
    patch the module-level env-cached vars in place.
    """
    saved = list(logging.getLogger().handlers)
    try:
        yield
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)


def _apply_env(monkeypatch, **overrides):
    """Mirror env vars onto the already-imported logging_config module."""
    from plugboard import logging_config

    for key, value in overrides.items():
        monkeypatch.setattr(logging_config, key, value)
    return logging_config


def test_file_handler_is_opt_in(reset_root_logger, monkeypatch):
    """Without PLUGBOARD_LOG_FILE, no file handler is attached."""
    cfg = _apply_env(monkeypatch, _LOG_FILE="")
    cfg.setup_logging()
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert file_handlers == []


def test_rotating_file_handler_configured_from_env(
    tmp_path, reset_root_logger, monkeypatch
):
    log_path = tmp_path / "subdir" / "enabler.log"
    cfg = _apply_env(
        monkeypatch,
        _LOG_FILE=str(log_path),
        _LOG_MAX_BYTES=4096,
        _LOG_BACKUP_COUNT=3,
    )
    cfg.setup_logging()

    root = logging.getLogger()
    rfhs = [
        h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rfhs) == 1
    handler = rfhs[0]
    assert handler.maxBytes == 4096
    assert handler.backupCount == 3
    # The parent directory should have been created on our behalf.
    assert log_path.parent.exists()

    # Smoke: emit something and confirm it lands on disk (with redaction).
    log = logging.getLogger("plugboard.test")
    log.info("boot marker ghp_supersecret1234567890ABCDEFGH1234")
    for h in root.handlers:
        h.flush()
    contents = log_path.read_text(encoding="utf-8")
    assert "boot marker" in contents
    # Redaction must apply to file sink too (SEC-1 coverage check).
    assert "ghp_***REDACTED***" in contents
    assert "supersecret1234" not in contents


def test_invalid_log_path_does_not_crash(reset_root_logger, monkeypatch, capsys):
    """A bad path must not kill setup_logging — console sink still works."""
    bad = "/root/cannot-write/here.log"  # unlikely to be writable
    cfg = _apply_env(monkeypatch, _LOG_FILE=bad)
    cfg.setup_logging()
    root = logging.getLogger()
    # Console sink is always present.
    stream_handlers = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(stream_handlers) >= 1
