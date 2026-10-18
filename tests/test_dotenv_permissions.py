"""SEC-2026-10 — boot warning when `.env` (ADMIN_KEY, ENCRYPTION_KEY) is
readable by other local users."""

from __future__ import annotations

import os

import pytest

from plugboard import config
from plugboard.config import dotenv_permission_warning


@pytest.fixture
def dotenv_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("ADMIN_KEY=x\nENCRYPTION_KEY=y\n")
    return path


def test_owner_only_env_file_raises_no_warning(dotenv_file):
    dotenv_file.chmod(0o600)
    assert dotenv_permission_warning(str(dotenv_file)) is None


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_group_or_world_access_warns_with_the_fix(dotenv_file, mode):
    dotenv_file.chmod(mode)
    warning = dotenv_permission_warning(str(dotenv_file))
    assert warning is not None
    assert f"{mode:04o}" in warning
    assert f"chmod 600 {dotenv_file}" in warning


def test_missing_file_raises_no_warning(tmp_path):
    assert dotenv_permission_warning(str(tmp_path / "absent.env")) is None


def test_defaults_to_the_loaded_dotenv(dotenv_file, monkeypatch):
    dotenv_file.chmod(0o644)
    monkeypatch.setattr(config, "_DOTENV_PATH", str(dotenv_file))
    assert "chmod 600" in dotenv_permission_warning()
    monkeypatch.setattr(config, "_DOTENV_PATH", "")
    assert dotenv_permission_warning() is None


def test_windows_is_skipped(dotenv_file, monkeypatch):
    dotenv_file.chmod(0o644)
    monkeypatch.setattr(os, "name", "nt")
    assert dotenv_permission_warning(str(dotenv_file)) is None
