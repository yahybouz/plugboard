"""Ollama switch, admin side (PLU-12): background calls, the switch itself.

While Ollama is off, nothing in the background calls it (the boot check and
model sync, the semantic cache's embeddings). The switch is the ``is_active``
field of the ``ollama`` provider, set from GET/PUT /admin/ollama/switch and
audited on every PUT.
"""

from __future__ import annotations

import json

import pytest

from plugboard import ollama, provider_admission, router, semantic_cache
from tests import test_ollama_switch as switch_tests
from tests.test_model_not_found import ACME, _isolated_providers
from tests.test_ollama_switch import OLLAMA

http = switch_tests.http  # fixture: client key, forward() recorded
off = switch_tests.off  # fixture: Ollama off, two active providers
ADMIN = {"Authorization": "Bearer adminkey"}  # conftest's _admin_key


@pytest.fixture
def daemon(monkeypatch):
    """Fakes of the Ollama daemon calls, recording each one."""
    calls: list = []

    async def _check_available():
        calls.append("check_available")
        return True

    async def _list_models():
        calls.append("list_models")
        return [{"name": "qwen3:8b"}, {"name": "llama3:latest"}]

    monkeypatch.setattr(ollama, "check_available", _check_available)
    monkeypatch.setattr(ollama, "list_models", _list_models)
    return calls


# ── No background call while Ollama is off ──────────────────────────────────


@pytest.mark.parametrize("setting", ["auto", "true"])
async def test_boot_skips_a_disabled_ollama(tmp_path, monkeypatch, daemon, setting):
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, ACME])
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", setting)

    record = await ollama.bootstrap_ollama()

    assert record["id"] == "ollama" and record["is_active"] is False
    assert daemon == [], "no boot check and no model sync while Ollama is off"


async def test_boot_keeps_the_relay_token_current_while_off(
    tmp_path, monkeypatch, daemon
):
    from plugboard.config import get_provider

    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, ACME])
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "auto")
    monkeypatch.setattr(ollama, "_OLLAMA_TOKEN", "relay-token-for-tests")

    await ollama.bootstrap_ollama()

    assert get_provider("ollama")["bearer_token"] == "relay-token-for-tests"
    assert get_provider("ollama")["is_active"] is False
    assert daemon == []


async def test_boot_still_syncs_an_enabled_ollama(tmp_path, monkeypatch, daemon):
    _isolated_providers(tmp_path, monkeypatch, [{**OLLAMA, "is_active": True}, ACME])
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "auto")

    record = await ollama.bootstrap_ollama()

    assert record["id"] == "ollama"
    assert daemon == ["check_available", "list_models"]


@pytest.mark.parametrize(("active", "sessions"), [(False, 0), (True, 1)])
async def test_semantic_embed_skips_a_disabled_provider(
    tmp_path, monkeypatch, active, sessions
):
    """Off: no session is opened. On (the control): one is."""
    from plugboard import netguard

    _isolated_providers(tmp_path, monkeypatch, [{**OLLAMA, "is_active": active}, ACME])
    for name, value in {
        "PLUGBOARD_SEMANTIC_CACHE": "1",
        "PLUGBOARD_SEMANTIC_EMBED": "1",
        "PLUGBOARD_EMBED_PROVIDER_ID": "ollama",
        "PLUGBOARD_EMBED_MODEL": "nomic-embed-text",
    }.items():
        monkeypatch.setenv(name, value)

    opened: list = []

    def _no_session(*args, **_kwargs):
        # _embed swallows any error: record the attempt, the assert is below.
        opened.append(args)
        raise RuntimeError("no network in this test")

    monkeypatch.setattr(netguard, "client_session", _no_session)
    assert await semantic_cache._embed("a prompt long enough to be embedded") is None
    assert len(opened) == sessions


# ── The switch: GET/PUT /admin/ollama/switch ────────────────────────────────


@pytest.fixture
def registry(_providers_file, tmp_path, monkeypatch):
    """Write the provider registry of admin_client; audit kept per test."""
    from plugboard import audit

    monkeypatch.setattr(audit, "_DB_PATH", tmp_path / "audit.db")

    def write(providers: list):
        _providers_file.write_text(json.dumps({"providers": providers}))
        router.invalidate_models_cache()
        return _providers_file

    write([{**OLLAMA, "is_active": True}, ACME])
    return write


