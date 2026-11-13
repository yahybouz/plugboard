"""A stored secret the ENCRYPTION_KEY cannot decrypt is never sent (BUG-57).

The provider whose key cannot be decrypted is turned off (in memory: the
file is not touched), audited and alerted once; the boot says which stored
secrets the key cannot read.
"""

from __future__ import annotations

import json
import logging

import pytest
from cryptography.fernet import Fernet

from tests.conftest import _setup_providers
from tests.factories import PROVIDER_A, PROVIDER_B

AUTH = {"Authorization": "Bearer testkey"}


def _sealed(plain: str, key: bytes) -> str:
    return "enc:" + Fernet(key).encrypt(plain.encode()).decode()


@pytest.fixture
def other_key_store(tmp_path, monkeypatch):
    """PROVIDER_A's key sealed with another ENCRYPTION_KEY than the running one."""
    from plugboard import config

    monkeypatch.setattr(config, "_ENCRYPTION_KEY", Fernet.generate_key().decode())
    sealed = _sealed("sk-provider-a", Fernet.generate_key())
    base = tmp_path / "sealed"  # beside the client fixture's own data/
    base.mkdir()
    pfile = _setup_providers(
        base, monkeypatch, [{**PROVIDER_A, "bearer_token": sealed}, PROVIDER_B]
    )
    monkeypatch.setattr(config, "_unreadable_secret_reported", set())
    monkeypatch.setattr(config, "_unreadable_secret_alerted", set())
    return pfile, sealed


@pytest.fixture
def audit_db(monkeypatch, tmp_path):
    from plugboard import audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")
    return audit_mod


@pytest.fixture
def alerts_sent(monkeypatch):
    from plugboard import alerts

    sent = []

    async def _alert(provider_id):
        sent.append(provider_id)

    monkeypatch.setattr(alerts, "send_secret_unreadable_alert", _alert, raising=False)
    return sent


def test_decrypt_token_never_returns_the_ciphertext(monkeypatch):
    from plugboard import config

    monkeypatch.setattr(config, "_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(config.SecretUnreadable):
        config._decrypt_token(_sealed("sk-x", Fernet.generate_key()))


def test_provider_whose_key_cannot_be_decrypted_is_turned_off(
    other_key_store, audit_db, alerts_sent
):
    from plugboard import config

    pfile, sealed = other_key_store
    before = pfile.read_text()
    cut, other = config.load_providers()
    assert (cut["id"], cut["is_active"], cut["bearer_token"]) == (
        "test-provider",
        False,
        "",
    )
    assert cut["secret_unreadable"] is True
    assert "secret_unreadable" not in other and other["is_active"] is True
    # In memory only: the stored key and switch are left for the right key.
    assert pfile.read_text() == before
    assert "enc:" not in json.dumps(config.load_providers())


async def test_chat_to_that_provider_calls_nobody(
    client, other_key_store, audit_db, alerts_sent
):
    from aioresponses import aioresponses

    with aioresponses() as mock:
        r = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        sent = [call for calls in mock.requests.values() for call in calls]
    assert sent == []  # no request, so no "enc:" header either
    # The answer to a provider that is turned off (PLU-12).
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "provider_disabled"
    # Review 4.13.0 MIN-2: not "turned off by the administrator", but why.
    body = r.json()
    assert "administrator" not in body["detail"]
    assert "ENCRYPTION_KEY" in body["detail"] and "ENCRYPTION_KEY" in body["hint"]


async def test_unreadable_key_is_logged_audited_and_alerted_once(
    other_key_store, audit_db, alerts_sent, caplog
):
    import asyncio

    from plugboard import config

    _, sealed = other_key_store
    with caplog.at_level(logging.CRITICAL, logger="plugboard.config"):
        for _ in range(3):
            config.load_providers()
        await asyncio.gather(*list(config._report_tasks))
    [line] = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert "test-provider" in line and "ENCRYPTION_KEY" in line
    assert sealed[4:] not in caplog.text
    [event] = audit_db.get_audit_log(action="provider.secret_unreadable")
    assert (event["actor"], event["target_id"]) == ("system", "test-provider")
    assert alerts_sent == ["test-provider"]


def test_boot_check_names_every_secret_the_key_cannot_read(
    other_key_store, audit_db, alerts_sent, caplog
):
    from plugboard import app as app_mod

    _, sealed = other_key_store
    with caplog.at_level(logging.CRITICAL):
        app_mod._check_stored_secrets()
    boot = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.CRITICAL and r.name == "plugboard.app"
    ]
    assert len(boot) == 1
    assert "providers.json: provider test-provider" in boot[0]
    assert "other-provider" not in boot[0]  # its key is plain text: readable
    assert "restore" in boot[0].lower()
    assert sealed[4:] not in caplog.text
    # The providers concerned are reported (audit) right away.
    assert audit_db.get_audit_log(action="provider.secret_unreadable")


