"""A personal key on the admin API, always with its own TOTP code (PB-AUTH-01)."""

import json
import logging

import pytest

from plugboard import admin_mfa, admin_people, audit, ratelimit
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


async def test_a_personal_key_without_its_code_is_refused_even_without_global_mfa(
    admin_client, env
):
    made = ph.new_person()
    assert not admin_mfa.is_required()
    assert await ph.keys_status(admin_client, ph.bearer(made)) == 401
    assert await ph.keys_status(admin_client, ph.ADMIN) == 200


async def test_its_code_makes_it_admin_and_confirms_the_key(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    assert (
        await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made)))
        == 200
    )
    assert admin_people.list_people()[0]["status"] == "active"
    confirmed = audit.get_audit_log(action="admin.person.confirmed")
    assert [e["target_id"] for e in confirmed] == [pid]
    assert confirmed[0]["principal"] == f"person:{pid}"
    assert confirmed[0]["principal_name"] == "person-one"


async def test_a_later_code_does_not_confirm_again(admin_client, env):
    made = ph.new_person()
    assert (
        await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made)))
        == 200
    )
    ph.next_step(env)
    assert (
        await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made)))
        == 200
    )
    assert len(audit.get_audit_log(action="admin.person.confirmed")) == 1


async def test_a_code_of_another_secret_is_refused(admin_client, env):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    assert (
        await ph.keys_status(admin_client, ph.bearer(one, ph.code_now(env, two))) == 401
    )
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    admin_code = admin_mfa._generate_code(secret, at=env.clock.t)
    assert await ph.keys_status(admin_client, ph.bearer(one, admin_code)) == 401


async def test_a_code_is_single_use_on_the_header(admin_client, env):
    made = ph.new_person()
    headers = ph.bearer(made, ph.code_now(env, made))
    assert await ph.keys_status(admin_client, headers) == 200
    assert await ph.keys_status(admin_client, headers) == 401


async def test_an_unknown_personal_key_is_denied_and_not_counted(admin_client, env):
    headers = {"Authorization": "Bearer pba_" + "A" * 43, ph.TOTP: "123456"}
    assert await ph.keys_status(admin_client, headers) == 401
    assert not ratelimit._auth_buckets and not ratelimit._lockouts


async def test_a_revoked_key_with_a_valid_code_is_refused(admin_client, env):
    made = ph.new_person()
    admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
    assert (
        await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made)))
        == 401
    )


async def test_an_unreadable_store_refuses_every_personal_key_without_500(
    admin_client, env
):
    made = ph.new_person()
    code = ph.code_now(env, made)
    env.store.write_text("{not json", encoding="utf-8")
    assert await ph.keys_status(admin_client, ph.bearer(made, code)) == 401
    assert await ph.keys_status(admin_client, ph.bearer(made)) == 401
    assert await ph.keys_status(admin_client, ph.ADMIN) == 200


async def test_wrong_codes_lock_that_person_only(admin_client, env, monkeypatch):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    pid = one["person"]["id"]
    bad = ph.wrong(ph.code_now(env, one))
    for _ in range(5):
        assert await ph.keys_status(admin_client, ph.bearer(one, bad)) == 401
    assert (
        await ph.keys_status(admin_client, ph.bearer(one, ph.code_now(env, one))) == 429
    )
    assert (
        await ph.keys_status(admin_client, ph.bearer(two, ph.code_now(env, two))) == 200
    )
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    admin = {**ph.ADMIN, ph.TOTP: admin_mfa._generate_code(secret)}
    assert await ph.keys_status(admin_client, admin) == 200
    assert set(ratelimit._lockouts) == {f"key:admin:{pid}"}
    failed = audit.get_audit_log(action="auth.person.mfa_failed")
    assert len(failed) == 5 and {e["target_id"] for e in failed} == {pid}
    assert {e["principal"] for e in failed} == {f"person:{pid}"}
    assert {e["principal_name"] for e in failed} == {"person-one"}
    assert bad not in json.dumps(failed)


async def test_no_raw_key_code_or_secret_in_logs_or_audit(admin_client, env, caplog):
    made = ph.new_person()
    good = ph.code_now(env, made)
    bad = ph.wrong(good)
    caplog.set_level(logging.DEBUG)
    assert await ph.keys_status(admin_client, ph.bearer(made, bad)) == 401
    assert await ph.keys_status(admin_client, ph.bearer(made, good)) == 200
    entries = json.dumps(audit.get_audit_log(limit=1000))
    for value in (made["key"], made["totp_secret"], good, bad):
        assert value not in caplog.text
        assert value not in entries


