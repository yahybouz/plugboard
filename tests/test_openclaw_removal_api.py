"""Removed connector admission, including cache and admin bypass paths."""

import importlib

import pytest

from plugboard import cache


@pytest.mark.parametrize(
    "model",
    ["openclaw:gpt-5", "openclaw-llm-proxy:gpt-5", "proxy:openclaw/openai--gpt-5"],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_removed_request_rejected_before_cached_response(
    monkeypatch, model, stream, client
):
    app = importlib.import_module("plugboard.app")
    monkeypatch.setattr(app, "_API_KEY", "testkey")
    monkeypatch.setattr(app, "load_providers", lambda: [{"id": "proxy"}])
    lookups = []

    def cached(*args, **kwargs):
        lookups.append(args)
        return {"choices": [{"message": {"content": "OLD CACHED ANSWER"}}]}

    monkeypatch.setattr(cache, "get_cached", cached)
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
        },
    )
    assert response.status_code == 400
    assert "removed" in response.text.lower()
    assert lookups == []


async def test_admin_legacy_view_is_inactive_without_mutating_record(monkeypatch):
    app = importlib.import_module("plugboard.app")
    record = {
        "id": "custom",
        "type": "openclaw",
        "is_active": True,
        "bearer_token": "synthetic-secret",
    }
    monkeypatch.setattr(app, "load_providers", lambda: [record])
    result = await app.get_providers()
    assert result[0]["is_active"] is False
    assert result[0]["migration_required"] is True
    assert record["is_active"] is True
    assert "synthetic-secret" not in str(result)


@pytest.mark.parametrize(
    "record",
    [
        {"id": "openclaw", "is_active": True, "base_url": "http://synthetic.invalid"},
        {
            "id": "direct",
            "default_model": "openclaw/openai--gpt-5",
            "is_active": True,
            "base_url": "http://synthetic.invalid",
        },
    ],
)
async def test_detailed_health_refuses_legacy_without_transport(monkeypatch, record):
    app = importlib.import_module("plugboard.app")
    import aiohttp

    monkeypatch.setattr(app, "load_providers", lambda: [record])
    attempts = []

    def transport(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("legacy health reached HTTP")

    monkeypatch.setattr(aiohttp, "ClientSession", transport)
    response = await app.health_detailed()
    assert response["ok"] is False
    assert "removed" in str(response["providers"]).lower()
    assert attempts == []


@pytest.mark.parametrize(
    "record",
    [
        {"id": "openclaw"},
        {"id": "custom", "auth_source": "openclaw"},
        {"id": "custom", "provider_type": "openclaw-llm-proxy"},
        {"id": "custom", "type": "openclaw"},
    ],
)
def test_explicit_markers_are_removed(record):
    from plugboard.legacy_connectors import is_removed_provider

    assert is_removed_provider(record)


@pytest.mark.parametrize(
    "model",
    [
        "qwen3:openclaw",
        "org/model:openclaw",
        "qwen3:kev",
        "team-openclaw-notes:qwen3:laya",
    ],
)
def test_native_tags_are_opaque(model):
    from plugboard.legacy_connectors import require_supported_model

    require_supported_model(model, provider_ids=("team-openclaw-notes",))


@pytest.mark.parametrize("native", [None, "qwen3:jev"])
async def test_direct_admin_completion_skips_removed_catalogue_ids(monkeypatch, native):
    from aioresponses import aioresponses, CallbackResult
    from plugboard.app import _test_openai_endpoint

    rows = [{"id": "openclaw/openai--gpt-5"}]
    if native:
        rows.append({"id": native})
    sent = []

    def completion(url, **kwargs):
        sent.append(kwargs["json"]["model"])
        return CallbackResult(status=200, payload={"choices": []})

    with aioresponses() as transport:
        transport.get("http://synthetic.test/v1/models", payload={"data": rows})
        transport.post("http://synthetic.test/v1/chat/completions", callback=completion)
        result = await _test_openai_endpoint("http://synthetic.test/v1", "", True)
    if native:
        assert sent == ["qwen3:jev"]
    else:
        assert result["completion_skipped"] == "removed_legacy_connector"
        assert sent == []
