"""Tests for cron script orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path

import pytest


_PROJECT_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _hermetic_script_env(monkeypatch):
    """The scripts load .env and copy legacy names into os.environ at import:
    never read the developer's real .env, and undo every environment write."""
    from unittest import mock

    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    with mock.patch.dict(os.environ):
        yield


def _load_script(module_name: str, filename: str):
    path = _PROJECT_DIR / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _DailyDateTime:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 4, 10, 6, 0, tzinfo=timezone.utc)


class _MonthlyDateTime:
    @staticmethod
    def now(tz=None):
        return datetime(2026, 4, 1, 0, 5, tzinfo=timezone.utc)


@pytest.mark.parametrize("script", ["smoke_models.py"])
@pytest.mark.parametrize("source", ["process_env", "dotenv_file"])
def test_cron_scripts_honour_pre_rename_variable_names(monkeypatch, script, source):
    # Added by the /ship coverage audit.
    # Value: protects=cron scripts reach the URL set under the pre-rename name,
    # from the process env or from .env; fails_when=the legacy-name loop is
    # removed or runs before load_dotenv(); why_new=test_envcompat covers the
    # package helper only, the scripts carry their own copy; seam=none
    import dotenv

    legacy_url = "http://legacy-host.test:8799"
    # setenv then delenv records "unset" as the original value, so the
    # script's own os.environ writes are undone after the test.
    for name in ("PLUGBOARD_URL", "LLM_ENABLER_URL"):
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)

    if source == "process_env":
        monkeypatch.setenv("LLM_ENABLER_URL", legacy_url)
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    else:

        def load_dotenv_with_legacy_line(*_args, **_kwargs):
            # What load_dotenv() does with a `.env` line LLM_ENABLER_URL=...
            os.environ.setdefault("LLM_ENABLER_URL", legacy_url)
            return True

        monkeypatch.setattr(dotenv, "load_dotenv", load_dotenv_with_legacy_line)

    module = _load_script(f"legacy_env_{source}_{script[:-3]}", script)

    assert module.BASE_URL == legacy_url
