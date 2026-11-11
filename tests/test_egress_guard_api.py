"""The ``egress_guard`` provider field and its API (PLU-7, D-18 c).

A manual provider is protected only when the admin ticks the box
(``egress_guard``); catalogue providers and built-in cloud presets are always
protected (``egress_guard_forced``). Ticking the box requires a public
http(s) ``base_url``: a refusal is audited as ``provider.egress.rejected``.
DNS answers are faked: no test resolves a real name.
"""

from __future__ import annotations

import json

import pytest

from plugboard import audit as audit_mod
from plugboard import config
from plugboard.llm_pricing import UnsafeURL
from tests.factories import PROVIDER_A, PROVIDER_OLLAMA

AUTH = {"Authorization": "Bearer testkey"}


@pytest.fixture
def private(monkeypatch):
    async def _resolve(host, port):
        raise UnsafeURL(f"{host} resolves to a non-public address (10.0.0.5)")

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _resolve)


@pytest.fixture
def public(monkeypatch):
    async def _resolve(host, port):
        return []

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _resolve)


@pytest.fixture
def no_lookup(monkeypatch):
    async def _resolve(host, port):
        pytest.fail(f"unexpected DNS lookup of {host}")

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _resolve)


@pytest.fixture
def audit_db(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")


@pytest.fixture
def guard_providers(_providers_file):
    """providers.json with one provider per kind of guard."""
    providers = [
        {
            **PROVIDER_A,
            "id": "catalog-x",
            "base_url": "https://api.acme.test/v1",
            "preset_key": "catalog-acme",
        },
        {
            **PROVIDER_A,
            "id": "openai",
            "base_url": "https://api.openai.com/v1",
            "preset_key": "openai",
        },
        {**PROVIDER_A, "id": "manual", "base_url": "https://manual.test/v1"},
        {
            **PROVIDER_A,
            "id": "guarded",
            "base_url": "https://guarded.test/v1",
            "egress_guard": True,
        },
        PROVIDER_OLLAMA,
    ]
    _providers_file.write_text(json.dumps({"providers": providers}))
    return _providers_file


def test_create_provider_stores_egress_guard_only_when_set(_empty_providers_file):
    record = config.create_provider(
        name="A", base_url="https://a.test", bearer_token="k"
    )
    assert "egress_guard" not in record
    guarded = config.create_provider(
        name="B", base_url="https://b.test", bearer_token="k", egress_guard=True
    )
    assert guarded["egress_guard"] is True
    assert config.get_provider("b")["egress_guard"] is True


async def test_post_guarded_provider_needs_public_url(client, private, audit_db):
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={
            "name": "Lan",
            "base_url": "http://10.0.0.5:8000",
            "bearer_token": "k",
            "egress_guard": True,
        },
    )
    assert r.status_code == 422
    assert "non-public" in r.json()["detail"]
    assert config.get_provider("lan") is None
    events = audit_mod.get_audit_log(action="provider.egress.rejected")
    assert len(events) == 1
    assert json.loads(events[0]["details"])["base_url"] == "http://10.0.0.5:8000"


async def test_post_guarded_provider_on_public_url(client, public, audit_db):
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={
            "name": "Pub",
            "base_url": "https://pub.test/v1",
            "bearer_token": "k",
            "egress_guard": True,
        },
    )
    assert r.status_code == 201
    assert r.json()["egress_guard"] is True
    assert r.json()["egress_guard_forced"] is False
    assert config.get_provider("pub")["egress_guard"] is True


async def test_post_unguarded_provider_makes_no_lookup(client, no_lookup, audit_db):
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={"name": "Lan2", "base_url": "http://10.0.0.6", "bearer_token": "k"},
    )
    assert r.status_code == 201
    assert r.json()["egress_guard"] is False
    assert "egress_guard" not in config.get_provider("lan2")


async def test_post_guard_on_non_http_url_is_422(client, no_lookup, audit_db):
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={
            "name": "S",
            "base_url": "subprocess://agy",
            "bearer_token": "k",
            "egress_guard": True,
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "The egress guard needs an http(s) base_url"
    assert config.get_provider("s") is None
    assert audit_mod.get_audit_log(action="provider.egress.rejected")


async def test_post_guarded_provider_unknown_host_is_503(client, monkeypatch, audit_db):
    async def _resolve(host, port):
        raise UnsafeURL(f"cannot resolve {host}")

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _resolve)
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={
            "name": "Gone",
            "base_url": "https://gone.test/v1",
            "bearer_token": "k",
            "egress_guard": True,
        },
    )
    assert r.status_code == 503
    assert r.json()["detail"].startswith("Provider base_url host unreachable:")
    assert config.get_provider("gone") is None
    assert audit_mod.get_audit_log(action="provider.egress.rejected")


async def test_get_providers_exposes_effective_guard(client, guard_providers):
    r = await client.get("/providers", headers=AUTH)
    got = {p["id"]: (p["egress_guard"], p["egress_guard_forced"]) for p in r.json()}
    assert got == {
        "catalog-x": (True, True),
        "openai": (True, True),
        "manual": (False, False),
        "guarded": (True, False),
        "ollama": (False, False),
    }
