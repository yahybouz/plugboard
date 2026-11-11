"""PB-SEC-04: ``POST /auth/admin/logout-all`` revokes every dashboard session.

The admin (open session or admin bearer key) signs out every browser at once,
this one included. A session store that cannot be rewritten is reported as a
503 and leaves no ``auth.logout_all`` audit row. On a local install, browsers
remembered for auto-login are forgotten too: otherwise their next visit to
``/login`` would open a new session at once.

All values are synthetic; HTTP runs in-process via ASGITransport.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import admin_sessions, local_login
from plugboard import app as app_module
from plugboard.audit import count_audit_log

ADMIN_BEARER = {"Authorization": "Bearer adminkey"}
# What a browser sends when the user types the URL or reloads the page.
_NAV = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """Sessions and MFA state in private files; auth rate limits cleared."""
    from plugboard import admin_mfa, ratelimit

    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.setattr(
        admin_sessions, "_path", lambda: tmp_path / "admin_sessions.json"
    )
    monkeypatch.setattr(
        local_login, "_devices_path", lambda: tmp_path / "local_devices.json"
    )
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()
    yield tmp_path
    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()


async def _login(client):
    client.cookies.clear()
    resp = await client.post("/auth/admin/login", json={"key": "adminkey"})
    assert resp.status_code == 204, f"login {resp.status_code}"
    raw = resp.headers.get("set-cookie", "")
    client.cookies.clear()
    value = raw.split("plugboard_admin_session=", 1)[1].split(";", 1)[0]
    return {"Cookie": f"plugboard_admin_session={value}"}


async def _keys(client, headers):
    client.cookies.clear()
    return (await client.get("/admin/keys", headers=headers)).status_code


async def test_logout_all_revokes_every_session(admin_client, sessions):
    first, second = await _login(admin_client), await _login(admin_client)
    assert await _keys(admin_client, first) == 200
    assert await _keys(admin_client, second) == 200
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=first)
    assert resp.status_code == 204
    assert await _keys(admin_client, first) == 401
    assert await _keys(admin_client, second) == 401


async def test_logout_all_clears_this_browser_cookie(admin_client, sessions):
    first = await _login(admin_client)
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=first)
    assert resp.status_code == 204
    cookies = resp.headers.get_list("set-cookie")
    assert any(
        c.startswith("plugboard_admin_session=") and "Max-Age=0" in c for c in cookies
    )


async def test_logout_all_with_admin_key(admin_client, sessions):
    first = await _login(admin_client)
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=ADMIN_BEARER)
    assert resp.status_code == 204
    assert await _keys(admin_client, first) == 401


async def test_logout_all_needs_admin(admin_client, sessions):
    first = await _login(admin_client)
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all")
    assert resp.status_code == 401
    resp = await admin_client.post(
        "/auth/admin/logout-all", headers={"Authorization": "Bearer testkey"}
    )
    assert resp.status_code in (401, 403)
    assert await _keys(admin_client, first) == 200


async def test_logout_all_is_audited(admin_client, sessions):
    before = count_audit_log("auth.logout_all")
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=ADMIN_BEARER)
    assert resp.status_code == 204
    assert count_audit_log("auth.logout_all") == before + 1


async def test_logout_all_unwritable_store_says_so(admin_client, sessions, monkeypatch):
    first = await _login(admin_client)
    before = count_audit_log("auth.logout_all")

    def refuse(*args, **kwargs):
        raise PermissionError("read-only store")

    monkeypatch.setattr(admin_sessions, "_save", refuse)
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=first)
    assert resp.status_code == 503
    assert "not writable" in resp.json()["detail"]
    assert count_audit_log("auth.logout_all") == before


async def _local_visit(device: str):
    """A remembered browser on the operator's machine opens /login."""
    transport = ASGITransport(app=app_module.app, client=("127.0.0.1", 50123))
    async with AsyncClient(transport=transport, base_url="http://localhost:8765") as c:
        cookie = {"Cookie": f"{local_login.DEVICE_COOKIE}={device}"}
        return await c.get("/login", headers={**_NAV, **cookie})


async def test_logout_all_forgets_remembered_browsers(
    admin_client, sessions, monkeypatch
):
    # Local install with auto-login: without this, the browser just signed
    # out reloads the page and gets a new session through its device cookie.
    monkeypatch.setenv("PLUGBOARD_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("PLUGBOARD_LOCAL_AUTO_LOGIN", raising=False)
    device = local_login.remember_device()
    granted = await _local_visit(device)
    assert granted.status_code == 302, "auto-login should work before"
    assert app_module._ADMIN_COOKIE_NAME in granted.cookies

    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=ADMIN_BEARER)
    assert resp.status_code == 204

    assert not local_login.is_remembered(device)
    again = await _local_visit(device)
    assert again.status_code == 200
    assert app_module._ADMIN_COOKIE_NAME not in again.cookies
    assert "enter your admin key" in again.text.lower()


async def test_logout_all_unwritable_devices_says_so(
    admin_client, sessions, monkeypatch
):
    first = await _login(admin_client)
    device = local_login.remember_device()
    before = count_audit_log("auth.logout_all")

    def refuse() -> None:
        raise PermissionError("read-only store")

    monkeypatch.setattr(local_login, "forget_devices", refuse)
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/logout-all", headers=first)
    assert resp.status_code == 503
    assert "not writable" in resp.json()["detail"]
    assert count_audit_log("auth.logout_all") == before
    # Nothing claimed, nothing half done: the sessions are still open.
    assert await _keys(admin_client, first) == 200
    assert local_login.is_remembered(device)