def test_boot_check_is_quiet_when_every_secret_is_readable(
    tmp_path, monkeypatch, caplog
):
    from plugboard import app as app_mod
    from plugboard import config

    key = Fernet.generate_key()
    monkeypatch.setattr(config, "_ENCRYPTION_KEY", key.decode())
    _setup_providers(
        tmp_path,
        monkeypatch,
        [{**PROVIDER_A, "bearer_token": _sealed("sk-provider-a", key)}],
    )
    with caplog.at_level(logging.WARNING):
        app_mod._check_stored_secrets()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_oauth_credential_that_cannot_be_decrypted_is_not_returned(
    tmp_path, monkeypatch
):
    from plugboard import config, oauth_store
    from plugboard.oauth_store import OAuthCredential

    monkeypatch.setattr(oauth_store, "_DB_PATH", tmp_path / "credentials.db")
    oauth_store.save_credential(
        OAuthCredential(
            source="github",
            account="a",
            access_token="gho-x",
            refresh_token="",
            expires_at=None,
        )
    )
    monkeypatch.setattr(config, "_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert oauth_store.get_credential("github") is None


async def test_editing_a_cut_provider_keeps_its_switch_for_the_right_key(
    client, other_key_store, audit_db, alerts_sent, monkeypatch
):
    # The dashboard sends the stored switch back (stored_is_active), not the
    # effective one: an edit must not leave the provider off for good.
    from plugboard import config

    pfile, sealed = other_key_store
    [listed] = [
        p
        for p in (await client.get("/providers", headers=AUTH)).json()
        if p["id"] == "test-provider"
    ]
    assert (listed["is_active"], listed["stored_is_active"]) == (False, True)
    assert listed["secret_unreadable"] is True
    r = await client.put(
        "/providers/test-provider",
        headers=AUTH,
        json={"timeout": 30, "is_active": listed["stored_is_active"]},
    )
    assert r.status_code == 200, r.text
    stored = json.loads(pfile.read_text())["providers"][0]
    assert (stored["is_active"], stored["bearer_token"]) == (True, sealed)
    assert "secret_unreadable" not in stored and "stored_is_active" not in stored
    # Entering the key again brings the provider back.
    r = await client.put(
        "/providers/test-provider",
        headers=AUTH,
        json={"bearer_token": "sk-provider-a-new", "is_active": True},
    )
    assert r.status_code == 200, r.text
    back = config.get_provider("test-provider")
    assert (back["is_active"], back["bearer_token"]) == (True, "sk-provider-a-new")
    assert "secret_unreadable" not in back


def test_restoring_the_key_brings_the_provider_back(monkeypatch, tmp_path):
    from plugboard import config

    key = Fernet.generate_key()
    _setup_providers(
        tmp_path,
        monkeypatch,
        [{**PROVIDER_A, "bearer_token": _sealed("sk-provider-a", key)}],
    )
    monkeypatch.setattr(config, "_unreadable_secret_reported", set())
    monkeypatch.setattr(config, "_unreadable_secret_alerted", set())
    monkeypatch.setattr(config, "_audit_unreadable_secret", lambda pid: None)
    monkeypatch.setattr(config, "_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert config.get_provider("test-provider")["is_active"] is False
    monkeypatch.setattr(config, "_ENCRYPTION_KEY", key.decode())
    back = config.get_provider("test-provider")
    assert (back["is_active"], back["bearer_token"]) == (True, "sk-provider-a")


async def test_found_outside_an_event_loop_the_alert_waits_for_one(
    other_key_store, audit_db, alerts_sent
):
    import asyncio

    from plugboard import config

    await asyncio.to_thread(config.load_providers)  # a thread: no event loop
    assert alerts_sent == []
    assert len(audit_db.get_audit_log(action="provider.secret_unreadable")) == 1
    config.load_providers()
    await asyncio.gather(*list(config._report_tasks))
    assert alerts_sent == ["test-provider"]
    assert len(audit_db.get_audit_log(action="provider.secret_unreadable")) == 1


def test_boot_check_tells_a_damaged_file_from_a_wrong_key(
    tmp_path, monkeypatch, caplog
):
    from plugboard import app as app_mod

    pfile = _setup_providers(tmp_path, monkeypatch, [])
    pfile.write_text("{not json")
    with caplog.at_level(logging.CRITICAL):
        app_mod._check_stored_secrets()
    [line] = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.CRITICAL and r.name == "plugboard.app"
    ]
    assert "providers.json (not readable JSON)" in line
    assert "ENCRYPTION_KEY" not in line and "backup" in line


def test_dashboard_edit_form_sends_the_stored_switch_back():
    import re
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert re.search(
        r'getElementById\("pf-active"\)\.checked = p\.secret_unreadable === true '
        r"\? p\.stored_is_active !== false : p\.is_active;",
        js,
    )
    assert '"key unreadable"' in js


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/providers/test-provider/health"),  # Ping
        ("POST", "/providers/test-provider/discover?dry_run=true"),  # Discover
    ],
)
async def test_ping_and_discover_of_a_cut_provider_call_nobody(
    client, other_key_store, audit_db, alerts_sent, method, path
):
    # BUG-79: they called the provider with an empty token, and the admin read
    # "connection refused" instead of why the provider is off.
    from aioresponses import aioresponses

    with aioresponses() as mock:
        r = await client.request(method, path, headers=AUTH)
        sent = [call for calls in mock.requests.values() for call in calls]
    assert sent == []
    assert r.status_code == 409, r.text
    assert "ENCRYPTION_KEY" in r.json()["detail"]
