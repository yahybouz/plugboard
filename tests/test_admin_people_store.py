"""The personal dashboard key store (PB-AUTH-01)."""

import hashlib
import json
import os
import re
import stat
import threading
import time as _time

import pytest

from plugboard import admin_people, admin_sessions
from plugboard.storage import StoreUnreadableError
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


def test_create_returns_the_key_and_the_secret_once(env):
    made = ph.new_person()
    assert made["key"].startswith("pba_") and len(made["key"]) >= 40
    assert made["otpauth_uri"].startswith(
        "otpauth://totp/Plugboard%3Aperson-one?secret=" + made["totp_secret"]
    )
    assert "issuer=Plugboard" in made["otpauth_uri"]
    view = made["person"]
    assert set(view) == admin_people.VIEW_FIELDS
    assert re.fullmatch(r"ap_[0-9a-f]{12}", view["id"])
    assert (view["name"], view["status"], view["created_by"]) == (
        "person-one",
        "pending",
        "admin-key",
    )
    assert view["key_prefix"] == made["key"][:10]


def test_the_store_holds_neither_the_key_nor_the_secret(env):
    made = ph.new_person()
    text = env.store.read_text()
    assert made["key"] not in text and made["totp_secret"] not in text
    record = json.loads(text)["people"][0]
    assert record["key_hash"] == hashlib.sha256(made["key"].encode()).hexdigest()
    assert record["totp_secret_enc"].startswith("enc:")
    assert stat.S_IMODE(os.stat(env.store).st_mode) == 0o600


def test_the_list_never_shows_a_hash_or_a_secret(env):
    made = ph.new_person()
    listed = admin_people.list_people()
    assert [p["id"] for p in listed] == [made["person"]["id"]]
    assert all(set(p) == admin_people.VIEW_FIELDS for p in listed)
    dumped = json.dumps(listed)
    assert "key_hash" not in dumped and "totp_secret" not in dumped
    assert made["key"] not in dumped and made["totp_secret"] not in dumped


@pytest.mark.parametrize(
    "name", ["", "   ", "x" * 65, "two\nlines", "tab\there", "nul\x00"]
)
def test_invalid_names_are_refused(env, name):
    with pytest.raises(admin_people.PersonError) as err:
        admin_people.create(name, created_by="admin-key")
    assert err.value.code == "invalid_name"
    assert str(err.value) == "Name must be 1 to 64 printable characters"
    assert not env.store.exists()


def test_a_name_is_trimmed_and_unique_whatever_its_case(env):
    admin_people.create("  Person-One ", created_by="admin-key")
    with pytest.raises(admin_people.PersonError) as err:
        admin_people.create("person-one", created_by="admin-key")
    assert err.value.code == "name_taken"
    assert [p["name"] for p in admin_people.list_people()] == ["Person-One"]


def test_printable_unicode_names_are_accepted(env):
    assert ph.new_person("Zoé Été")["person"]["name"] == "Zoé Été"


@pytest.mark.parametrize("content", ["{not json", "[]", '{"people": "x"}'])
def test_an_unreadable_store_refuses_create_and_list(env, content, caplog):
    env.store.write_text(content)
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.list_people()
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.create("person-one", created_by="admin-key")
    assert env.store.read_text() == content
    assert "not json" not in caplog.text


WITNESS = "witness-5c1e9a0b"


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        '{"people": [1]}',
        '{"people": [{"id": "ap_000000000000"}, "' + WITNESS + '"]}',
        '{"' + WITNESS + '": ',
        '"' + WITNESS + '"',
    ],
)
def test_a_malformed_store_fails_closed_without_quoting_it(env, content, caplog):
    env.store.write_text(content)
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.list_people()
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.create("person-one", created_by="admin-key")
    assert env.store.read_text() == content
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert WITNESS not in caplog.text


