"""ENCRYPTION_KEY rotation re-encrypts the personal TOTP secrets (PB-AUTH-01)."""

import json
import logging
import os
import stat

import pytest
from cryptography.fernet import Fernet

from plugboard import admin_mfa, secrets_rekey
from tests.test_secrets_rekey import (
    NEW,
    OLD,
    _dec,
    _enc,
    _run_cli,
    _seed_session,
    _sessions,
)
from tests.test_secrets_rekey import _seed as _seed_all

PID = "ap_0123456789ab"


def _seed(data, value):
    data.mkdir(parents=True, exist_ok=True)
    (data / "admin_people.json").write_text(
        json.dumps(
            {
                "people": [
                    {
                        "id": PID,
                        "name": "person-one",
                        "key_hash": "0" * 64,
                        "totp_secret_enc": value,
                    }
                ]
            }
        )
    )


def test_rotation_re_encrypts_personal_secrets(tmp_path):
    data = tmp_path / "data"
    _seed(data, _enc(OLD, "JBSWY3DPEHPK3PXP"))
    report = secrets_rekey.rotate(data, OLD, NEW)
    assert report.ok and report.written and report.rotated == 1
    stored = json.loads((data / "admin_people.json").read_text())["people"][0]
    assert _dec(NEW, stored["totp_secret_enc"]) == "JBSWY3DPEHPK3PXP"
    assert stored["key_hash"] == "0" * 64
    assert (report.backup_dir / "admin_people.json").exists()
    assert stat.S_IMODE(os.stat(data / "admin_people.json").st_mode) == 0o600


def test_an_unreadable_personal_secret_stops_the_rotation(tmp_path):
    data = tmp_path / "data"
    third = Fernet.generate_key().decode()
    _seed(data, _enc(third, "JBSWY3DPEHPK3PXP"))
    before = (data / "admin_people.json").read_text()
    report = secrets_rekey.rotate(data, OLD, NEW)
    assert not report.ok and report.unreadable == [f"admin_people.json: person {PID}"]
    assert (data / "admin_people.json").read_text() == before


# -- Same guarantees as for the ADMIN_KEY second factor ------------------------


def _snapshot(data):
    return {
        p.name: p.read_bytes()
        for p in sorted(data.iterdir())
        if p.is_file() and not p.name.endswith(".lock")
    }


@pytest.mark.parametrize("readable", [True, False])
def test_the_report_never_quotes_the_secret(tmp_path, caplog, readable):
    # A unique witness, on a completed rotation and on a refused one: neither
    # the report, nor the log, nor the script's output ever shows it.
    witness = admin_mfa.generate_secret()
    data = tmp_path / "data"
    key = OLD if readable else Fernet.generate_key().decode()
    _seed(data, _enc(key, witness))
    with caplog.at_level(logging.DEBUG):
        report = secrets_rekey.rotate(data, OLD, NEW)
    assert report.ok is readable and report.written is readable
    assert witness not in repr(report) and witness not in caplog.text

    cli = tmp_path / "cli"
    _seed(cli / "data", _enc(key, witness))
    (cli / "data" / ".encryption_key").write_text(OLD)
    run = _run_cli(cli, "--new-key-file", str(cli / "new.key"), "--generate")
    assert run.returncode == (0 if readable else 1), run.stderr
    assert witness not in run.stdout and witness not in run.stderr
    if not readable:
        assert f"admin_people.json: person {PID}" in run.stderr


def test_a_dry_run_writes_nothing(tmp_path):
    data = tmp_path / "data"
    _seed_all(data)
    _seed(data, _enc(OLD, "JBSWY3DPEHPK3PXP"))
    before = _snapshot(data)
    report = secrets_rekey.rotate(data, OLD, NEW, dry_run=True)
    assert report.ok and not report.written and report.rotated >= 1
    assert _snapshot(data) == before
    assert not (data / "backups").exists()


