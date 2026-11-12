"""Admin routes refuse inputs the gateway would misread (BUG-60 to 64)."""

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
    assert [k["client_id"] for k in keys_store.load_keys()] == ["admin-team"]


async def test_existing_key_named_admin_is_left_as_is(admin_client, keys_store):
    # The check is the route's, not create_key's: rotate_key revokes the old
    # key before it calls create_key, and must not stop half-way.
    old = keys_store.create_key("admin", "legacy")
    r = await admin_client.post(
        "/admin/keys", headers=ADMIN, json={"client_id": "admin"}
    )
    assert r.status_code == 422
    assert [k["key_id"] for k in keys_store.load_keys()] == [old["key_id"]]
    r = await admin_client.post(f"/admin/keys/{old['key_id']}/rotate", headers=ADMIN)
    assert r.status_code == 201
    assert keys_store.get_key_record(r.json()["key"])["client_id"] == "admin"


# -- BUG-64: a client key with a blank client_id ------------------------------


@pytest.mark.parametrize("client_id", ["", " ", "\t\n"])
async def test_key_for_blank_client_is_refused(admin_client, keys_store, client_id):
    # The chat routes read an empty client_id as "no caller": /v1/preflight
    # skipped the key's allowed_models, budget and rpm_limit and billed
    # nothing, and so did /v1/chat/completions in permissive mode.
    r = await admin_client.post(
        "/admin/keys", headers=ADMIN, json={"client_id": client_id}
    )
    assert r.status_code == 422
    # Same answer as the refusal of "admin" (BUG-60).
    errors = r.json()["detail"]
    assert [e["loc"] for e in errors] == [["body", "client_id"]]
    assert errors[0]["type"] == "value_error"
    assert keys_store.load_keys() == []


async def test_existing_key_with_blank_client_is_left_as_is(admin_client, keys_store):
    # Like a key named admin: the store is not touched, rotating still works.
    old = keys_store.create_key("", "legacy")
    r = await admin_client.post(f"/admin/keys/{old['key_id']}/rotate", headers=ADMIN)
    assert r.status_code == 201
    assert keys_store.get_key_record(r.json()["key"])["client_id"] == ""


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


# -- BUG-62: a provider address the guard cannot parse ------------------------


@pytest.fixture
def idna_resolver(monkeypatch):
    """socket.getaddrinfo as far as CPython goes before any lookup.

    CPython encodes a str host with the "idna" codec first and raises its
    UnicodeError; any name that gets past it is refused here, so no test
    sends a DNS query.
    """
    import socket

    def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        host.encode("idna")
        raise socket.gaierror(socket.EAI_NONAME, f"unexpected lookup of {host}")

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)


@pytest.fixture
def audit_db(monkeypatch, tmp_path):
    from plugboard import audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")
    return audit_mod


@pytest.mark.parametrize(
    "base_url",
    [
        "https://" + "a" * 64 + ".test/v1",  # IDNA: label too long
        "https://a..test/v1",  # IDNA: empty label
        "https://ex\u2028ample.test/v1",  # IDNA: U+2028 is forbidden
        "https://api.acme.test:99999/v1",  # port out of range
        "https://[api.acme.test/v1",  # unclosed IPv6 bracket
    ],
)
async def test_guarded_provider_with_invalid_address_is_refused_and_audited(
    client, _providers_file, idna_resolver, audit_db, base_url
):
    import json

    before = _providers_file.read_text()
    r = await client.post(
        "/providers",
        headers={"Authorization": "Bearer testkey"},
        json={
            "name": "Bad",
            "base_url": base_url,
            "bearer_token": "k",
            "egress_guard": True,
        },
    )
    # Same refusal as an address that resolves inside the network.
    assert r.status_code == 422
    assert r.json()["detail"].startswith("Provider base_url refused:")
    assert _providers_file.read_text() == before
    [event] = audit_db.get_audit_log(action="provider.egress.rejected")
    assert json.loads(event["details"])["base_url"] == base_url


async def test_guarded_provider_edit_to_invalid_address_is_refused_and_audited(
    client, _providers_file, idna_resolver, audit_db
):
    import json

    from plugboard import config

    providers = json.loads(_providers_file.read_text())["providers"]
    providers[0]["egress_guard"] = True
    _providers_file.write_text(json.dumps({"providers": providers}))
    r = await client.put(
        "/providers/test-provider",
        headers={"Authorization": "Bearer testkey"},
        json={"base_url": "https://a..test/v1"},
    )
    assert r.status_code == 422
    assert r.json()["detail"].startswith("Provider base_url refused:")
    assert config.get_provider("test-provider")["base_url"] == "http://fake-llm.test"
    assert audit_db.get_audit_log(action="provider.egress.rejected")


async def test_catalog_preset_with_invalid_address_is_refused(idna_resolver):
    # The check catalog presets get before a connection test or a creation.
    from fastapi import HTTPException

    from plugboard import app as app_mod

    with pytest.raises(HTTPException) as exc:
        await app_mod._require_public_host("https://a..test/v1")
    assert exc.value.status_code == 422
    assert exc.value.detail.startswith("Catalog preset refused:")


def test_dashboard_reports_a_refused_key_creation():
    # tests/e2e/test_keys.py drives it in a browser (opt-in suite).
    import re
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert re.search(
        r'const res = await api\("POST", "/admin/keys", [^;]*\);\s*'
        r'if \(!res\.ok\) \{ toast\("Failed to create key: " \+ await _errorDetail\(res\)',
        js,
    )


# -- BUG-63: a custom endpoint address that does not parse --------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "https://[x",  # unclosed IPv6 bracket
        "https://[x]/v1",  # bracketed host that is not an IP address
        "https://api.acme.test:99999/v1",  # port out of range
        "https://api.acme.test:abc/v1",  # port that is not a number
    ],
)
async def test_direct_provider_test_with_invalid_address_is_refused(
    admin_client, base_url
):
    from aioresponses import aioresponses

    with aioresponses() as mock:
        r = await admin_client.post(
            "/admin/direct-providers/test",
            headers=ADMIN,
            json={"base_url": base_url, "api_key": "k"},
        )
        assert not mock.requests  # refused before any connection
    # The route's refusal of an unusable base_url: 422 and a detail, not a 500.
    assert r.status_code == 422
    assert r.json()["detail"].startswith("base_url refused: not a valid address")
