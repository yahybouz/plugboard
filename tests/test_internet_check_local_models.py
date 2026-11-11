"""The bulk internet check can leave local Ollama models out.

A local model has no web access, and probing it makes the host load and run
every installed model in turn. The boot probe asks for ``skip_local``; Ollama
Cloud models (``:cloud`` / ``-cloud`` tags) run remotely and are still probed.
"""

from __future__ import annotations

import pytest

from plugboard import app as app_module

PROVIDERS = [
    {
        "id": "ollama",
        "name": "Ollama",
        "base_url": "http://localhost:11434",
        "provider_type": "sovereign",
    },
    {
        "id": "anthropic-direct",
        "name": "Anthropic Direct",
        "base_url": "https://api.anthropic.com",
        "provider_type": "cloud",
    },
]

MODELS = [
    {"id": "ollama:qwen3:8b", "owned_by": "ollama", "provider": "ollama"},
    {"id": "ollama:gpt-oss:120b-cloud", "owned_by": "ollama", "provider": "ollama"},
    {
        "id": "anthropic-direct:claude-haiku-5-5",
        "owned_by": "anthropic-direct",
        "provider": "anthropic-direct",
    },
]


@pytest.fixture
def probed(monkeypatch):
    calls: list[str] = []

    async def _list_models():
        return [dict(m) for m in MODELS]

    async def _probe(body, _=None):
        calls.append(body["model_id"])
        return {"model_id": body["model_id"], "internet_access": False, "status": "ok"}

    monkeypatch.setattr(app_module, "list_models", _list_models)
    monkeypatch.setattr(app_module, "load_providers", lambda: PROVIDERS)
    monkeypatch.setattr(app_module, "check_model_internet_access", _probe)
    return calls


@pytest.mark.asyncio
async def test_skip_local_leaves_local_ollama_models_out(probed):
    summary = await app_module.check_all_models_internet_access(
        {"skip_local": True}, _=None
    )

    assert sorted(probed) == [
        "anthropic-direct:claude-haiku-5-5",
        "ollama:gpt-oss:120b-cloud",
    ]
    assert summary["total"] == 2


@pytest.mark.asyncio
async def test_on_demand_bulk_check_still_probes_every_model(probed):
    summary = await app_module.check_all_models_internet_access({}, _=None)

    assert sorted(probed) == sorted(m["id"] for m in MODELS)
    assert summary["total"] == 3
