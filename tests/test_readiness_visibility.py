"""GET /health/ready shows its checks to key holders only (PLU-75).

The route stays public: a load balancer or a deployment gate reads its status
code (200 ready, 503 not) without a key. Without a valid key the body is only
``{"ready", "version"}``; which check failed (the auth posture, the usage
database, the CLI binaries, Ollama, the background jobs) is shown with a valid
key: an active client key, the admin key (no TOTP needed: a read of generic
states) or an admin session. A wrong, forged or revoked key is not refused
(no 401, no refusal logged): it gets the minimal body. With both API_KEY and
ADMIN_KEY empty (a keyless install, loopback only) every route is open, so
the checks are shown.
"""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import cli_subprocess, provider_watchdog, readiness, usage

ADMIN = "adminkey"
MINIMAL = {"ready", "version"}
FULL = {"ready", "version", "checks"}

CLI_PROVIDER = {
    "id": "cli-missing-75",
    "name": "CLI missing",
    "base_url": "subprocess://gemini",
    "bearer_token": "",
    "auth_source": "subprocess:gemini",
    "default_model": "gemini-2.5-pro",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture
def gw(monkeypatch, _providers_file, _admin_key, tmp_path):
    """Admin key ``adminkey``, API_KEY set, an isolated key store, no limit."""
    import plugboard.apikeys as ak
    import plugboard.ratelimit as ratelimit

    keys_file = tmp_path / "api_keys.json"
    keys_file.write_text('{"keys": []}')
    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", keys_file)
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    ak._invalidate_cache()
    monkeypatch.setattr(ratelimit, "_LIMIT", 0)
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    monkeypatch.setattr(
        provider_watchdog,
        "get_watchdog_status",
        lambda: {"running": True, "check_interval_s": 30, "providers": {}},
    )
    readiness.reset_cache()
    yield ak
    readiness.reset_cache()
    ak._invalidate_cache()


def _client(base_url: str = "http://test") -> AsyncClient:
    from plugboard.app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url=base_url)


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def _ready(headers=None, base_url: str = "http://test"):
    async with _client(base_url) as client:
        return await client.get("/health/ready", headers=headers or {})


# -- without a valid key: status and version only ----------------------------


async def test_no_key_gets_status_and_version_only(gw):
    resp = await _ready()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == MINIMAL
    assert body["ready"] is True
    assert "checks" not in resp.text


async def test_no_key_still_gets_503_when_not_ready(gw, _providers_file, monkeypatch):
    import json

    _providers_file.write_text(json.dumps({"providers": [CLI_PROVIDER]}))
    monkeypatch.setattr(
        cli_subprocess,
        "check_cli_installed",
        lambda kind: {"ok": False, "error": "not found"},
    )
    resp = await _ready()
    assert resp.status_code == 503, resp.text
    assert resp.json() == {"ready": False, "version": resp.json()["version"]}


async def test_auth_posture_is_hidden_without_a_key(gw, monkeypatch):
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._DEV_MODE", False)
    resp = await _ready()
    assert resp.status_code == 503, resp.text
    assert set(resp.json()) == MINIMAL
    assert "auth" not in resp.text


@pytest.mark.parametrize(
    "bearer",
    [
        "pb_" + "0" * 64,  # the shape of a client key, unknown
        "lle_forged",
        "adminkey-but-not-quite",
        "testkey",  # the API_KEY value is not a client key
        "not-a-key",
    ],
)
async def test_a_wrong_key_is_not_refused_and_gets_the_minimal_body(gw, bearer, caplog):
    caplog.set_level(logging.DEBUG)
    resp = await _ready(_bearer(bearer))
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == MINIMAL
    assert "auth rejected" not in caplog.text
    assert "client key refused" not in caplog.text


async def test_a_revoked_key_gets_the_minimal_body(gw):
    key = gw.create_key(client_id="alpha", label="plu-75")
    assert gw.revoke_key(key["key_id"])
    resp = await _ready(_bearer(key["key"]))
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == MINIMAL


async def test_a_forged_session_cookie_gets_the_minimal_body(gw):
    cookie = {"Cookie": "plugboard_admin_session=1800000000.nonce.deadbeef"}
    resp = await _ready(cookie)
    assert set(resp.json()) == MINIMAL


# -- with a valid key: the checks ---------------------------------------------


async def test_an_active_client_key_sees_the_checks(gw):
    key = gw.create_key(client_id="alpha", label="plu-75")
    resp = await _ready(_bearer(key["key"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == FULL
    assert set(body["checks"]) == {
        "usage_db",
        "cli",
        "auth",
        "ollama",
        "background",
    }


async def test_the_admin_key_sees_the_checks_without_totp(gw, monkeypatch):
    monkeypatch.setattr("plugboard.admin_mfa.is_required", lambda: True)
    resp = await _ready(_bearer(ADMIN))
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == FULL


async def test_an_admin_session_sees_the_checks(gw):
    async with _client() as client:
        login = await client.post("/auth/admin/login", json={"key": ADMIN})
        assert login.status_code == 204, login.text
        resp = await client.get("/health/ready")
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == FULL


async def test_a_key_holder_sees_why_it_is_not_ready(gw, monkeypatch):
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._DEV_MODE", False)
    resp = await _ready(_bearer(ADMIN))
    assert resp.status_code == 503, resp.text
    assert resp.json()["checks"]["auth"] == "fail"


# -- dev modes ----------------------------------------------------------------


async def test_keyless_install_shows_the_checks(gw, monkeypatch):
    """Both keys empty: every route is open (loopback only), so are the checks."""
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")
    monkeypatch.setattr("plugboard.app._DEV_MODE", True)
    resp = await _ready(base_url="http://127.0.0.1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"]["auth"] == "dev"


async def test_client_auth_off_but_admin_key_set_keeps_the_minimal_body(
    gw, monkeypatch
):
    """API_KEY empty, ADMIN_KEY set: the checks still need a key."""
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._DEV_MODE", True)
    resp = await _ready()
    assert resp.status_code == 200, resp.text
    assert set(resp.json()) == MINIMAL
    resp = await _ready(_bearer(ADMIN))
    assert resp.json()["checks"]["auth"] == "dev"


# -- the contract -------------------------------------------------------------


def test_contract_marks_checks_optional_and_is_a_major_version():
    from plugboard import api_contract

    doc = api_contract.build_openapi()
    schema = doc["components"]["schemas"]["Ready"]
    assert set(schema["required"]) == {"ready", "version"}
    assert "valid key" in schema["properties"]["checks"]["description"]
    assert int(api_contract.CONTRACT_VERSION.split(".")[0]) >= 3
    assert doc["paths"]["/health/ready"]["get"]["security"] == []
