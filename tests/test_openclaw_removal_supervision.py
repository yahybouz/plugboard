"""Removed connectors are inert at every supervision boundary."""

import json

import pytest


@pytest.fixture(params=["openclaw", "openclaw-llm-proxy", "marked"])
def legacy(request):
    record = {
        "id": request.param,
        "base_url": "http://legacy.invalid/v1",
        "bearer_token": "enc:historical-token",
        "is_active": True,
        "auto_discover": True,
        "models": [{"id": "old-model"}],
    }
    if request.param == "marked":
        record["auth_source"] = "openclaw"
    return record


def forbidden(*args, **kwargs):
    pytest.fail("removed connector reached transport, token handling or persistence")


def test_removed_env_does_not_bootstrap_or_encrypt(tmp_path, monkeypatch):
    from plugboard import config

    path = tmp_path / "providers.json"
    path.write_text('{"providers": []}\n')
    monkeypatch.setattr(config, "PROVIDERS_FILE", path)
    monkeypatch.setattr(config, "PROVIDERS_LOCK", tmp_path / "providers.lock")
    monkeypatch.setenv("OPENCLAW_URL", "http://legacy.invalid")
    monkeypatch.setenv("OPENCLAW_KEY", "unused-secret")
    monkeypatch.setattr(config, "_encrypt_token", forbidden)
    monkeypatch.setattr(config, "_write", forbidden)

    assert config.load_providers() == []
    assert path.read_text() == '{"providers": []}\n'


def test_legacy_store_read_preserves_bytes_without_token_decryption(
    legacy, tmp_path, monkeypatch
):
    from plugboard import config

    path = tmp_path / "providers.json"
    content = json.dumps({"providers": [legacy]})
    path.write_text(content)
    monkeypatch.setattr(config, "PROVIDERS_FILE", path)
    monkeypatch.setattr(config, "PROVIDERS_LOCK", tmp_path / "providers.lock")
    monkeypatch.setattr(config, "_decrypt_token", forbidden)
    monkeypatch.setattr(config, "_write", forbidden)

    assert config.load_providers()[0]["id"] == legacy["id"]
    assert path.read_text() == content


@pytest.mark.parametrize("operation", ["create", "update", "modify"])
def test_legacy_writes_rejected_before_token_or_store_mutation(
    legacy, operation, tmp_path, monkeypatch
):
    from plugboard import config

    path = tmp_path / "providers.json"
    content = json.dumps({"providers": [legacy]})
    path.write_text(content)
    monkeypatch.setattr(config, "PROVIDERS_FILE", path)
    monkeypatch.setattr(config, "PROVIDERS_LOCK", tmp_path / "providers.lock")
    monkeypatch.setattr(config, "_encrypt_token", forbidden)
    monkeypatch.setattr(config, "_write", forbidden)

    with pytest.raises(
        ValueError, match="(?i)(removed|unsupported).*(legacy|connector)"
    ):
        if operation == "create":
            config.create_provider(
                legacy["id"],
                legacy["base_url"],
                "new-secret",
                auth_source=legacy.get("auth_source", ""),
            )
        elif operation == "update":
            config.update_provider(legacy["id"], {"bearer_token": "new-secret"})
        else:
            config.modify_provider(legacy["id"], lambda _: {"models": []})
    assert path.read_text() == content


@pytest.mark.asyncio
async def test_legacy_health_stops_before_auth_and_transport(legacy, monkeypatch):
    from plugboard import health_probe, router

    monkeypatch.setattr(health_probe, "_probe_http", forbidden)
    monkeypatch.setattr(health_probe, "_probe_subprocess", forbidden)
    monkeypatch.setattr(router, "_build_auth_header_async", forbidden)
    result = await health_probe.probe_provider(legacy)
    assert result["ok"] is False
    assert "removed" in result["error"].lower()


@pytest.mark.asyncio
async def test_legacy_watchdog_stops_before_transport(legacy, monkeypatch):
    from plugboard import provider_watchdog as watchdog

    monkeypatch.setattr(watchdog, "get_provider", lambda _: legacy)
    monkeypatch.setattr(watchdog, "_shallow_health", forbidden)
    monkeypatch.setattr(watchdog, "_sovereign_health", forbidden)
    monkeypatch.setattr(watchdog, "_deep_health", forbidden)
    result = await watchdog.ping_provider(legacy["id"], force_deep=True)
    assert result["ok"] is False
    assert "removed" in result["error"].lower()