def test_a_refused_rotation_writes_no_other_file(tmp_path):
    # The other secrets are readable: one personal secret is enough to refuse.
    data = tmp_path / "data"
    _seed_all(data)
    _seed(data, _enc(Fernet.generate_key().decode(), "JBSWY3DPEHPK3PXP"))
    before = _snapshot(data)
    report = secrets_rekey.rotate(data, OLD, NEW)
    assert not report.ok and not report.written
    assert report.unreadable == [f"admin_people.json: person {PID}"]
    assert _snapshot(data) == before
    assert not (data / "backups").exists()


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"people": "x"}', '{"people": 3}', '{"people": [1]}'],
)
def test_a_damaged_people_file_refuses_the_rotation(tmp_path, content):
    # The store refuses every personal key on such a file: the rotation must
    # not report success while personal secrets may stay on the old key.
    data = tmp_path / "data"
    _seed_all(data)
    (data / "admin_people.json").write_text(content)
    before = _snapshot(data)
    report = secrets_rekey.rotate(data, OLD, NEW)
    assert not report.ok and not report.written
    assert report.damaged and report.damaged[0].startswith("admin_people.json (")
    assert _snapshot(data) == before


def test_people_without_a_secret_are_kept_as_they_are(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    people = [
        {"id": PID, "name": "person-one", "totp_secret_enc": _enc(OLD, "JBSWY3DP")},
        {"id": "ap_ba9876543210", "name": "person-two", "totp_secret_enc": None},
        {"id": "ap_00000000000f", "name": "person-three"},
    ]
    (data / "admin_people.json").write_text(json.dumps({"people": people}))
    report = secrets_rekey.rotate(data, OLD, NEW)
    assert report.ok and report.rotated == 1
    stored = json.loads((data / "admin_people.json").read_text())["people"]
    assert _dec(NEW, stored[0]["totp_secret_enc"]) == "JBSWY3DP"
    assert stored[1:] == people[1:]


def test_the_rotation_can_be_run_again(tmp_path):
    data = tmp_path / "data"
    _seed(data, _enc(OLD, "JBSWY3DPEHPK3PXP"))
    secrets_rekey.rotate(data, OLD, NEW)
    again = secrets_rekey.rotate(data, OLD, NEW)
    assert again.ok and again.already_new == 1 and not again.written


def test_the_boot_check_names_an_unreadable_personal_secret(tmp_path):
    data = tmp_path / "data"
    _seed(data, _enc(Fernet.generate_key().decode(), "JBSWY3DPEHPK3PXP"))
    report = secrets_rekey.unreadable_secrets(data, OLD)
    assert report.unreadable == [f"admin_people.json: person {PID}"]


def test_no_people_file_is_fine(tmp_path):
    data = tmp_path / "data"
    _seed_all(data)
    report = secrets_rekey.rotate(data, OLD, NEW)
    assert report.ok and report.written
    assert not (data / "admin_people.json").exists()
    assert not (report.backup_dir / "admin_people.json").exists()


# -- The script: sessions signed out after a completed rotation only ----------


def test_cli_rotation_of_personal_secrets_signs_out_every_session(tmp_path):
    data = tmp_path / "data"
    _seed(data, _enc(OLD, "JBSWY3DPEHPK3PXP"))
    _seed_session(data)
    (data / ".encryption_key").write_text(OLD)
    run = _run_cli(tmp_path, "--new-key-file", str(tmp_path / "new.key"), "--generate")
    assert run.returncode == 0, run.stderr
    new_key = (tmp_path / "new.key").read_text().strip()
    stored = json.loads((data / "admin_people.json").read_text())["people"][0]
    assert _dec(new_key, stored["totp_secret_enc"]) == "JBSWY3DPEHPK3PXP"
    assert _sessions(data) == {}
    assert "JBSWY3DPEHPK3PXP" not in run.stdout + run.stderr


def test_cli_dry_run_and_refusal_keep_people_and_sessions(tmp_path):
    data = tmp_path / "data"
    _seed(data, _enc(OLD, "JBSWY3DPEHPK3PXP"))
    _seed_session(data)
    (data / ".encryption_key").write_text(OLD)
    before = (data / "admin_people.json").read_text()
    sessions = _sessions(data)
    run = _run_cli(
        tmp_path, "--new-key-file", str(tmp_path / "a.key"), "--generate", "--dry-run"
    )
    assert run.returncode == 0, run.stderr
    assert (data / "admin_people.json").read_text() == before
    assert _sessions(data) == sessions

    _seed(data, _enc(Fernet.generate_key().decode(), "JBSWY3DPEHPK3PXP"))
    before = (data / "admin_people.json").read_text()
    run = _run_cli(tmp_path, "--new-key-file", str(tmp_path / "b.key"), "--generate")
    assert run.returncode == 1
    assert f"admin_people.json: person {PID}" in run.stderr
    assert (data / "admin_people.json").read_text() == before
    assert _sessions(data) == sessions