def _stored(pfile, provider_id: str = "ollama") -> dict:
    data = json.loads(pfile.read_text())
    return next(p for p in data["providers"] if p["id"] == provider_id)


async def _model_ids(client) -> list:
    resp = await client.get("/v1/models", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return [m["id"] for m in resp.json()["data"]]


async def test_switch_reads_the_state(admin_client, registry, daemon):
    resp = await admin_client.get("/admin/ollama/switch", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "provider_id": "ollama",
        "registered": True,
        "enabled": True,
        "in_flight": 0,
        "waiting": 0,
    }
    assert daemon == [], "reading the switch never calls the daemon"


async def test_switch_off_persists_and_hides_the_models(admin_client, registry, daemon):
    pfile = registry([{**OLLAMA, "is_active": True}, ACME])
    assert "ollama:qwen3:8b" in await _model_ids(admin_client)

    resp = await admin_client.put(
        "/admin/ollama/switch", json={"enabled": False}, headers=ADMIN
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False and resp.json()["registered"] is True
    assert _stored(pfile)["is_active"] is False
    assert not [m for m in await _model_ids(admin_client) if m.startswith("ollama:")]
    assert daemon == []


async def test_switch_on_resyncs_once_and_lists_again(admin_client, registry, daemon):
    registry([OLLAMA, ACME])  # off

    resp = await admin_client.put(
        "/admin/ollama/switch", json={"enabled": True}, headers=ADMIN
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True
    assert daemon == ["list_models"], "one model sync when turned back on"
    assert "ollama:qwen3:8b" in await _model_ids(admin_client)

    await admin_client.put(
        "/admin/ollama/switch", json={"enabled": True}, headers=ADMIN
    )
    assert daemon == ["list_models"], "no sync when it was already on"


async def test_each_switch_is_audited(admin_client, registry, daemon):
    from plugboard import audit

    for enabled in (False, False, True):
        resp = await admin_client.put(
            "/admin/ollama/switch", json={"enabled": enabled}, headers=ADMIN
        )
        assert resp.status_code == 200, resp.text

    entries = audit.get_audit_log(action="ollama.switch")
    assert [(e["target_type"], e["target_id"]) for e in entries] == [
        ("provider", "ollama")
    ] * 3
    assert [json.loads(e["details"]) for e in reversed(entries)] == [
        {"from": True, "to": False, "in_flight": 0, "waiting": 0},
        {"from": False, "to": False, "in_flight": 0, "waiting": 0},
        {"from": False, "to": True, "in_flight": 0, "waiting": 0},
    ]


async def test_switch_reports_calls_in_flight(admin_client, registry, daemon):
    from plugboard import audit

    held = provider_admission.try_acquire({**OLLAMA, "is_active": True})
    try:
        state = (await admin_client.get("/admin/ollama/switch", headers=ADMIN)).json()
        assert state["in_flight"] == 1
        await admin_client.put(
            "/admin/ollama/switch", json={"enabled": False}, headers=ADMIN
        )
    finally:
        held.release()
    (entry,) = audit.get_audit_log(action="ollama.switch")
    assert json.loads(entry["details"])["in_flight"] == 1


async def test_switch_404_when_ollama_is_not_registered(admin_client, registry):
    from plugboard import audit

    registry([ACME])
    state = (await admin_client.get("/admin/ollama/switch", headers=ADMIN)).json()
    assert (state["registered"], state["enabled"]) == (False, False)

    resp = await admin_client.put(
        "/admin/ollama/switch", json={"enabled": True}, headers=ADMIN
    )
    assert resp.status_code == 404, resp.text
    assert "not registered" in resp.json()["detail"]
    assert audit.get_audit_log(action="ollama.switch") == []


@pytest.mark.parametrize(
    "body", [{}, {"enabled": "no"}, {"enabled": 0}, {"enabled": False, "x": 1}]
)
async def test_switch_put_is_strict(admin_client, registry, body):
    pfile = registry([{**OLLAMA, "is_active": True}, ACME])
    resp = await admin_client.put("/admin/ollama/switch", json=body, headers=ADMIN)
    assert resp.status_code == 422, resp.text
    assert _stored(pfile)["is_active"] is True


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_switch_needs_the_admin_key(off, http, method):
    client, auth, _calls = http
    for headers in (auth, {}):
        resp = await client.request(
            method, "/admin/ollama/switch", json={"enabled": True}, headers=headers
        )
        assert resp.status_code in (401, 403), (headers, resp.text)
    assert _stored(off)["is_active"] is False


async def test_provider_update_audit_keeps_the_is_active_value(admin_client, registry):
    from plugboard import audit

    resp = await admin_client.put(
        "/providers/ollama", json={"is_active": False}, headers=ADMIN
    )
    assert resp.status_code == 200, resp.text
    (entry,) = audit.get_audit_log(action="provider.update")
    assert json.loads(entry["details"]) == {"fields": ["is_active"], "is_active": False}


async def test_switch_on_lists_models_installed_while_off(
    admin_client, registry, monkeypatch
):
    async def _list_models():
        return [{"name": "qwen3:8b"}, {"name": "mistral:7b"}]

    monkeypatch.setattr(ollama, "list_models", _list_models)
    registry([OLLAMA, ACME])  # off, lists qwen3:8b and llama3:latest
    await admin_client.put(
        "/admin/ollama/switch", json={"enabled": True}, headers=ADMIN
    )
    ids = await _model_ids(admin_client)
    assert "ollama:mistral:7b" in ids and "ollama:llama3:latest" not in ids


async def test_switch_on_survives_a_failed_model_sync(
    admin_client, registry, monkeypatch
):
    from plugboard import audit

    async def _daemon_down():
        raise OSError("connection refused")

    monkeypatch.setattr(ollama, "list_models", _daemon_down)
    registry([OLLAMA, ACME])
    resp = await admin_client.put(
        "/admin/ollama/switch", json={"enabled": True}, headers=ADMIN
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True
    assert len(audit.get_audit_log(action="ollama.switch")) == 1


async def test_switch_is_audited_before_the_model_sync(
    admin_client, registry, monkeypatch
):
    from plugboard import audit

    seen: list = []

    async def _resync():
        seen.append(len(audit.get_audit_log(action="ollama.switch")))

    monkeypatch.setattr(ollama, "resync_models", _resync)
    registry([OLLAMA, ACME])
    await admin_client.put(
        "/admin/ollama/switch", json={"enabled": True}, headers=ADMIN
    )
    assert seen == [1], "the change is in the audit log before the sync starts"


async def test_unchanged_switch_does_not_rewrite_the_registry(
    admin_client, registry, daemon
):
    pfile = registry([OLLAMA, ACME])
    before = pfile.stat().st_mtime_ns
    await admin_client.put(
        "/admin/ollama/switch", json={"enabled": False}, headers=ADMIN
    )
    assert pfile.stat().st_mtime_ns == before


async def test_switch_reports_calls_waiting(admin_client, registry, daemon):
    import asyncio

    provider = {**OLLAMA, "is_active": True}
    held = provider_admission.try_acquire(provider)  # local Ollama: one at a time
    waiter = asyncio.create_task(provider_admission.wait_acquire([provider], 5))
    try:
        for _ in range(100):
            if provider_admission.waiting("ollama") == 1:
                break
            await asyncio.sleep(0.01)
        state = (await admin_client.get("/admin/ollama/switch", headers=ADMIN)).json()
        assert (state["in_flight"], state["waiting"]) == (1, 1)
    finally:
        held.release()
        lease = await waiter
        lease.release()


async def test_providers_page_turning_ollama_on_resyncs_the_models(
    admin_client, registry, daemon
):
    registry([OLLAMA, ACME])
    resp = await admin_client.put(
        "/providers/ollama", json={"is_active": True}, headers=ADMIN
    )
    assert resp.status_code == 200, resp.text
    assert daemon == ["list_models"], "the same field as the switch: same resync"
    assert "ollama:qwen3:8b" in await _model_ids(admin_client)


async def test_providers_page_other_updates_do_not_resync(
    admin_client, registry, daemon
):
    registry([OLLAMA, ACME])
    await admin_client.put("/providers/ollama", json={"timeout": 30}, headers=ADMIN)
    await admin_client.put(
        "/providers/acme-direct", json={"is_active": True}, headers=ADMIN
    )
    assert daemon == []
