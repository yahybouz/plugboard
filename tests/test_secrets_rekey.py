"""ENCRYPTION_KEY rotation: every stored secret moves to the new key (PB-SEC-01)."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from plugboard import secrets_rekey
from plugboard.config import fernet_for_key

ROOT = Path(__file__).resolve().parent.parent
OLD = Fernet.generate_key().decode()
NEW = Fernet.generate_key().decode()


def _enc(key: str, value: str) -> str:
    return "enc:" + fernet_for_key(key).encrypt(value.encode()).decode()


def _dec(key: str, value: str) -> str:
    assert value.startswith("enc:")
    return fernet_for_key(key).decrypt(value[4:].encode()).decode()


def _seed(data: Path, key: str = OLD) -> None:
    """A data dir with every kind of encrypted secret (synthetic values)."""
    data.mkdir(parents=True, exist_ok=True)
    (data / "providers.json").write_text(
        json.dumps(
            {
                "providers": [
                    {"id": "alpha", "bearer_token": _enc(key, "tok-alpha")},
                    {"id": "legacy", "bearer_token": "tok-plain"},
                    {"id": "keyless", "bearer_token": ""},
                ]
            }
        )
    )
    (data / "admin_mfa.json").write_text(
        json.dumps({"secret_enc": _enc(key, "JBSWY3DPEHPK3PXP"), "last_step": 7})
    )
    conn = sqlite3.connect(str(data / "credentials.db"))
    with conn:
        conn.execute(
            "CREATE TABLE oauth_credentials (source TEXT PRIMARY KEY, account TEXT, "
            "access_token TEXT, refresh_token TEXT, expires_at REAL, scope TEXT, "
            "extra TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO oauth_credentials VALUES (?, 'a', ?, ?, NULL, '', '{}', 'x', 'x')",
            ("github", _enc(key, "tok-access"), _enc(key, "tok-refresh")),
        )
    conn.close()


def _stored(data: Path) -> dict:
    providers = json.loads((data / "providers.json").read_text())["providers"]
    mfa = json.loads((data / "admin_mfa.json").read_text())
    conn = sqlite3.connect(str(data / "credentials.db"))
    access, refresh = conn.execute(
        "SELECT access_token, refresh_token FROM oauth_credentials"
    ).fetchone()
    conn.close()
    return {
        "alpha": providers[0]["bearer_token"],
        "legacy": providers[1]["bearer_token"],
        "keyless": providers[2]["bearer_token"],
        "mfa": mfa["secret_enc"],
        "mfa_last_step": mfa["last_step"],
        "access": access,
        "refresh": refresh,
    }


def test_rotation_moves_every_secret_to_the_new_key(tmp_path):
    _seed(tmp_path)

    report = secrets_rekey.rotate(tmp_path, OLD, NEW)

    assert report.ok and report.written
    assert (report.rotated, report.encrypted_plain, report.already_new) == (4, 1, 0)
    s = _stored(tmp_path)
    assert _dec(NEW, s["alpha"]) == "tok-alpha"
    assert _dec(NEW, s["legacy"]) == "tok-plain"  # plain text is now encrypted
    assert s["keyless"] == ""
    assert _dec(NEW, s["mfa"]) == "JBSWY3DPEHPK3PXP"
    assert s["mfa_last_step"] == 7  # other fields untouched
    assert _dec(NEW, s["access"]) == "tok-access"
    assert _dec(NEW, s["refresh"]) == "tok-refresh"
    with pytest.raises(Exception):
        _dec(OLD, s["alpha"])
    for name in ("providers.json", "admin_mfa.json"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600

    # The backup holds the files as they were, owner-only.
    backup = report.backup_dir
    assert backup is not None and backup.parent == tmp_path / "backups"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    old_providers = json.loads((backup / "providers.json").read_text())["providers"]
    assert _dec(OLD, old_providers[0]["bearer_token"]) == "tok-alpha"
    conn = sqlite3.connect(str(backup / "credentials.db"))
    (access,) = conn.execute("SELECT access_token FROM oauth_credentials").fetchone()
    conn.close()
    assert _dec(OLD, access) == "tok-access"


def test_unreadable_secret_aborts_without_writing(tmp_path):
    _seed(tmp_path)
    stranger = Fernet.generate_key().decode()
    conn = sqlite3.connect(str(tmp_path / "credentials.db"))
    with conn:
        conn.execute(
            "UPDATE oauth_credentials SET refresh_token = ?",
            (_enc(stranger, "tok-other"),),
        )
    conn.close()
    before = _stored(tmp_path)

    report = secrets_rekey.rotate(tmp_path, OLD, NEW)

    assert not report.ok and not report.written
    assert report.unreadable == ["credentials.db: github refresh_token"]
    assert _stored(tmp_path) == before
    assert not (tmp_path / "backups").exists()


def test_rotation_can_be_run_again(tmp_path):
    _seed(tmp_path)
    secrets_rekey.rotate(tmp_path, OLD, NEW)
    once = _stored(tmp_path)

    report = secrets_rekey.rotate(tmp_path, OLD, NEW)

    assert report.ok
    assert (report.rotated, report.encrypted_plain, report.already_new) == (0, 0, 5)
    assert _stored(tmp_path) == once


def test_dry_run_writes_nothing(tmp_path):
    _seed(tmp_path)
    before = _stored(tmp_path)

    report = secrets_rekey.rotate(tmp_path, OLD, NEW, dry_run=True)

    assert report.ok and not report.written and report.rotated == 4
    assert _stored(tmp_path) == before
    assert not (tmp_path / "backups").exists()


def test_empty_data_dir_is_fine(tmp_path):
    report = secrets_rekey.rotate(tmp_path, OLD, NEW)
    assert report.ok and report.rotated == 0


def test_only_real_fernet_keys_are_accepted_as_new_key():
    assert secrets_rekey.is_strong_key(NEW)
    assert not secrets_rekey.is_strong_key("# copied from .env.example")
    assert not secrets_rekey.is_strong_key("short")


def test_mfa_secret_unreadable_with_current_key_refuses_codes(monkeypatch, tmp_path):
    from plugboard import admin_mfa

    mfa_file = tmp_path / "admin_mfa.json"
    mfa_file.write_text(json.dumps({"secret_enc": _enc(OLD, "JBSWY3DPEHPK3PXP")}))
    monkeypatch.setattr(admin_mfa, "_MFA_FILE", mfa_file)

    # The server key is not OLD: before the fix this raised (HTTP 500).
    assert admin_mfa.verify("123456") is False


def _run_cli(tmp_path: Path, *args: str, env_key: str | None = None):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"ENCRYPTION_KEY", "PLUGBOARD_DATA"}
    }
    env.update(PLUGBOARD_DATA=str(tmp_path / "data"), PLUGBOARD_SKIP_DOTENV="1")
    if env_key is not None:
        env["ENCRYPTION_KEY"] = env_key
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "rotate_encryption_key.py"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_with_key_file_install_replaces_the_key_file(tmp_path):
    data = tmp_path / "data"
    _seed(data)
    (data / ".encryption_key").write_text(OLD)
    new_file = tmp_path / "new.key"

    run = _run_cli(tmp_path, "--new-key-file", str(new_file), "--generate")

    assert run.returncode == 0, run.stderr
    new_key = new_file.read_text().strip()
    assert stat.S_IMODE(new_file.stat().st_mode) == 0o600
    assert new_key not in run.stdout + run.stderr  # never printed
    assert (data / ".encryption_key").read_text().strip() == new_key
    assert _dec(new_key, _stored(data)["alpha"]) == "tok-alpha"
    backups = list((data / "backups").iterdir())
    assert (backups[0] / ".encryption_key").read_text() == OLD


def test_cli_with_env_key_leaves_the_key_to_the_operator(tmp_path):
    data = tmp_path / "data"
    _seed(data)
    new_file = tmp_path / "new.key"
    new_file.write_text(NEW + "\n")

    run = _run_cli(tmp_path, "--new-key-file", str(new_file), env_key=OLD)

    assert run.returncode == 0, run.stderr
    assert "set ENCRYPTION_KEY" in run.stdout
    assert not (data / ".encryption_key").exists()
    assert _dec(NEW, _stored(data)["access"]) == "tok-access"


def test_cli_refuses_a_weak_key_and_reports_unreadable_secrets(tmp_path):
    data = tmp_path / "data"
    _seed(data, key=Fernet.generate_key().decode())  # not the server key
    weak = tmp_path / "weak.key"
    weak.write_text("# not a key\n")

    assert _run_cli(tmp_path, "--new-key-file", str(weak), env_key=OLD).returncode == 2

    good = tmp_path / "good.key"
    good.write_text(NEW)
    run = _run_cli(tmp_path, "--new-key-file", str(good), env_key=OLD)
    assert run.returncode == 1
    assert "providers.json: provider alpha" in run.stderr
    assert "Nothing was written" in run.stderr


# A completed rotation signs out every dashboard session (PB-SEC-04).
_SESSION = {"issued_at": 1800000000, "mfa": False, "mfa_generation": None}


def _seed_session(data: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    (data / "admin_sessions.json").write_text(
        json.dumps({"sessions": {"0" * 64: dict(_SESSION)}})
    )


def _sessions(data: Path) -> dict:
    return json.loads((data / "admin_sessions.json").read_text())["sessions"]


def test_cli_rotation_signs_out_every_session(tmp_path):
    data = tmp_path / "data"
    _seed(data)
    _seed_session(data)
    (data / ".encryption_key").write_text(OLD)

    run = _run_cli(tmp_path, "--new-key-file", str(tmp_path / "new.key"), "--generate")

    assert run.returncode == 0, run.stderr
    assert _sessions(data) == {}
    assert "Every dashboard session was signed out" in run.stdout


def test_cli_dry_run_keeps_sessions(tmp_path):
    data = tmp_path / "data"
    _seed(data)
    _seed_session(data)
    (data / ".encryption_key").write_text(OLD)
    new_file = tmp_path / "new.key"

    run = _run_cli(tmp_path, "--new-key-file", str(new_file), "--generate", "--dry-run")

    assert run.returncode == 0, run.stderr
    assert _sessions(data) == {"0" * 64: _SESSION}
    assert "signed out" not in run.stdout


def test_cli_refused_rotation_keeps_sessions(tmp_path):
    data = tmp_path / "data"
    _seed(data, key=Fernet.generate_key().decode())  # not the server key
    _seed_session(data)
    good = tmp_path / "good.key"
    good.write_text(NEW)

    run = _run_cli(tmp_path, "--new-key-file", str(good), env_key=OLD)

    assert run.returncode == 1
    assert _sessions(data) == {"0" * 64: _SESSION}
    assert "signed out" not in run.stdout


def test_rotation_warns_when_sessions_cannot_be_revoked(tmp_path, monkeypatch, capsys):
    import importlib.util

    from plugboard import admin_sessions, config

    data = tmp_path / "data"
    _seed(data)
    _seed_session(data)
    monkeypatch.setenv("PLUGBOARD_DATA", str(data))
    monkeypatch.setenv("PLUGBOARD_SKIP_DOTENV", "1")
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(config, "_ENCRYPTION_KEY", OLD)
    monkeypatch.setattr(sys, "path", list(sys.path))  # the script prepends ROOT

    def refuse() -> None:
        raise PermissionError("read-only store")

    monkeypatch.setattr(admin_sessions, "revoke_all", refuse)
    spec = importlib.util.spec_from_file_location(
        "rotate_encryption_key_under_test",
        ROOT / "scripts" / "rotate_encryption_key.py",
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    code = script.main(["--new-key-file", str(tmp_path / "new.key"), "--generate"])

    assert code == 0
    out = capsys.readouterr()
    assert "Log out everywhere" in out.err
    assert "read-only store" in out.err
    assert "Every dashboard session was signed out" not in out.out
    # The rotation itself went through: secrets and key file are on the new key.
    new_key = (tmp_path / "new.key").read_text().strip()
    assert (data / ".encryption_key").read_text().strip() == new_key
    assert _dec(new_key, _stored(data)["alpha"]) == "tok-alpha"
