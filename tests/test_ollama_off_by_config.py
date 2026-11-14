"""Ollama off by configuration (PLU-53, lot A): ``OLLAMA_ENABLED=false``.

Until now the variable only stopped the boot registration: a provider already
in ``providers.json`` stayed active (the UAT case). Now it turns that provider
off in memory, on every surface, and writes nothing: setting the variable back
to ``true`` or ``auto`` brings Ollama back, switch and models untouched.
"""

# ruff: noqa: F811
# Fixtures imported from the switch tests are used by name as test arguments.
from __future__ import annotations

import json

import pytest

from plugboard import config, ollama
from tests.test_ollama_switch import ADMIN_KEY, OLLAMA, _chat, _preflight  # noqa: F401
from tests.test_ollama_switch_e2e import (  # noqa: F401
    EMBEDDING_MODEL,
    app_client,
    mcp_tools,
)


@pytest.fixture
def config_off(monkeypatch):
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "false")


def test_a_registered_provider_is_turned_off_in_memory_only(
    app_client,
    config_off,
):
    provider = config.get_provider("ollama")
    assert provider["is_active"] is False
    assert provider["disabled_by_config"] is True
    stored = json.loads(config.PROVIDERS_FILE.read_text())["providers"]
    assert next(p for p in stored if p["id"] == "ollama")["is_active"] is True


@pytest.mark.parametrize("value", ["auto", "true", ""])
def test_any_other_value_leaves_the_provider_alone(app_client, monkeypatch, value):
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", value)
    provider = config.get_provider("ollama")
    assert provider["is_active"] is True
    assert "disabled_by_config" not in provider


async def test_every_surface_refuses_and_nothing_is_called(
    app_client,
    mcp_tools,
    config_off,
):
    client, auth, _key, calls = app_client

    ids = [
        m["id"] for m in (await client.get("/v1/models", headers=auth)).json()["data"]
    ]
    assert ids and not [m for m in ids if m.startswith("ollama:")]
    v2 = (await client.get("/v2/models", headers=auth)).json()["data"]
    assert not [m for m in v2 if m["id"].startswith("ollama:")]

    for model in ("ollama:qwen3:8b", "qwen3:8b"):
        for stream in (False, True):
            resp = await client.post(
                "/v1/chat/completions", headers=auth, json=_chat(model, stream=stream)
            )
            assert resp.status_code == 503, (model, stream, resp.text)
            body = resp.json()
            assert body["code"] == "provider_disabled"
            assert "OLLAMA_ENABLED" in body["detail"]

    playground = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "X-Plugboard-Surface": "playground",
        },
        json=_chat("ollama:qwen3:8b"),
    )
    assert playground.status_code == 503
    assert playground.json()["code"] == "provider_disabled"

    assert (await _preflight(client, auth, "ollama", "qwen3:8b"))[
        "error"
    ] == "provider_disabled"

    listed = await mcp_tools.list_models()
    assert "acme-direct:" in listed and "ollama:" not in listed
    out = await mcp_tools.chat(model="ollama:qwen3:8b", message="hi")
    # The MCP tool gives the API's code and detail (lot B), not "HTTP 503".
    assert out.startswith("Error: provider_disabled: ") and "OLLAMA_ENABLED" in out

    assert calls == [], "turned off by configuration, nothing is called"


async def test_turning_the_variable_back_on_brings_ollama_back(
    app_client,
    monkeypatch,
):
    client, auth, _key, calls = app_client
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "false")
    ids = [
        m["id"] for m in (await client.get("/v1/models", headers=auth)).json()["data"]
    ]
    assert "ollama:qwen3:8b" not in ids
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", "auto")
    ids = [
        m["id"] for m in (await client.get("/v1/models", headers=auth)).json()["data"]
    ]
    assert "ollama:qwen3:8b" in ids
    resp = await client.post(
        "/v1/chat/completions", headers=auth, json=_chat("ollama:qwen3:8b")
    )
    assert resp.status_code == 200, resp.text
    assert calls == ["ollama:qwen3:8b"]


