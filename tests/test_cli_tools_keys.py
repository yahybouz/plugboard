"""The key right to emulated tools on CLI routes (ADR-039, cases 2 and 3).

The admin sets ``allow_cli_tools`` when issuing a key (``POST /admin/keys``);
``rotate_key`` carries it to the new key. Only the boolean ``true`` grants it:
a key without the field, a hand-written ``"true"`` or ``1``, and the admin
(who has no key record) never hold it.
"""

from __future__ import annotations

import pytest

ADMIN = {"Authorization": "Bearer testkey"}


@pytest.fixture
def keys(tmp_path, monkeypatch):
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    return ak


def test_a_new_key_has_no_right_by_default(keys):
    record = keys.create_key("agent-a", "test")
    assert record["allow_cli_tools"] is False
    assert keys.allows_cli_tools(record) is False


def test_the_admin_can_grant_the_right(keys):
    record = keys.create_key("agent-a", "test", allow_cli_tools=True)
    assert record["allow_cli_tools"] is True
    assert keys.allows_cli_tools(keys.get_key_record(record["key"])) is True


@pytest.mark.parametrize("value", [True, False])
def test_rotation_carries_the_right(keys, value):
    old = keys.create_key("agent-a", "test", allow_cli_tools=value)
    new = keys.rotate_key(old["key_id"])
    assert new["allow_cli_tools"] is value
    assert keys.allows_cli_tools(keys.get_key_record(new["key"])) is value


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {"allow_cli_tools": None},
        {"allow_cli_tools": "true"},
        {"allow_cli_tools": 1},
        {"allow_cli_tools": [True]},
    ],
    ids=["admin-no-record", "absent", "null", "string", "one", "list"],
)
def test_only_the_boolean_true_grants_the_right(keys, record):
    assert keys.allows_cli_tools(record) is False


@pytest.mark.asyncio
async def test_post_admin_keys_sets_the_right(client, keys):
    resp = await client.post(
        "/admin/keys",
        headers=ADMIN,
        json={"client_id": "agent-a", "allow_cli_tools": True},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["allow_cli_tools"] is True
    stored = keys.get_key_record(resp.json()["key"])
    assert stored["allow_cli_tools"] is True


@pytest.mark.asyncio
async def test_post_admin_keys_leaves_the_right_off_by_default(client, keys):
    resp = await client.post(
        "/admin/keys", headers=ADMIN, json={"client_id": "agent-b"}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["allow_cli_tools"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", 1, "yes"])
async def test_post_admin_keys_accepts_only_a_json_boolean(client, keys, value):
    resp = await client.post(
        "/admin/keys",
        headers=ADMIN,
        json={"client_id": "agent-c", "allow_cli_tools": value},
    )
    assert resp.status_code == 422, resp.text
    assert keys.load_keys() == []
