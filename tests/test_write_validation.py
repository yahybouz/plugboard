"""Admin writes are validated before anything is stored (BUG-59, BUG-66).

- a key's allowed_models must name models, and models some provider knows;
- a provider address that cannot be parsed is refused, guard or not;
- rotating a key is all or nothing;
- the key hash never leaves the server.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import _setup_providers
from tests.factories import PROVIDER_B

ADMIN = {"Authorization": "Bearer adminkey"}


@pytest.fixture
def keys_store(monkeypatch, tmp_path):
    import plugboard.apikeys as keys

    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    yield keys
    keys._invalidate_cache()


# -- The key hash stays on the server ------------------------------------------


async def test_admin_key_answers_carry_no_hash(admin_client, keys_store):
    r = await admin_client.post(
        "/admin/keys",
        headers=ADMIN,
        json={"client_id": "billing", "org_id": "acme"},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    rotated = await admin_client.post(
        f"/admin/keys/{created['key_id']}/rotate", headers=ADMIN
    )
    assert rotated.status_code == 201, rotated.text
    answers = [created, rotated.json()]
    for path in ("/admin/keys", "/admin/keys/client/billing", "/admin/orgs/acme/keys"):
        r = await admin_client.get(path, headers=ADMIN)
        assert r.status_code == 200, (path, r.text)
        assert r.json() and all("key_display" in k for k in r.json())
        answers.extend(r.json())
    assert all("key_hash" not in answer for answer in answers)
    # The hash is still what the server checks the key against.
    assert keys_store.get_key_record(rotated.json()["key"])["client_id"] == "billing"


# -- Rotation is all or nothing ------------------------------------------------


def _active(keys_store, client_id):
    return [
        k["key_id"]
        for k in keys_store.load_keys()
        if k["client_id"] == client_id and k.get("is_active", True)
    ]


@pytest.mark.parametrize("failing_write", [1, 2])
def test_rotation_that_fails_to_write_leaves_a_working_key(
    keys_store, monkeypatch, failing_write
):
    # Before, the old key was revoked by one write and the new one added by a
    # second: a failure of the second left the client with no working key.
    old = keys_store.create_key("billing", "prod")
    assert keys_store.get_key_record(old["key"])  # cache warm
    real_write = keys_store._write
    writes = []

    def _write(data):
        writes.append(1)
        if len(writes) == failing_write:
            raise OSError("disk full")
        real_write(data)

    monkeypatch.setattr(keys_store, "_write", _write)
    try:
        keys_store.rotate_key(old["key_id"])
    except OSError:
        pass
    assert len(_active(keys_store, "billing")) == 1
    if failing_write == 1:  # the only write of a rotation failed: nothing changed
        assert keys_store.get_key_record(old["key"])["key_id"] == old["key_id"]


def test_rotation_refused_by_the_new_key_leaves_the_old_one(keys_store):
    # A decision model no longer offered: the new key cannot be written.
    old = keys_store.create_key("billing", "prod")
    data = json.loads(keys_store.KEYS_FILE.read_text())
    data["keys"][0]["allowed_decision_models"] = ["retired-model"]
    keys_store.KEYS_FILE.write_text(json.dumps(data))
    keys_store._invalidate_cache()
    with pytest.raises(ValueError):
        keys_store.rotate_key(old["key_id"])
    keys_store._invalidate_cache()
    assert _active(keys_store, "billing") == [old["key_id"]]


def test_rotating_a_revoked_key_issues_nothing(keys_store):
    # BUG-80: a revoked key gave a new active key, so a revoke could be undone
    # and a repeated rotation left two live keys.
    old = keys_store.create_key("billing", "prod")
    keys_store.revoke_key(old["key_id"])
    with pytest.raises(keys_store.KeyRevoked):
        keys_store.rotate_key(old["key_id"])
    keys_store._invalidate_cache()
    assert _active(keys_store, "billing") == []
    assert len(keys_store.load_keys()) == 1


async def test_rotating_twice_answers_409_the_second_time(admin_client, keys_store):
    old = keys_store.create_key("billing", "prod")
    first = await admin_client.post(
        f"/admin/keys/{old['key_id']}/rotate", headers=ADMIN
    )
    assert first.status_code == 201, first.text
    again = await admin_client.post(
        f"/admin/keys/{old['key_id']}/rotate", headers=ADMIN
    )
    assert again.status_code == 409, again.text
    assert "already revoked" in again.json()["detail"]
    assert _active(keys_store, "billing") == [first.json()["key_id"]]


async def test_rotation_refused_by_the_new_key_answers_422(admin_client, keys_store):
    old = keys_store.create_key("billing", "prod")
    data = json.loads(keys_store.KEYS_FILE.read_text())
    data["keys"][0]["allowed_decision_models"] = ["retired-model"]
    keys_store.KEYS_FILE.write_text(json.dumps(data))
    keys_store._invalidate_cache()
    r = await admin_client.post(f"/admin/keys/{old['key_id']}/rotate", headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "create a new key, then revoke this one" in r.json()["detail"]
    assert _active(keys_store, "billing") == [old["key_id"]]


# -- allowed_models names models some provider knows ---------------------------


@pytest.mark.parametrize(
    "entry",
    ["", "   ", "other-provider:", "Codex:", " other:model-v2", "other:model-v2 "],
)
async def test_allowed_models_entry_that_names_no_model_is_refused(
    admin_client, keys_store, entry
):
    r = await admin_client.post(
        "/admin/keys",
        headers=ADMIN,
        json={"client_id": "billing", "allowed_models": ["other:model-v2", entry]},
    )
    assert r.status_code == 422, r.text
    [error] = r.json()["detail"]
    assert error["loc"][:2] == ["body", "allowed_models"]
    assert keys_store.load_keys() == []


@pytest.fixture
def listed_providers(admin_client, tmp_path, monkeypatch):
    """Only providers that list their models: an unknown name is known as such."""
    base = tmp_path / "listed"
    base.mkdir()
    return _setup_providers(base, monkeypatch, [PROVIDER_B])


@pytest.mark.parametrize(
    "entry", ["other:model-v9", "missing-provider:other:model-v2", "gpt-unknown"]
)
async def test_allowed_models_entry_no_provider_knows_is_refused(
    admin_client, keys_store, listed_providers, entry
):
    r = await admin_client.post(
        "/admin/keys",
        headers=ADMIN,
        json={"client_id": "billing", "allowed_models": [entry, "other:model-v2"]},
    )
    assert r.status_code == 422, r.text
    assert entry in r.json()["detail"]
    assert "'other:model-v2'" not in r.json()["detail"]  # the known one
    assert keys_store.load_keys() == []


@pytest.fixture
def dated_provider(admin_client, tmp_path, monkeypatch):
    """A provider that lists only a dated snapshot of a family."""
    base = tmp_path / "dated"
    base.mkdir()
    dated = {
        **PROVIDER_B,
        "id": "anthropic-direct",
        "models": [{"id": "claude-haiku-4-5-20251001", "name": "Haiku"}],
    }
    return _setup_providers(base, monkeypatch, [PROVIDER_B, dated])


@pytest.mark.parametrize(
    "entry, reason",
    [
        # Chat promotes it to the dated ID, which the guard then checks.
        ("claude-haiku-4-5", "dated ID"),
        ("anthropic-direct:claude-haiku-4-5", "dated ID"),
        # The guard compares exact IDs: another case never matches.
        ("OTHER:MODEL-V2", "spelled 'other:model-v2'"),
    ],
)
async def test_allowed_models_entry_known_under_another_spelling_is_refused(
    admin_client, keys_store, dated_provider, entry, reason
):
    r = await admin_client.post(
        "/admin/keys",
        headers=ADMIN,
        json={"client_id": "billing", "allowed_models": [entry]},
    )
    assert r.status_code == 422, r.text
    assert reason in r.json()["detail"]
    assert keys_store.load_keys() == []


async def test_allowed_models_dated_id_is_accepted(
    admin_client, keys_store, dated_provider
):
    r = await admin_client.post(
        "/admin/keys",
        headers=ADMIN,
        json={
            "client_id": "billing",
            "allowed_models": ["anthropic-direct:claude-haiku-4-5-20251001"],
        },
    )
    assert r.status_code == 201, r.text


async def test_provider_domain_refusal_stays_audited_before_the_parse_check(
    client, _providers_file, monkeypatch, tmp_path
):
    # TM-004: with an allowlist, a refused address is audited, parsable or not.
    from plugboard import audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")
    monkeypatch.setenv("PLUGBOARD_PROVIDER_DOMAINS_ALLOWED", "api.allowed.test")
    r = await client.post(
        "/providers",
        headers={"Authorization": "Bearer testkey"},
        json={"name": "Bad", "base_url": "https://[x", "bearer_token": "k"},
    )
    assert r.status_code == 400, r.text
    assert audit_mod.get_audit_log(action="provider.domain.rejected")


async def test_edit_of_an_unknown_provider_is_404_whatever_the_address(
    client, _providers_file
):
    r = await client.put(
        "/providers/no-such-provider",
        headers={"Authorization": "Bearer testkey"},
        json={"base_url": "https://[x"},
    )
    assert r.status_code == 404, r.text


@pytest.mark.parametrize(
    "allowed",
    [
        ["other:model-v2"],
        ["other-provider:other:model-v3"],
        # A registered provider prefix is routed to it, as chat does.
        ["other-provider:not-listed-yet"],
        [],
    ],
)
async def test_allowed_models_a_provider_knows_are_accepted(
    admin_client, keys_store, listed_providers, allowed
):
    r = await admin_client.post(
        "/admin/keys",
        headers=ADMIN,
        json={"client_id": "billing", "allowed_models": allowed},
    )
    assert r.status_code == 201, r.text
    assert keys_store.load_keys()[0]["allowed_models"] == allowed


# -- A provider address must parse, guard or not (BUG-66) ----------------------

INVALID_ADDRESSES = [
    "https://[x",  # unclosed IPv6 bracket
    "https://[x]/v1",  # bracketed name
    "https://api.acme.test:99999/v1",  # port out of range
    "https://api..acme.test/v1",  # empty label
]


@pytest.mark.parametrize("base_url", INVALID_ADDRESSES)
async def test_unguarded_provider_with_invalid_address_is_refused(
    client, _providers_file, base_url
):
    before = _providers_file.read_text()
    r = await client.post(
        "/providers",
        headers={"Authorization": "Bearer testkey"},
        json={"name": "Bad", "base_url": base_url, "bearer_token": "k"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith(
        "Provider base_url refused: not a valid address"
    )
    assert _providers_file.read_text() == before


@pytest.mark.parametrize("base_url", INVALID_ADDRESSES)
async def test_unguarded_provider_edit_to_invalid_address_is_refused(
    client, _providers_file, base_url
):
    before = _providers_file.read_text()
    r = await client.put(
        "/providers/test-provider",
        headers={"Authorization": "Bearer testkey"},
        json={"base_url": base_url},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith(
        "Provider base_url refused: not a valid address"
    )
    assert _providers_file.read_text() == before