def test_a_store_that_is_not_utf8_is_unreadable(env, caplog):
    raw = b"\xff\xfe" + WITNESS.encode() + b"\x80"
    env.store.write_bytes(raw)
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.list_people()
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.create("person-one", created_by="admin-key")
    assert env.store.read_bytes() == raw
    assert WITNESS not in caplog.text


def test_a_directory_in_place_of_the_store_is_unreadable(env):
    env.store.mkdir()
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.list_people()
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.create("person-one", created_by="admin-key")
    assert env.store.is_dir()


class _UnreachablePath:
    """A store path whose parent cannot be searched: every look raises."""

    def exists(self):
        raise PermissionError(13, "Permission denied")

    def read_text(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")


def test_a_store_that_cannot_be_reached_is_unreadable(env, monkeypatch, caplog):
    monkeypatch.setattr(admin_people, "_path", lambda: _UnreachablePath())
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.list_people()
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.create("person-one", created_by="admin-key")
    assert "PermissionError" in caplog.text


def test_find_by_key_returns_the_live_record_only(env):
    made = ph.new_person()
    assert admin_people.find_by_key(made["key"])["id"] == made["person"]["id"]
    assert admin_people.find_by_key(made["key"] + "x") is None
    assert admin_people.find_by_key("adminkey") is None


def test_the_first_code_confirms_then_each_code_is_single_use(env):
    made = ph.new_person()
    pid, first = made["person"]["id"], ph.code_now(env, made)
    assert admin_people.verify_code(pid, first) == "confirmed"
    view = admin_people.list_people()[0]
    assert view["status"] == "active" and view["confirmed_at"] and view["last_used_at"]
    assert admin_people.verify_code(pid, first) == "refused"
    ph.next_step(env)
    assert admin_people.verify_code(pid, ph.code_now(env, made)) == "ok"


def test_an_older_code_is_refused_after_a_newer_one(env):
    made = ph.new_person()
    pid, older = made["person"]["id"], ph.code_now(env, made)
    ph.next_step(env)
    assert admin_people.verify_code(pid, ph.code_now(env, made)) == "confirmed"
    assert admin_people.verify_code(pid, older) == "refused"


@pytest.mark.parametrize("bad", ["", "12345", "abcdef"])
def test_a_wrong_or_malformed_code_is_refused(env, bad):
    made = ph.new_person()
    pid = made["person"]["id"]
    assert admin_people.verify_code(pid, bad) == "refused"
    assert admin_people.verify_code(pid, ph.wrong(ph.code_now(env, made))) == "refused"
    assert admin_people.list_people()[0]["status"] == "pending"


def test_each_person_has_their_own_replay_state(env):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    assert (
        admin_people.verify_code(one["person"]["id"], ph.code_now(env, one))
        == "confirmed"
    )
    assert (
        admin_people.verify_code(two["person"]["id"], ph.code_now(env, two))
        == "confirmed"
    )


def test_an_unreadable_store_refuses_keys_and_codes(env):
    made = ph.new_person()
    pid = made["person"]["id"]
    env.store.write_text("{not json")
    assert admin_people.find_by_key(made["key"]) is None
    assert admin_people.get(pid) is None
    assert admin_people.verify_code(pid, ph.code_now(env, made)) == "refused"


@pytest.mark.parametrize("stored", ["enc:not-a-fernet-token", "JBSWY3DPEHPK3PXP"])
def test_a_secret_that_cannot_be_decrypted_refuses_codes(env, stored):
    made = ph.new_person()
    data = json.loads(env.store.read_text())
    data["people"][0]["totp_secret_enc"] = stored
    env.store.write_text(json.dumps(data))
    assert (
        admin_people.verify_code(made["person"]["id"], ph.code_now(env, made))
        == "refused"
    )


def test_a_revoked_record_is_kept_by_get_but_never_matches(env):
    made = ph.new_person()
    pid = made["person"]["id"]
    data = json.loads(env.store.read_text())
    data["people"][0]["revoked_at"] = "2026-01-01T00:00:00+00:00"
    env.store.write_text(json.dumps(data))
    assert admin_people.find_by_key(made["key"]) is None
    assert admin_people.get(pid)["revoked_at"]
    assert admin_people.verify_code(pid, ph.code_now(env, made)) == "refused"


@pytest.mark.parametrize("bad_id", ["", "ap_000000000000x", "../ap_000000000000", 7])
def test_an_id_outside_the_pattern_is_refused(env, bad_id):
    made = ph.new_person()
    data = json.loads(env.store.read_text())
    data["people"][0]["id"] = bad_id  # even a record stored under it is not reached
    env.store.write_text(json.dumps(data))
    assert admin_people.get(bad_id) is None
    assert admin_people.verify_code(bad_id, ph.code_now(env, made)) == "refused"


def test_a_code_is_refused_when_its_step_cannot_be_recorded(env, monkeypatch, caplog):
    made = ph.new_person()
    pid, code = made["person"]["id"], ph.code_now(env, made)

    def fail(people):
        raise OSError("disk full")

    monkeypatch.setattr(admin_people, "_save", fail)
    assert admin_people.verify_code(pid, code) == "refused"
    assert "OSError" in caplog.text and code not in caplog.text
    assert admin_people.list_people()[0]["status"] == "pending"


@pytest.mark.parametrize("last", ["5", True, 1.5])
def test_a_malformed_last_step_refuses_codes(env, last):
    made = ph.new_person()
    data = json.loads(env.store.read_text())
    data["people"][0]["last_step"] = last
    env.store.write_text(json.dumps(data))
    assert (
        admin_people.verify_code(made["person"]["id"], ph.code_now(env, made))
        == "refused"
    )


def test_a_replayed_code_is_logged_without_the_code(env, caplog):
    made = ph.new_person()
    pid, code = made["person"]["id"], ph.code_now(env, made)
    assert admin_people.verify_code(pid, code) == "confirmed"
    assert admin_people.verify_code(pid, code) == "refused"
    assert "replayed" in caplog.text and pid in caplog.text
    assert code not in caplog.text


def test_a_code_of_one_person_never_verifies_for_another(env):
    one, two = ph.new_person("person-one"), ph.new_person("person-two")
    while ph.code_now(env, one) == ph.code_now(env, two):
        ph.next_step(env)  # two random secrets can share a code at one step
    other = two["person"]["id"]
    assert admin_people.verify_code(other, ph.code_now(env, one)) == "refused"
    assert admin_people.get(other)["last_step"] is None
    assert admin_people.verify_code(other, ph.code_now(env, two)) == "confirmed"
    assert (
        admin_people.verify_code(one["person"]["id"], ph.code_now(env, one))
        == "confirmed"
    )


def test_a_code_is_accepted_once_when_two_threads_check_it_together(env, monkeypatch):
    made = ph.new_person()
    pid, code = made["person"]["id"], ph.code_now(env, made)
    real_load = admin_people._load

    def slow_load():
        loaded = real_load()
        _time.sleep(0.05)  # widen the read-check-write window
        return loaded

    monkeypatch.setattr(admin_people, "_load", slow_load)
    start, results = threading.Barrier(2), []

    def check():
        start.wait()
        results.append(admin_people.verify_code(pid, code))

    threads = [threading.Thread(target=check) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(results) == ["confirmed", "refused"]


def _session(nonce, **person):
    admin_sessions.register(
        nonce, int(_time.time()), mfa=bool(person), max_age=3600, **person
    )


def test_revoke_marks_the_record_changes_its_generation_and_is_idempotent(env):
    made = ph.new_person()
    pid = made["person"]["id"]
    before = admin_people.get(pid)["generation"]
    view, changed = admin_people.revoke(pid, revoked_by="admin-key")
    assert changed and view["status"] == "revoked"
    assert view["revoked_by"] == "admin-key" and view["revoked_at"]
    assert admin_people.get(pid)["generation"] != before
    assert admin_people.find_by_key(made["key"]) is None
    assert admin_people.verify_code(pid, ph.code_now(env, made)) == "refused"
    again, changed = admin_people.revoke(pid, revoked_by="person:ap_000000000000")
    assert not changed and again["revoked_by"] == "admin-key"


@pytest.mark.parametrize(
    "pid", ["ap_000000000000", "../admin", "ap_XYZ", "", "ap_000000000000x"]
)
def test_revoking_an_unknown_or_malformed_id_returns_none(env, pid):
    ph.new_person()
    assert admin_people.revoke(pid, revoked_by="admin-key") is None


def test_a_revoked_name_can_be_given_again(env):
    first = ph.new_person()
    admin_people.revoke(first["person"]["id"], revoked_by="admin-key")
    assert ph.new_person()["person"]["status"] == "pending"


def test_an_unreadable_store_refuses_a_revocation(env):
    pid = ph.new_person()["person"]["id"]
    env.store.write_text("{not json")
    with pytest.raises(admin_people.PeopleStoreUnreadable):
        admin_people.revoke(pid, revoked_by="admin-key")


def test_sessions_record_the_person_and_revoke_person_drops_only_theirs(env):
    _session("n-admin")
    _session("n-one", person_id="ap_aaaaaaaaaaaa", person_generation="g1")
    _session("n-two", person_id="ap_bbbbbbbbbbbb", person_generation="g2")
    one = admin_sessions.lookup("n-one")
    assert (one["person_id"], one["person_generation"]) == ("ap_aaaaaaaaaaaa", "g1")
    assert "person_id" not in admin_sessions.lookup("n-admin")
    assert admin_sessions.revoke_person("ap_aaaaaaaaaaaa") == 1
    assert admin_sessions.lookup("n-one") is None
    assert admin_sessions.lookup("n-two") and admin_sessions.lookup("n-admin")


def test_revoke_cuts_the_person_sessions(env):
    pid = ph.new_person()["person"]["id"]
    _session("n-one", person_id=pid, person_generation="g")
    admin_people.revoke(pid, revoked_by="admin-key")
    assert admin_sessions.lookup("n-one") is None


def test_revoke_person_on_an_unreadable_session_store_does_not_raise(env):
    (env.dir / "admin_sessions.json").write_text("{bad")
    assert admin_sessions.revoke_person("ap_aaaaaaaaaaaa") == 0


def test_a_revocation_and_a_code_check_at_once_lose_neither(env, monkeypatch):
    made = ph.new_person()
    pid, code = made["person"]["id"], ph.code_now(env, made)
    real_load = admin_people._load

    def slow_load():
        loaded = real_load()
        _time.sleep(0.05)  # widen the read-modify-write window
        return loaded

    monkeypatch.setattr(admin_people, "_load", slow_load)
    start, outcome = threading.Barrier(2), {}

    def check():
        start.wait()
        outcome["code"] = admin_people.verify_code(pid, code)

    def revoke():
        start.wait()
        admin_people.revoke(pid, revoked_by="admin-key")

    threads = [threading.Thread(target=check), threading.Thread(target=revoke)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    record = admin_people.get(pid)
    assert record["revoked_at"]
    if outcome["code"] != "refused":
        assert record["last_step"] is not None


@pytest.mark.parametrize("error", [OSError("disk full"), StoreUnreadableError("x")])
def test_a_session_store_that_cannot_be_rewritten_does_not_undo_a_revocation(
    env, monkeypatch, caplog, error
):
    pid = ph.new_person()["person"]["id"]

    def fail(person_id):
        raise error

    monkeypatch.setattr(admin_sessions, "revoke_person", fail)
    view, changed = admin_people.revoke(pid, revoked_by="admin-key")
    assert changed and view["status"] == "revoked"
    assert type(error).__name__ in caplog.text
