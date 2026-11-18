"""A person never manages, nor locks, the second factor of ADMIN_KEY (PB-AUTH-01)."""

import time

import pytest

from plugboard import admin_mfa, audit, ratelimit
from tests import people_helpers as ph

ROUTES = [
    ("POST", "/admin/mfa/begin", None),
    (
        "POST",
        "/admin/mfa/setup",
        {"secret": "JBSWY3DPEHPK3PXP", "code": "123456", "current_code": "000000"},
    ),
    ("POST", "/admin/mfa/verify", {"code": "000000"}),
    ("DELETE", "/admin/mfa", None),
]
_REFUSED = "The second factor of ADMIN_KEY is managed with ADMIN_KEY"


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


@pytest.mark.parametrize("enrolled", [True, False])
@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_a_person_cannot_touch_the_admin_key_factor(
    admin_client, env, enrolled, method, path, body
):
    if enrolled:
        admin_mfa.enroll(admin_mfa.generate_secret())
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_client.cookies.clear()
    r = await admin_client.request(
        method, path, json=body, headers={**cookie, ph.TOTP: "000000"}
    )
    assert r.status_code == 403
    assert r.json()["detail"] == _REFUSED
    assert admin_mfa.is_enrolled() is enrolled
    assert "key:admin" not in ratelimit._auth_buckets


async def test_a_person_may_read_the_mfa_status(admin_client, env):
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_client.cookies.clear()
    assert (
        await admin_client.get("/admin/mfa/status", headers=cookie)
    ).status_code == 200


# -- The parked point of the Task 6 re-review: no person code on key:admin -----


@pytest.mark.parametrize("via", ["session", "key"])
async def test_wrong_codes_from_a_person_never_lock_admin_key(admin_client, env, via):
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    for _ in range(ratelimit._AUTH_LIMIT + 2):
        for method, path, body in ROUTES[1:]:
            ph.next_step(env)
            if via == "session":
                headers = {**cookie, ph.TOTP: "000000"}
            else:
                # The person's own valid code: it opens the request, and must
                # never be checked against the secret of ADMIN_KEY.
                headers = ph.bearer(made, ph.code_now(env, made))
            admin_client.cookies.clear()
            r = await admin_client.request(method, path, json=body, headers=headers)
            assert r.status_code == 403 and r.json()["detail"] == _REFUSED
    assert "key:admin" not in ratelimit._auth_buckets
    assert "key:admin" not in ratelimit._lockouts
    for action in ("admin.mfa.step_up_failed", "admin.mfa.step_up_missing"):
        assert not audit.get_audit_log(action=action)
    assert admin_mfa.is_enrolled()
    admin_client.cookies.clear()
    code = admin_mfa._generate_code(secret, at=time.time())
    r = await admin_client.delete("/admin/mfa", headers={**ph.ADMIN, ph.TOTP: code})
    assert r.status_code == 204
    assert not admin_mfa.is_enrolled()


# -- A person refused on an /admin/mfa route leaves one audit entry -----------


@pytest.mark.parametrize("via", ["session", "key"])
@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_a_refused_person_is_audited_under_their_own_name(
    admin_client, env, via, method, path, body
):
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    made = ph.new_person()
    pid = made["person"]["id"]
    if via == "session":
        cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
        ph.next_step(env)
        headers = {**cookie, ph.TOTP: "000000"}
    else:
        headers = ph.bearer(made, ph.code_now(env, made))
    admin_client.cookies.clear()
    r = await admin_client.request(method, path, json=body, headers=headers)
    assert r.status_code == 403 and r.json()["detail"] == _REFUSED
    entries = audit.get_audit_log(action="admin.mfa.person_refused")
    assert len(entries) == 1
    entry = entries[0]
    assert (entry["principal"], entry["principal_name"]) == (
        f"person:{pid}",
        "person-one",
    )
    assert path in entry["details"] and method in entry["details"]
    # No code was read, nothing counts against ADMIN_KEY.
    assert "key:admin" not in ratelimit._auth_buckets
    assert "key:admin" not in ratelimit._lockouts
    for action in ("admin.mfa.step_up_failed", "admin.mfa.step_up_missing"):
        assert not audit.get_audit_log(action=action)
    assert not [
        e
        for e in audit.get_audit_log(limit=1000)
        if e["action"].startswith("admin.mfa") and e["principal"] == "admin-key"
    ]
    assert admin_mfa.is_enrolled()


async def test_admin_key_on_the_mfa_routes_writes_no_person_refusal(admin_client, env):
    admin_client.cookies.clear()
    r = await admin_client.post("/admin/mfa/begin", headers=ph.ADMIN)
    assert r.status_code == 200
    assert not audit.get_audit_log(action="admin.mfa.person_refused")
