"""Ollama off by configuration (PLU-53, lot A): ``OLLAMA_ENABLED=false``.

Until now the variable only stopped the boot registration: a provider already
in ``providers.json`` stayed active (the UAT case). Now it turns that provider
off in memory, on every surface, and writes nothing: setting the variable back
to ``true`` or ``auto`` brings Ollama back, switch and models untouched.
"""

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
    config_off,  # noqa: F811
):
    provider = config.get_provider("ollama")
    assert provider["is_active"] is False
    assert provider["disabled_by_config"] is True
    stored = json.loads(config.PROVIDERS_FILE.read_text())["providers"]
    assert next(p for p in stored if p["id"] == "ollama")["is_active"] is True


@pytest.mark.parametrize("value", ["auto", "true", ""])
def test_any_other_value_leaves_the_provider_alone(app_client, monkeypatch, value):  # noqa: F811
    monkeypatch.setattr(ollama, "_OLLAMA_ENABLED", value)
    provider = config.get_provider("ollama")
    assert provider["is_active"] is True
    assert "disabled_by_config" not in provider


async def test_every_surface_refuses_and_nothing_is_called(
    app_client,
    mcp_tools,
    config_off,  # noqa: F811
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
    assert "HTTP 503" in out and "provider_disabled" in out

    assert calls == [], "turned off by configuration, nothing is called"


async def test_turning_the_variable_back_on_brings_ollama_back(
    app_client,
    monkeypatch,  # noqa: F811
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
