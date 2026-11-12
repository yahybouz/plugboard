"""Admin routes refuse inputs the gateway would misread (BUG-60, 61, 62)."""

from __future__ import annotations

import pytest

ADMIN = {"Authorization": "Bearer adminkey"}


@pytest.fixture
def keys_store(monkeypatch, tmp_path):
    import plugboard.apikeys as keys

    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    yield keys
    keys._invalidate_cache()


# -- BUG-60: a client key named "admin" ---------------------------------------


@pytest.mark.parametrize("client_id", ["admin", "Admin", " ADMIN ", "\tadmin\n"])
async def test_key_for_client_named_admin_is_refused(
    admin_client, keys_store, client_id
):
    # The gateway resolves a key to its client_id and treats "admin" as the
    # admin (every in-flight call, no model restriction): never issue one.
    r = await admin_client.post(
        "/admin/keys", headers=ADMIN, json={"client_id": client_id}
    )
    assert r.status_code == 422
    # Same answer as the route's other validation refusals (pydantic).
    errors = r.json()["detail"]
    assert [e["loc"] for e in errors] == [["body", "client_id"]]
    assert errors[0]["type"] == "value_error"
    assert keys_store.load_keys() == []


async def test_key_for_client_containing_admin_is_created(admin_client, keys_store):
    r = await admin_client.post(
        "/admin/keys", headers=ADMIN, json={"client_id": "admin-team"}
    )
    assert r.status_code == 201
    assert r.json()["client_id"] == "admin-team"


# -- BUG-61: a provider name that gives an empty id ---------------------------

EMPTY_ID_NAMES = ["!!!", "  ", "日本語"]


@pytest.mark.parametrize("name", EMPTY_ID_NAMES)
def test_create_provider_refuses_a_name_without_id(_empty_providers_file, name):
    from plugboard import config

    before = _empty_providers_file.read_text()
    with pytest.raises(ValueError, match="letter or digit"):
        config.create_provider(name=name, base_url="https://a.test", bearer_token="k")
    assert _empty_providers_file.read_text() == before


@pytest.mark.parametrize("name", EMPTY_ID_NAMES)
async def test_post_provider_without_id_is_400(client, _providers_file, name):
    from plugboard import config

    before = _providers_file.read_text()
    r = await client.post(
        "/providers",
        headers={"Authorization": "Bearer testkey"},
        json={"name": name, "base_url": "https://a.test", "bearer_token": "k"},
    )
    # Same answer as the route's other refusals from create_provider.
    assert r.status_code == 400
    assert "letter or digit" in r.json()["detail"]
    assert _providers_file.read_text() == before
    assert config.get_provider("") is None
