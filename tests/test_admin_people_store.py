"""The personal dashboard key store (PB-AUTH-01)."""

import hashlib
import json
import os
import re
import stat

import pytest

from plugboard import admin_people
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
