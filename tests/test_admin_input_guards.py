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
