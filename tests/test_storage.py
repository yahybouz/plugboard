"""Shared data-directory and atomic-write helpers."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from plugboard import storage


def test_data_dir_defaults_to_the_project_not_the_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("PLUGBOARD_DATA", raising=False)
    monkeypatch.chdir(tmp_path)

    assert storage.data_dir() == Path(storage.__file__).resolve().parent.parent / "data"


def test_data_dir_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path / "custom"))

    assert storage.data_dir() == tmp_path / "custom"


def test_atomic_write_replaces_content_and_sets_mode(tmp_path):
    target = tmp_path / "sub" / "keys.json"
    storage.atomic_write_json(target, {"a": 1}, mode=0o600)
    storage.atomic_write_json(target, {"a": 2}, mode=0o600)

    assert json.loads(target.read_text()) == {"a": 2}
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["keys.json"]


def test_failed_write_keeps_the_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "providers.json"
    target.write_text('{"providers": [1]}')

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage.os, "replace", boom)
    with pytest.raises(OSError):
        storage.atomic_write_json(target, {"providers": []})

    assert json.loads(target.read_text()) == {"providers": [1]}
    assert [p.name for p in tmp_path.iterdir()] == ["providers.json"]


def test_unreadable_store_is_never_overwritten(tmp_path):
    # Readers return an empty default for this file; a write must not turn
    # that default into data loss.
    target = tmp_path / "providers.json"
    target.write_text("{not json")

    with pytest.raises(storage.StoreUnreadableError):
        storage.atomic_write_json(target, {"providers": []})

    assert target.read_text() == "{not json"


def test_provider_write_over_corrupt_file_fails_closed(tmp_path, monkeypatch):
    import plugboard.config as cfg

    pfile = tmp_path / "providers.json"
    pfile.write_text("{{NOT VALID JSON")
    monkeypatch.setattr(cfg, "PROVIDERS_FILE", pfile)

    assert cfg._read() == {"providers": []}
    with pytest.raises(storage.StoreUnreadableError):
        cfg._write(cfg._read())
    assert pfile.read_text() == "{{NOT VALID JSON"


def test_suite_does_not_use_the_repository_data_dir():
    repo_data = Path(storage.__file__).resolve().parent.parent / "data"

    assert storage.data_dir() != repo_data


def test_server_refuses_to_start_without_admin_key_outside_dev_mode():
    from plugboard.app import _require_admin_key_outside_dev

    with pytest.raises(RuntimeError, match="ADMIN_KEY is not set"):
        _require_admin_key_outside_dev("", dev_mode=False)
    _require_admin_key_outside_dev("", dev_mode=True)
    _require_admin_key_outside_dev("k" * 48, dev_mode=False)
    # A short, guessable admin key is refused outside dev mode.
    with pytest.raises(RuntimeError, match="shorter than 16"):
        _require_admin_key_outside_dev("admin", dev_mode=False)
    _require_admin_key_outside_dev("admin", dev_mode=True)


def test_bootstrap_over_corrupt_providers_file_keeps_it_and_serves_from_memory(
    tmp_path, monkeypatch
):
    import plugboard.config as cfg

    pfile = tmp_path / "providers.json"
    pfile.write_text("{{NOT VALID JSON")
    monkeypatch.setattr(cfg, "PROVIDERS_FILE", pfile)
    monkeypatch.setattr(
        cfg, "PROVIDERS_LOCK", tmp_path / "providers.json.lock", raising=False
    )
    monkeypatch.setenv("OPENCLAW_URL", "http://openclaw.test")
    monkeypatch.setenv("OPENCLAW_KEY", "oc-test-key")

    providers = cfg.load_providers()

    assert [p["base_url"] for p in providers] == ["http://openclaw.test"]
    assert pfile.read_text() == "{{NOT VALID JSON"
