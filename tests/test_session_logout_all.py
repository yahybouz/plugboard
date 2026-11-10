"""PB-SEC-04: ``POST /auth/admin/logout-all`` revokes every dashboard session.

The admin (open session or admin bearer key) signs out every browser at once,
this one included. A session store that cannot be rewritten is reported as a
503 and leaves no ``auth.logout_all`` audit row.

All values are synthetic; HTTP runs in-process via ASGITransport.
"""

from __future__ import annotations

import pytest

from plugboard import admin_sessions
from plugboard.audit import count_audit_log

ADMIN_BEARER = {"Authorization": "Bearer adminkey"}


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    """Sessions and MFA state in private files; auth rate limits cleared."""
    from plugboard import admin_mfa, ratelimit

    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.setattr(
        admin_sessions, "_path", lambda: tmp_path / "admin_sessions.json"
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
