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
