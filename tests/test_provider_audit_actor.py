"""Provider admin calls made by the MCP server are audited as actor ``mcp``.

PB-MCP-02: ``POST /providers`` and ``POST /providers/{id}/discover`` record
the actor ``mcp`` when the request carries ``X-Plugboard-Surface: mcp``, and
for that actor also record their refusals and previews. The dashboard (no
header) is unchanged. DNS answers are faked: no test resolves a real name.
"""

from __future__ import annotations

import json

import pytest

from plugboard import audit as audit_mod
from plugboard.llm_pricing import UnsafeURL

MCP = {"Authorization": "Bearer adminkey", "X-Plugboard-Surface": "mcp"}
DASH = {"Authorization": "Bearer adminkey"}
PROVIDER_KEY = "sk-synthetic-provider-key"


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
def audit_db(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")


@pytest.fixture
def found_m1(monkeypatch):
    async def found(provider):
        return [{"id": "m1"}]

    monkeypatch.setattr("plugboard.app.discover_models", found)


def _entries(action):
    return audit_mod.get_audit_log(action=action)


def _create(name="Acme", base_url="https://api.acme.test/v1", **extra):
    return {"name": name, "base_url": base_url, "bearer_token": "k", **extra}


async def test_create_from_mcp_is_audited_as_mcp(admin_client, audit_db, public):
    body = _create(bearer_token=PROVIDER_KEY, egress_guard=True)
    r = await admin_client.post("/providers", headers=MCP, json=body)
    assert r.status_code == 201
    [entry] = _entries("provider.create")
    assert entry["actor"] == "mcp" and entry["target_id"] == "acme"


async def test_dashboard_create_is_unchanged(admin_client, audit_db, public):
    r = await admin_client.post("/providers", headers=DASH, json=_create())
    assert r.status_code == 201
    assert _entries("provider.create")[0]["actor"] == "admin"


async def test_other_surface_counts_as_admin(admin_client, audit_db, public):
    headers = {**DASH, "X-Plugboard-Surface": "playground"}
    await admin_client.post("/providers", headers=headers, json=_create())
    assert _entries("provider.create")[0]["actor"] == "admin"


async def test_mcp_egress_refusal_is_audited_as_mcp(admin_client, audit_db, private):
    body = _create(name="In", base_url="http://10.0.0.5/v1", egress_guard=True)
    r = await admin_client.post("/providers", headers=MCP, json=body)
    assert r.status_code == 422
    [entry] = _entries("provider.egress.rejected")
    assert entry["actor"] == "mcp"


async def test_mcp_domain_refusal_is_audited_as_mcp(
    admin_client, audit_db, public, monkeypatch
):
    monkeypatch.setenv("PLUGBOARD_PROVIDER_DOMAINS_ALLOWED", "api.acme.test")
    body = _create(name="Out", base_url="https://api.other.test", egress_guard=True)
    r = await admin_client.post("/providers", headers=MCP, json=body)
    assert r.status_code == 400
    assert _entries("provider.domain.rejected")[0]["actor"] == "mcp"
    assert _entries("provider.create.refused") == []  # audited once


async def test_mcp_value_error_refusal_is_audited(admin_client, audit_db, public):
    body = _create(name="OpenClaw", base_url="https://x.test", egress_guard=True)
    r = await admin_client.post("/providers", headers=MCP, json=body)
    assert r.status_code == 400
    [entry] = _entries("provider.create.refused")
    details = json.loads(entry["details"])
    assert entry["actor"] == "mcp" and entry["target_type"] == "provider"
    assert details == {
        "name": "OpenClaw",
        "base_url": "https://x.test",
        "status": 400,
        "reason": r.json()["detail"],
    }


async def test_dashboard_value_error_is_not_audited(admin_client, audit_db, public):
    body = _create(name="OpenClaw", base_url="https://x.test")
    r = await admin_client.post("/providers", headers=DASH, json=body)
    assert r.status_code == 400
    assert _entries("provider.create.refused") == []


async def test_mcp_discover_refusal_preview_and_write_are_audited(
    admin_client, audit_db, found_m1
):
    r = await admin_client.post("/providers/nope/discover", headers=MCP)
    assert r.status_code == 404
    r = await admin_client.post(
        "/providers/test-provider/discover?dry_run=true", headers=MCP
    )
    assert r.status_code == 200 and r.json()["dry_run"] is True
    shown = f"added={len(r.json()['added'])} removed={len(r.json()['removed'])}"
    r = await admin_client.post("/providers/test-provider/discover", headers=MCP)
    assert r.status_code == 200
    written = f"added={len(r.json()['added'])} removed={len(r.json()['removed'])}"

    [refused] = _entries("provider.discover.refused")
    assert refused["actor"] == "mcp" and refused["target_id"] == "nope"
    assert json.loads(refused["details"]) == {
        "status": 404,
        "reason": "Provider 'nope' not found",
    }
    [preview] = _entries("provider.discover.preview")
    assert preview["actor"] == "mcp" and preview["target_id"] == "test-provider"
    assert preview["details"] == shown
    [write] = _entries("provider.discover")
    assert write["actor"] == "mcp" and write["details"] == written


async def test_dashboard_discover_is_unchanged(admin_client, audit_db, found_m1):
    await admin_client.post("/providers/nope/discover", headers=DASH)
    await admin_client.post(
        "/providers/test-provider/discover?dry_run=true", headers=DASH
    )
    await admin_client.post("/providers/test-provider/discover", headers=DASH)
    assert _entries("provider.discover.refused") == []
    assert _entries("provider.discover.preview") == []
    assert [e["actor"] for e in _entries("provider.discover")] == ["admin"]


async def test_mcp_discover_failure_is_audited(admin_client, audit_db, monkeypatch):
    async def nothing(provider):
        return []

    monkeypatch.setattr("plugboard.app.discover_models", nothing)
    r = await admin_client.post("/providers/test-provider/discover", headers=MCP)
    assert r.status_code == 502
    [refused] = _entries("provider.discover.refused")
    assert json.loads(refused["details"])["status"] == 502


@pytest.mark.parametrize(
    "dns,name,allowed,status",
    [
        ("public", "K", "", 201),
        ("private", "K", "", 422),
        ("public", "OpenClaw", "", 400),
        ("public", "K", "api.acme.test", 400),
    ],
)
async def test_provider_key_never_reaches_the_audit(
    admin_client, audit_db, request, monkeypatch, dns, name, allowed, status
):
    request.getfixturevalue(dns)
    if allowed:
        monkeypatch.setenv("PLUGBOARD_PROVIDER_DOMAINS_ALLOWED", allowed)
    body = _create(name=name, base_url="https://k.test", egress_guard=True)
    body["bearer_token"] = PROVIDER_KEY
    r = await admin_client.post("/providers", headers=MCP, json=body)
    assert r.status_code == status
    entries = audit_mod.get_audit_log(limit=500)
    assert entries and all(e["actor"] == "mcp" for e in entries)
    assert PROVIDER_KEY not in json.dumps(entries)
