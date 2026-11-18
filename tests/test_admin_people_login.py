"""Sign in with a personal key; its sessions die with it (PB-AUTH-01)."""

import json
import logging

import pytest

from plugboard import admin_people, admin_sessions, audit, ratelimit
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


def _sessions(env):
    return json.loads((env.dir / "admin_sessions.json").read_text())["sessions"]


async def test_login_with_a_personal_key_asks_for_its_code(admin_client, env):
    made = ph.new_person()
    r = await admin_client.post("/auth/admin/login", json={"key": made["key"]})
    assert r.status_code == 401
    assert r.json() == {"detail": "MFA code required", "mfa_required": True}
    assert "set-cookie" not in r.headers


async def test_its_code_opens_a_session_bound_to_the_person(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    assert await ph.keys_status(admin_client, cookie) == 200
    record = next(iter(_sessions(env).values()))
    assert record["person_id"] == pid and record["mfa"] is True
    assert record["person_generation"] == admin_people.get(pid)["generation"]
    assert (
        audit.get_audit_log(action="auth.login_success")[0]["principal"]
        == f"person:{pid}"
    )
    admin_client.cookies.clear()
    await admin_client.post(
        "/admin/keys", json={"client_id": "acme-test"}, headers=cookie
    )
    assert audit.get_audit_log(action="key.create")[0]["principal"] == f"person:{pid}"


async def test_admin_key_login_and_sessions_keep_their_shape(admin_client, env):
    cookie = await ph.login(admin_client, "adminkey")
    assert await ph.keys_status(admin_client, cookie) == 200
    assert "person_id" not in next(iter(_sessions(env).values()))
    assert (
        audit.get_audit_log(action="auth.login_success")[0]["principal"] == "admin-key"
    )


async def test_an_unknown_personal_key_counts_against_the_address(admin_client, env):
    r = await admin_client.post(
        "/auth/admin/login", json={"key": "pba_" + "B" * 43, "totp_code": "123456"}
    )
    assert r.status_code == 401 and r.json()["detail"] == "Admin key required"
    assert list(ratelimit._auth_buckets) == ["ip:127.0.0.1"]


async def test_a_valid_personal_key_passes_an_address_lockout(admin_client, env):
    for _ in range(5):
        await admin_client.post("/auth/admin/login", json={"key": "not-the-key"})
    assert not ratelimit.check_auth_rate_limit("ip:127.0.0.1")
    made = ph.new_person()
    await ph.login(admin_client, made["key"], ph.code_now(env, made))


async def test_wrong_login_codes_lock_that_person_only(admin_client, env):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    bad = {"key": one["key"], "totp_code": ph.wrong(ph.code_now(env, one))}
    codes = [
        (await admin_client.post("/auth/admin/login", json=bad)).status_code
        for _ in range(5)
    ]
    assert codes == [401, 401, 401, 401, 429]
    right = {"key": one["key"], "totp_code": ph.code_now(env, one)}
    assert (await admin_client.post("/auth/admin/login", json=right)).status_code == 429
    await ph.login(admin_client, two["key"], ph.code_now(env, two))
    await ph.login(admin_client, "adminkey")
    assert set(ratelimit._lockouts) == {f"key:admin:{one['person']['id']}"}


async def test_a_code_used_at_login_is_refused_on_the_header(admin_client, env):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    code = ph.code_now(env, one)
    await ph.login(admin_client, one["key"], code)
    assert await ph.keys_status(admin_client, ph.bearer(one, code)) == 401
    await ph.login(admin_client, two["key"], ph.code_now(env, two))


async def test_revoking_the_person_cuts_the_session_at_once(admin_client, env):
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
    assert await ph.keys_status(admin_client, cookie) == 401
    admin_client.cookies.clear()
    page = await admin_client.get("/dashboard", headers=cookie)
    assert page.status_code in (302, 307) and "/login" in page.headers["location"]


async def test_a_session_from_another_generation_is_refused(admin_client, env):
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    data = json.loads(env.store.read_text())
    data["people"][0]["generation"] = "0" * 32
    env.store.write_text(json.dumps(data))
    assert await ph.keys_status(admin_client, cookie) == 401


async def test_a_revoked_key_cannot_sign_in(admin_client, env):
    made = ph.new_person()
    admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
    r = await admin_client.post(
        "/auth/admin/login",
        json={"key": made["key"], "totp_code": ph.code_now(env, made)},
    )
    assert r.status_code == 401


async def test_an_unreadable_store_refuses_people_but_not_the_admin_key(
    admin_client, env, caplog
):
    made = ph.new_person()
    person = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin = await ph.login(admin_client, "adminkey")
    env.store.write_text("{not json")
    ph.next_step(env)
    assert await ph.keys_status(admin_client, person) == 401
    r = await admin_client.post(
        "/auth/admin/login",
        json={"key": made["key"], "totp_code": ph.code_now(env, made)},
    )
    assert r.status_code == 401
    assert await ph.keys_status(admin_client, admin) == 200
    assert await ph.keys_status(admin_client, ph.ADMIN) == 200
    assert "not json" not in caplog.text


async def test_a_person_session_is_refused_cross_site(admin_client, env):
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_client.cookies.clear()
    r = await admin_client.post(
        "/admin/keys",
        json={"client_id": "acme-test"},
        headers={**cookie, "Sec-Fetch-Site": "cross-site"},
    )
    assert r.status_code == 403


# -- Additions: anti-goals and finding C4 -------------------------------------


async def test_a_revoked_person_session_left_in_the_store_is_refused(
    admin_client, env, monkeypatch
):
    """The revoked status alone refuses the session (the store kept it)."""
    made = ph.new_person()
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))

    def store_not_writable(person_id):
        raise OSError("read-only")

    monkeypatch.setattr(admin_sessions, "revoke_person", store_not_writable)
    admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
    assert len(_sessions(env)) == 1
    assert await ph.keys_status(admin_client, cookie) == 401
    admin_client.cookies.clear()
    page = await admin_client.get("/dashboard", headers=cookie)
    assert page.status_code in (302, 307) and "/login" in page.headers["location"]