@pytest.mark.asyncio
async def test_legacy_monitoring_model_stops_before_transport(legacy, monkeypatch):
    from plugboard import monitoring

    monkeypatch.setattr(monitoring, "load_providers", lambda: [legacy])
    monkeypatch.setattr(monitoring.aiohttp, "ClientSession", forbidden)
    result = await monitoring.check_model_availability(
        legacy["id"] + ":old-model", "unused-secret"
    )
    assert result["ok"] is False
    assert "removed" in result["error"].lower()


@pytest.mark.asyncio
async def test_direct_owner_cannot_wrap_removed_monitoring_model(monkeypatch):
    from plugboard import monitoring

    monkeypatch.setattr(monitoring, "load_providers", lambda: [{"id": "direct"}])
    monkeypatch.setattr(monitoring.aiohttp, "ClientSession", forbidden)
    result = await monitoring.check_model_availability(
        "direct:openclaw:old-model", "unused-secret"
    )
    assert result["ok"] is False
    assert "removed" in result["error"].lower()


@pytest.mark.asyncio
async def test_monitoring_honours_longest_legacy_owner_prefix(monkeypatch):
    from plugboard import monitoring

    monkeypatch.setattr(
        monitoring,
        "load_providers",
        lambda: [{"id": "team"}, {"id": "team:bridge", "connector": "openclaw"}],
    )
    monkeypatch.setattr(monitoring.aiohttp, "ClientSession", forbidden)
    result = await monitoring.check_model_availability(
        "team:bridge:old-model", "unused-secret"
    )
    assert result["ok"] is False
    assert "removed" in result["error"].lower()


@pytest.mark.asyncio
async def test_health_default_removed_model_stops_before_auth_or_transport(monkeypatch):
    from plugboard import health_probe, router

    monkeypatch.setattr(health_probe, "_probe_http", forbidden)
    monkeypatch.setattr(router, "_build_auth_header_async", forbidden)
    result = await health_probe.probe_provider(
        {
            "id": "direct",
            "base_url": "http://direct.invalid",
            "default_model": "openclaw/anthropic--old-model",
        }
    )
    assert result["ok"] is False
    assert "removed" in result["error"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "openclaw:old-model",
        "openclaw/anthropic--old-model",
        "openclaw-llm-proxy:old-model",
    ],
)
@pytest.mark.parametrize("boundary", ["watchdog", "subprocess", "ollama"])
async def test_raw_model_probe_rejects_removed_wrappers(model, boundary, monkeypatch):
    from plugboard import cli_subprocess, model_availability, provider_watchdog
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(cli_subprocess, "complete", forbidden)
    if boundary == "watchdog":
        result = await provider_watchdog._deep_health(
            "http://native.invalid", "unused", model
        )
        reason = result.get("error", "")
    elif boundary == "subprocess":
        result = await model_availability.probe_subprocess_model(
            "codex", model, timeout_s=1
        )
        reason = result.get("reason", "")
    else:
        result = await model_availability.probe_ollama_model(
            model, base_url="http://native.invalid", timeout_s=1
        )
        reason = result.get("reason", "")
    assert result["ok"] is False
    assert "removed" in reason.lower()