# ── No Ollama registered at all (fresh install, or a registry cleaned up) ───


@pytest.fixture
async def bare(tmp_path, monkeypatch, config_off):
    """Ollama off by configuration and absent from the registry."""
    import httpx

    import plugboard.app as app_module
    import plugboard.ratelimit as rl
    from plugboard import apikeys, audit, embeddings_api, usage
    from tests.factories import FAKE_COMPLETION
    from tests.test_model_not_found import ACME, _isolated_providers
    from tests.test_ollama_switch import BETA

    _isolated_providers(tmp_path, monkeypatch, [ACME, BETA])
    monkeypatch.delenv("PLUGBOARD_EMBEDDING_MODELS", raising=False)
    monkeypatch.setattr(app_module, "_API_KEY", "instance-key")
    monkeypatch.setattr(app_module, "_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setattr(rl, "_LIMIT", 0)
    monkeypatch.setattr(apikeys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(apikeys, "KEYS_LOCK", tmp_path / "keys.lock")
    monkeypatch.setattr(audit, "_DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    key = apikeys.create_key("acme", "bare")["key"]
    calls: list = []

    async def _forward(body, **_kwargs):
        calls.append(body.get("model"))
        return dict(FAKE_COMPLETION)

    async def _forward_stream(body, **_kwargs):
        calls.append("stream:" + str(body.get("model")))
        raise RuntimeError("streams are not served in this test")
        yield b""  # pragma: no cover

    async def _engine(*_args, **_kwargs):
        calls.append("embeddings._call")
        raise embeddings_api.EngineError(502, "fake engine", "test")

    monkeypatch.setattr(app_module, "forward", _forward)
    monkeypatch.setattr(app_module, "forward_stream", _forward_stream)
    monkeypatch.setattr(embeddings_api, "_call", _engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        yield client, {"Authorization": f"Bearer {key}"}, calls


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
async def test_chat_to_ollama_is_refused_clearly_without_a_provider(bare, path, stream):
    client, auth, calls = bare
    resp = await client.post(
        path, headers=auth, json=_chat("ollama:qwen3:8b", stream=stream)
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "provider_disabled"
    assert "OLLAMA_ENABLED=false" in body["detail"]
    assert calls == []


async def test_models_list_has_no_ollama_without_a_provider(bare):
    client, auth, _calls = bare
    ids = [
        m["id"] for m in (await client.get("/v1/models", headers=auth)).json()["data"]
    ]
    assert ids and not [m for m in ids if m.startswith("ollama:")]


async def test_embeddings_say_there_is_no_provider_when_none_is_configured(bare):
    client, auth, calls = bare
    resp = await client.post(
        "/v1/embeddings", headers=auth, json={"model": EMBEDDING_MODEL, "input": "hi"}
    )
    assert resp.status_code == 404, resp.text
    assert "no embedding provider is configured" in resp.json()["detail"].lower()
    listed = (await client.get("/v1/embeddings/models", headers=auth)).json()
    assert listed["data"] == []
    assert calls == []


async def test_embeddings_name_the_configuration_when_ollama_is_off(
    app_client,
    config_off,
    monkeypatch,
):
    client, auth, _key, calls = app_client
    resp = await client.post(
        "/v1/embeddings", headers=auth, json={"model": EMBEDDING_MODEL, "input": "hi"}
    )
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail.startswith("Embedding provider unavailable")
    assert "OLLAMA_ENABLED=false" in detail
    assert calls == []


# ── Background work: nothing reaches Ollama, nothing logs an error ──────────


@pytest.fixture
def no_daemon(monkeypatch):
    """Any call to the daemon, the relay or ollama.com fails the test."""
    calls: list = []

    async def _refused(*_args, **_kwargs):
        calls.append("daemon")
        raise AssertionError("Ollama is off: no call is allowed")

    for name in ("check_available", "list_models", "discover_cloud_whitelisted_ids"):
        monkeypatch.setattr(ollama, name, _refused)
    return calls


async def test_the_boot_registers_and_syncs_nothing(app_client, config_off, no_daemon):
    assert await ollama.bootstrap_ollama() is None
    assert no_daemon == []


async def test_the_boot_with_no_provider_registered_calls_nothing(
    bare, no_daemon, caplog
):
    assert await ollama.bootstrap_ollama() is None
    assert config.get_provider("ollama") is None
    assert no_daemon == []
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


async def test_the_watchdog_does_not_ping_ollama(
    app_client, config_off, monkeypatch, caplog
):
    import asyncio
    from types import SimpleNamespace

    from plugboard import provider_watchdog

    pinged: list = []

    async def _ping(provider_id, force_deep=False):
        pinged.append(provider_id)
        return {"ok": True, "check": "shallow"}

    async def _stop(_seconds):
        provider_watchdog._running = False

    monkeypatch.setattr(provider_watchdog, "ping_provider", _ping)
    monkeypatch.setattr(
        provider_watchdog, "asyncio", SimpleNamespace(sleep=_stop), raising=False
    )
    monkeypatch.setattr(provider_watchdog, "_running", True)
    await provider_watchdog._watchdog_loop()
    assert "ollama" not in pinged
    assert pinged, "the other providers are still watched"
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert asyncio  # the real module is untouched for the other tests


async def test_the_connectivity_probes_skip_ollama(
    app_client, config_off, monkeypatch, no_daemon
):
    from plugboard import model_availability

    probed: list = []

    async def _probe(provider, *_a, **_kw):
        probed.append(provider.get("id"))
        return {}

    monkeypatch.setattr(model_availability, "probe_ollama_provider", _probe)
    await model_availability.run_connectivity_probes(probe_kinds={"ollama"})
    assert probed == []
    assert no_daemon == []


async def test_the_price_job_does_not_read_ollama_com(
    app_client, config_off, monkeypatch
):
    import aiohttp

    from plugboard import pricing_scraper

    def _no_session(*_a, **_kw):
        raise AssertionError("Ollama is off: ollama.com must not be read")

    monkeypatch.setattr(aiohttp, "ClientSession", _no_session)
    assert pricing_scraper._ollama_configured() is False
    assert await pricing_scraper._fetch_ollama_pricing() == {}


async def test_ready_stays_200_and_reports_ollama_as_not_configured(
    app_client, config_off, monkeypatch, no_daemon
):
    from plugboard import readiness

    readiness.reset_cache()
    client, _auth, _key, _calls = app_client
    resp = await client.get("/health/ready")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["ollama"] == "not_configured"
    assert no_daemon == []
    readiness.reset_cache()


# ── Review T1: nothing is written, the daemon stays untouched ───────────────


async def test_resync_does_not_call_the_daemon_when_off_by_config(
    app_client, config_off, no_daemon
):
    await ollama.resync_models()
    assert no_daemon == []


async def test_the_switch_and_the_edit_route_do_not_reach_the_daemon(
    app_client, config_off, no_daemon
):
    client, _auth, _key, _calls = app_client
    admin = {"Authorization": f"Bearer {ADMIN_KEY}"}
    resp = await client.put(
        "/admin/ollama/switch", json={"enabled": True}, headers=admin
    )
    assert resp.status_code in (200, 409), resp.text
    resp = await client.put(
        "/providers/ollama", json={"is_active": True}, headers=admin
    )
    assert resp.status_code < 500, resp.text
    assert no_daemon == []


def test_the_edit_form_sends_back_the_stored_switch(app_client, config_off):
    from pathlib import Path

    provider = config.get_provider("ollama")
    assert provider["stored_is_active"] is True
    source = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    assert "p.disabled_by_config === true" in source
