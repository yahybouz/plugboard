"""Read-only integration review probes, synthetic providers and no transport."""

import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request


def provider(pid="team", default="gpt-5", **extra):
    return {
        "id": pid,
        "name": pid,
        "is_active": True,
        "models": [],
        "default_model": default,
        "base_url": "https://synthetic.invalid/v1",
        "auth_source": "",
        **extra,
    }


@pytest.mark.parametrize(
    "resolver", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_effective_native_alias_named_model_has_opaque_openclaw_tag(
    monkeypatch, resolver
):
    from plugboard import router

    direct = provider(default="claude:openclaw")
    monkeypatch.setattr(router, "load_providers", lambda: [direct])
    monkeypatch.setattr(router, "_models_alias_cache", {})
    result = getattr(router, resolver)("team:")
    actual = result if resolver == "_resolve_all_providers" else [result]
    assert actual == [(direct, "claude:openclaw")]


@pytest.mark.asyncio
async def test_warm_catalogue_drops_custom_id_now_marked_legacy(monkeypatch):
    from plugboard import router

    direct = provider("custom-old", auth_source="openclaw")
    cached = [
        {"id": "custom-old:gpt-5", "owned_by": "custom-old", "provider": "custom-old"}
    ]
    monkeypatch.setattr(router, "load_providers", lambda: [direct])
    monkeypatch.setattr(router, "_models_cache", cached)
    monkeypatch.setattr(router, "_models_cache_ts", router._time.monotonic())
    monkeypatch.setattr(router, "_model_status_mtime_changed", lambda: False)
    assert await router.list_models() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["legacy_default", "legacy_alias", "legacy_only_config"]
)
async def test_cached_response_does_not_bypass_effective_legacy_route(
    monkeypatch, mode
):
    from plugboard import app, cache, router

    direct = provider()
    request_model = "team:"
    aliases = {}
    if mode == "legacy_default":
        direct["default_model"] = "openclaw/openai--gpt-5"
    elif mode == "legacy_alias":
        request_model = "team:gpt-5"
        aliases = {request_model: "team:openclaw/openai--gpt-5"}
    else:
        direct = provider("custom-old", auth_source="openclaw")
        request_model = "gpt-5"
    monkeypatch.setattr(app, "load_providers", lambda: [direct])
    monkeypatch.setattr(router, "load_providers", lambda: [direct])
    monkeypatch.setattr(router, "_models_alias_cache", aliases)
    monkeypatch.setattr(app, "_get_request_key_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cache,
        "get_cached",
        lambda *args, **kwargs: {
            "choices": [{"message": {"content": "STALE_REMOVED_ROUTE"}}]
        },
    )

    async def receive():
        return {
            "type": "http.request",
            "body": json.dumps(
                {
                    "model": request_model,
                    "messages": [{"role": "user", "content": "synthetic"}],
                }
            ).encode(),
        }

    request = Request({"type": "http", "method": "POST", "headers": []}, receive)
    with pytest.raises(HTTPException) as caught:
        await app.chat_completions(request, client_id="admin")
    assert caught.value.status_code == 400
    assert "removed" in str(caught.value.detail).lower()


@pytest.mark.asyncio
async def test_monitoring_longest_owner_marker_rejected_before_transport(monkeypatch):
    from plugboard import monitoring

    legacy = provider("team:bridge", auth_source="openclaw")
    monkeypatch.setattr(monitoring, "load_providers", lambda: [legacy])
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("TRANSPORT_SENTINEL")

    monkeypatch.setattr(monitoring.aiohttp, "ClientSession", forbidden)
    result = await monitoring.check_model_availability("team:bridge:gpt-5", "synthetic")
    assert "removed" in result.get("error", "").lower()
    assert attempts == []


@pytest.mark.asyncio
async def test_health_default_wrapper_refused_before_transport(monkeypatch):
    from plugboard import health_probe

    legacy = provider(default="openclaw/openai--gpt-5")
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("TRANSPORT_SENTINEL")

    monkeypatch.setattr(health_probe.aiohttp, "ClientSession", forbidden)
    result = await health_probe.probe_provider(legacy)
    assert "removed" in result.get("error", "").lower()
    assert attempts == []


def test_watchdog_status_cannot_surface_previously_healthy_legacy(monkeypatch):
    from plugboard import provider_watchdog as watchdog

    state = watchdog._ProviderState("custom-old")
    state.record_healthy()
    monkeypatch.setattr(watchdog, "_states", {"custom-old": state})
    monkeypatch.setattr(
        watchdog,
        "load_providers",
        lambda: [provider("custom-old", auth_source="openclaw")],
    )
    status = watchdog.get_watchdog_status()["providers"]["custom-old"]
    assert status["is_healthy"] is False
    assert status["can_restart"] is False
    assert "removed" in status["error"].lower()
    assert (
        state.is_healthy is True
    )  # only the current view changes, no historical mutation