@pytest.mark.asyncio
async def test_watchdog_rejects_removed_default_model_before_shallow_probe(monkeypatch):
    from plugboard import provider_watchdog as watchdog

    monkeypatch.setattr(
        watchdog,
        "get_provider",
        lambda _: {
            "id": "direct",
            "base_url": "http://direct.invalid",
            "default_model": "openclaw/anthropic--old",
        },
    )
    monkeypatch.setattr(watchdog, "_shallow_health", forbidden)
    result = await watchdog.ping_provider("direct", force_deep=True)
    assert result["ok"] is False
    assert "removed" in result["error"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    ["claude:openclaw", "codex:openclaw", "qwen3:kev", "qwen3:laya", "qwen3:jev"],
)
async def test_native_colon_models_reach_subprocess_unchanged(model, monkeypatch):
    from plugboard import cli_subprocess, model_availability

    received = []

    async def complete(kind, body, *, timeout_s):
        received.append((kind, body["model"], timeout_s))
        return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(cli_subprocess, "complete", complete)
    result = await model_availability.probe_subprocess_model(
        "codex", model, timeout_s=2
    )
    assert result["ok"] is True
    assert received == [("codex", model, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    [
        "discover_openai_models",
        "refresh_native_provider_models",
        "refresh_subprocess_provider_models",
    ],
)
async def test_legacy_catalog_stops_before_auth_or_transport(
    legacy, boundary, monkeypatch
):
    from plugboard import model_availability, router

    monkeypatch.setattr(router, "_build_auth_header_async", forbidden)
    monkeypatch.setattr(router, "discover_models", forbidden)
    with pytest.raises(
        ValueError, match="(?i)(removed|unsupported).*(legacy|connector)"
    ):
        await getattr(model_availability, boundary)(legacy)


def test_unobserved_watchdog_state_is_not_healthy():
    from plugboard.provider_watchdog import _ProviderState

    assert _ProviderState("unknown").is_healthy is False


@pytest.mark.asyncio
async def test_connectivity_marks_legacy_previous_success_offline(
    legacy, tmp_path, monkeypatch
):
    from plugboard import config, model_availability

    legacy["auth_source"] = legacy.get("auth_source") or "subprocess:codex"
    path = tmp_path / "model_status.json"
    key = legacy["id"] + ":old-model"
    path.write_text(
        json.dumps({"models": {key: {"connected": True, "provider": legacy["id"]}}})
    )
    monkeypatch.setattr(config, "load_providers", lambda: [legacy])
    monkeypatch.setattr(model_availability, "_probe_subprocess_provider", forbidden)
    monkeypatch.setattr(model_availability, "_probe_ollama_provider", forbidden)
    result = await model_availability.run_connectivity_probes(status_path=path)
    assert result["models"][key]["connected"] is False
    assert "removed" in result["models"][key]["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auth_source",
    ["subprocess:claude", "subprocess:codex", "subprocess:gemini", "", "cli:claude"],
)
async def test_direct_health_still_uses_native_transport(auth_source, monkeypatch):
    from plugboard import health_probe, router

    calls = []

    async def cli(source):
        calls.append(source)
        return {"ok": True, "version": "synthetic"}

    async def http(url, headers):
        calls.append((url, headers))
        return {"ok": True, "status": 200}

    async def auth(provider):
        return {"Authorization": "Bearer synthetic"}

    monkeypatch.setattr(health_probe, "_probe_subprocess", cli)
    monkeypatch.setattr(health_probe, "_probe_http", http)
    monkeypatch.setattr(router, "_build_auth_header_async", auth)
    result = await health_probe.probe_provider(
        {
            "id": "team-openclaw-notes",
            "base_url": "http://native.invalid/v1",
            "auth_source": auth_source,
        }
    )
    assert result["ok"] is True
    if auth_source.startswith("subprocess:"):
        assert calls == [auth_source]
    else:
        assert calls == [
            (
                "http://native.invalid/v1/health",
                {"Authorization": "Bearer synthetic", "Accept": "application/json"},
            )
        ]


def test_restart_does_not_read_dotenv_or_execute_launcher(tmp_path, monkeypatch):
    from pathlib import Path
    from plugboard import provider_watchdog as watchdog

    (tmp_path / "openclaw_launcher.sh").write_text("#!/bin/sh\nexit 0\n")
    (tmp_path / ".env").write_text("UNUSED_SECRET=test-only\n")
    monkeypatch.setattr(watchdog, "_PROJECT_DIR", tmp_path, raising=False)
    monkeypatch.setattr(watchdog, "get_provider", lambda _: {"id": "direct"})
    monkeypatch.setattr(watchdog, "_log_event", lambda *a, **kw: None)
    monkeypatch.setattr(Path, "read_text", forbidden)
    import subprocess

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = watchdog.restart_provider("direct")
    assert result["success"] is False
    assert "not supported" in result["error"].lower()


def test_missing_legacy_restart_has_explicit_removed_error(monkeypatch):
    from plugboard import provider_watchdog as watchdog

    monkeypatch.setattr(watchdog, "get_provider", lambda _: None)
    monkeypatch.setattr(watchdog, "_log_event", lambda *a, **kw: None)
    result = watchdog.restart_provider("openclaw")
    assert result["success"] is False
    assert "removed" in result["error"].lower()


@pytest.mark.asyncio
async def test_watchdog_legacy_previous_success_becomes_unhealthy(legacy, monkeypatch):
    from plugboard import provider_watchdog as watchdog

    state = watchdog._ProviderState(legacy["id"])
    state.record_healthy()
    monkeypatch.setattr(watchdog, "_states", {legacy["id"]: state})
    monkeypatch.setattr(watchdog, "load_providers", lambda: [legacy])
    monkeypatch.setattr(watchdog, "ping_provider", forbidden)
    monkeypatch.setattr(watchdog, "restart_provider", forbidden)
    monkeypatch.setattr(watchdog, "_log_event", lambda *a, **kw: None)

    async def stop(_):
        watchdog._running = False

    monkeypatch.setattr(watchdog.asyncio, "sleep", stop)
    await watchdog._watchdog_loop()
    assert state.is_healthy is False
    assert state.total_restarts == 0
