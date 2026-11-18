"""Session edge cases left by the personal login review (PB-AUTH-01).

The ADMIN_KEY MFA login entries name the admin key; a logout names the
author of its session even once that person is revoked; an unreadable people
store refuses a personal key without counting against the address; a
malformed session record is ignored, never a 500.
"""

import json

import pytest

from plugboard import admin_mfa, admin_people, admin_sessions, audit, ratelimit
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


def _sessions_file(env):
    return env.dir / "admin_sessions.json"


async def test_admin_key_mfa_login_entries_name_the_admin_key(
    admin_client, env, monkeypatch
):
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    good = admin_mfa._generate_code(secret)
    bad = {"key": "adminkey", "totp_code": ph.wrong(good)}
    codes = [
        (await admin_client.post("/auth/admin/login", json=bad)).status_code
        for _ in range(5)
    ]
    assert codes == [401, 401, 401, 401, 429]
    blocked = await admin_client.post(
        "/auth/admin/login", json={"key": "adminkey", "totp_code": good}
    )
    assert blocked.status_code == 429
    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()
    await ph.login(admin_client, "adminkey", good)
    for action, count in (
        ("auth.mfa_failed", 5),
        ("auth.login_blocked", 1),
        ("auth.mfa_success", 1),
        ("auth.login_success", 1),
    ):
        rows = audit.get_audit_log(action=action)
        assert len(rows) == count, action
        assert {(e["principal"], e["principal_name"]) for e in rows} == {
            ("admin-key", "")
        }, action


@pytest.mark.parametrize("store", ["revoked", "unreadable"])
async def test_logout_of_a_revoked_person_still_names_the_person(
    admin_client, env, monkeypatch, store
):
    made = ph.new_person()
    pid = made["person"]["id"]
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))

    def store_not_writable(person_id):
        raise OSError("read-only")

    # The session stays in the store: only the person's status refuses it.
    monkeypatch.setattr(admin_sessions, "revoke_person", store_not_writable)
    admin_people.revoke(pid, revoked_by="admin-key")
    if store == "unreadable":
        env.store.write_text("{not json")
    assert await ph.keys_status(admin_client, cookie) == 401
    r = await admin_client.post("/auth/admin/logout", headers=cookie)
    assert r.status_code == 204
    rows = audit.get_audit_log(action="auth.logout")
    assert len(rows) == 1
    name = "person-one" if store == "revoked" else ""
    assert (rows[0]["principal"], rows[0]["principal_name"]) == (f"person:{pid}", name)
    assert json.loads(_sessions_file(env).read_text())["sessions"] == {}


async def test_logout_of_a_stale_admin_key_session_names_the_admin_key(
    admin_client, env, monkeypatch
):
    cookie = await ph.login(admin_client, "adminkey")
    # An MFA change the session store did not record refuses the session.
    monkeypatch.setattr(admin_mfa, "generation", lambda: "another-generation")
    assert await ph.keys_status(admin_client, cookie) == 401
    r = await admin_client.post("/auth/admin/logout", headers=cookie)
    assert r.status_code == 204
    rows = audit.get_audit_log(action="auth.logout")
    assert [(e["principal"], e["principal_name"]) for e in rows] == [("admin-key", "")]


async def test_an_unreadable_people_store_counts_nothing_against_the_address(
    admin_client, env
):
    made = ph.new_person()
    code = ph.code_now(env, made)
    env.store.write_text("{not json")
    for _ in range(6):
        r = await admin_client.post(
            "/auth/admin/login", json={"key": made["key"], "totp_code": code}
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "Admin key required"
        assert "set-cookie" not in r.headers
    assert not ratelimit._auth_buckets and not ratelimit._lockouts
    assert audit.get_audit_log(action="auth.login_failed") == []
    assert await ph.keys_status(admin_client, ph.ADMIN) == 200


@pytest.mark.parametrize("junk", ["junk", 7, None, ["a"]], ids=repr)
async def test_a_malformed_session_record_is_refused_never_a_500(
    admin_client, env, junk
):
    cookie = await ph.login(admin_client, "adminkey")
    data = json.loads(_sessions_file(env).read_text())
    (digest,) = data["sessions"]
    data["sessions"][digest] = junk
    data["sessions"]["0" * 64] = junk
    _sessions_file(env).write_text(json.dumps(data))
    assert await ph.keys_status(admin_client, cookie) == 401
    admin_client.cookies.clear()
    assert (await admin_client.get("/health/ready", headers=cookie)).status_code in (
        200,
        503,
    )
    # A new session is still registered next to the malformed records.
    fresh = await ph.login(admin_client, "adminkey")
    assert await ph.keys_status(admin_client, fresh) == 200
    r = await admin_client.post("/auth/admin/logout", headers=cookie)
    assert r.status_code == 204