async def test_the_first_login_code_confirms_the_key_and_names_the_person(
    admin_client, env
):
    made = ph.new_person()
    pid = made["person"]["id"]
    await ph.login(admin_client, made["key"], ph.code_now(env, made))
    confirmed = audit.get_audit_log(action="admin.person.confirmed")
    assert [(e["target_id"], e["details"]) for e in confirmed] == [(pid, "via=login")]
    assert (confirmed[0]["principal"], confirmed[0]["principal_name"]) == (
        f"person:{pid}",
        "person-one",
    )
    success = audit.get_audit_log(action="auth.login_success")[0]
    assert success["principal_name"] == "person-one"


async def test_a_locked_person_login_is_audited_with_its_author(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    bad = {"key": made["key"], "totp_code": ph.wrong(ph.code_now(env, made))}
    for _ in range(5):
        await admin_client.post("/auth/admin/login", json=bad)
    r = await admin_client.post(
        "/auth/admin/login",
        json={"key": made["key"], "totp_code": ph.code_now(env, made)},
    )
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    blocked = audit.get_audit_log(action="auth.login_blocked")
    assert len(blocked) == 1
    entry = blocked[0]
    assert (entry["target_type"], entry["target_id"]) == ("admin_person", pid)
    assert entry["details"].startswith("personal key locked out (MFA), IP: ")
    assert (entry["principal"], entry["principal_name"]) == (
        f"person:{pid}",
        "person-one",
    )
    failed = audit.get_audit_log(action="auth.person.mfa_failed")
    assert len(failed) == 5
    assert {e["principal"] for e in failed} == {f"person:{pid}"}
    assert {e["details"].split(", ")[0] for e in failed} == {"IP: 127.0.0.1"}


async def test_logout_names_the_author_of_the_session(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    for key, code in ((made["key"], ph.code_now(env, made)), ("adminkey", None)):
        cookie = await ph.login(admin_client, key, code)
        r = await admin_client.post("/auth/admin/logout", headers=cookie)
        assert r.status_code == 204
        assert await ph.keys_status(admin_client, cookie) == 401
    rows = audit.get_audit_log(action="auth.logout")
    assert sorted((e["principal"], e["principal_name"]) for e in rows) == [
        ("admin-key", ""),
        (f"person:{pid}", "person-one"),
    ]


async def test_no_raw_key_code_or_secret_at_login(admin_client, env, caplog):
    made = ph.new_person()
    good = ph.code_now(env, made)
    bad = ph.wrong(good)
    caplog.set_level(logging.DEBUG)
    answers = []
    for code in (None, bad, good):
        admin_client.cookies.clear()
        body = (
            {"key": made["key"]}
            if code is None
            else {"key": made["key"], "totp_code": code}
        )
        r = await admin_client.post("/auth/admin/login", json=body)
        answers.append(r.text + json.dumps(dict(r.headers)))
    assert [a for a in answers if "set-cookie" in a] == answers[2:]
    entries = json.dumps(audit.get_audit_log(limit=1000))
    stored = (env.dir / "admin_sessions.json").read_text()
    for value in (made["key"], made["totp_secret"], good, bad):
        assert value not in caplog.text
        assert value not in entries
        assert value not in stored
        assert all(value not in a for a in answers)


async def test_a_refused_login_names_no_principal_in_the_request(
    admin_client, env, monkeypatch
):
    made = ph.new_person()
    seen = []
    real_end = audit.end_request

    def end(token):
        seen.append(audit.current_principal())
        real_end(token)

    monkeypatch.setattr(audit, "end_request", end)
    bad = {"key": made["key"], "totp_code": ph.wrong(ph.code_now(env, made))}
    assert (await admin_client.post("/auth/admin/login", json=bad)).status_code == 401
    assert seen == [("", "")]


@pytest.mark.parametrize(
    "edit",
    [
        lambda rec: rec.update(id="not-an-id"),
        lambda rec: rec.pop("generation"),
        lambda rec: rec.update(generation=""),
    ],
    ids=["bad-id", "no-generation", "empty-generation"],
)
async def test_a_hand_edited_record_cannot_sign_in_never_a_500(admin_client, env, edit):
    made = ph.new_person()
    code = ph.code_now(env, made)
    data = json.loads(env.store.read_text())
    edit(data["people"][0])
    env.store.write_text(json.dumps(data))
    r = await admin_client.post(
        "/auth/admin/login", json={"key": made["key"], "totp_code": code}
    )
    assert r.status_code == 401
    assert "set-cookie" not in r.headers
    assert not (env.dir / "admin_sessions.json").exists()
    assert not ratelimit._auth_buckets and not ratelimit._lockouts