@pytest.mark.parametrize("page", ["/dashboard", "/suivi"])
async def test_without_admin_key_a_personal_key_opens_no_page(
    admin_client, env, monkeypatch, page
):
    made = ph.new_person()
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")
    admin_client.cookies.clear()
    resp = await admin_client.get(page, headers=ph.bearer(made, ph.code_now(env, made)))
    assert resp.status_code in (302, 303, 307)
    assert resp.headers["location"].startswith("/login")
    assert admin_people.list_people()[0]["status"] == "pending"


async def test_without_admin_key_a_personal_key_is_refused_on_admin_routes(
    admin_client, env, monkeypatch
):
    made = ph.new_person()
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")
    status = await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made)))
    assert status == 503


@pytest.mark.parametrize("bad_id", [None, "ap_000000000000x", "../ap_000000000000", 7])
async def test_a_hand_edited_person_id_is_refused_never_a_500(
    admin_client, env, bad_id
):
    made = ph.new_person()
    code = ph.code_now(env, made)
    data = json.loads(env.store.read_text())
    if bad_id is None:
        del data["people"][0]["id"]
    else:
        data["people"][0]["id"] = bad_id
    env.store.write_text(json.dumps(data))
    assert await ph.keys_status(admin_client, ph.bearer(made, code)) == 401
    assert not ratelimit._auth_buckets and not ratelimit._lockouts
    assert audit.get_audit_log(action="auth.person.mfa_failed") == []


async def test_an_admin_key_starting_with_the_personal_prefix_stays_the_admin_key(
    admin_client, env, monkeypatch
):
    admin_key = "pba_" + "Z" * 43
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", admin_key)
    headers = {"Authorization": f"Bearer {admin_key}"}
    assert await ph.keys_status(admin_client, headers) == 200
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert await ph.keys_status(admin_client, headers) == 401
    bad = ph.wrong(admin_mfa._generate_code(secret))
    assert await ph.keys_status(admin_client, {**headers, ph.TOTP: bad}) == 401
    assert set(ratelimit._auth_buckets) == {"key:admin"}
    assert len(audit.get_audit_log(action="auth.mfa_failed")) == 1
    assert audit.get_audit_log(action="auth.person.mfa_failed") == []
    good = {**headers, ph.TOTP: admin_mfa._generate_code(secret)}
    assert await ph.keys_status(admin_client, good) == 200


async def test_a_personal_code_never_counts_as_the_admin_key_code(admin_client, env):
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    made = ph.new_person()
    admin_client.cookies.clear()
    resp = await admin_client.delete(
        "/admin/mfa", headers=ph.bearer(made, ph.code_now(env, made))
    )
    assert resp.status_code == 403
    assert admin_mfa.is_enrolled()


async def test_a_store_that_cannot_record_the_step_is_not_a_wrong_code(
    admin_client, env, monkeypatch, caplog
):
    made = ph.new_person()
    code = ph.code_now(env, made)

    def fail(people):
        raise OSError("disk full")

    monkeypatch.setattr(admin_people, "_save", fail)
    caplog.set_level(logging.DEBUG)
    assert await ph.keys_status(admin_client, ph.bearer(made, code)) == 401
    assert not ratelimit._auth_buckets and not ratelimit._lockouts
    assert audit.get_audit_log(action="auth.person.mfa_failed") == []
    assert "OSError" in caplog.text and code not in caplog.text


async def test_a_store_unreadable_at_code_time_is_not_a_wrong_code(
    admin_client, env, monkeypatch
):
    made = ph.new_person()
    code = ph.code_now(env, made)
    record = admin_people.find_by_key(made["key"])
    monkeypatch.setattr(admin_people, "find_by_key", lambda raw: record)
    env.store.write_text("{not json", encoding="utf-8")
    assert await ph.keys_status(admin_client, ph.bearer(made, code)) == 401
    assert not ratelimit._auth_buckets and not ratelimit._lockouts
    assert audit.get_audit_log(action="auth.person.mfa_failed") == []


def test_check_code_tells_a_server_fault_from_a_wrong_code(env, monkeypatch):
    made = ph.new_person()
    pid, code = made["person"]["id"], ph.code_now(env, made)
    assert admin_people.check_code(pid, ph.wrong(code)) == "refused"

    def fail(people):
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(admin_people, "_save", fail)
        assert admin_people.check_code(pid, code) == "unavailable"
        assert admin_people.verify_code(pid, code) == "refused"
    env.store.write_text("{not json", encoding="utf-8")
    assert admin_people.check_code(pid, code) == "unavailable"
