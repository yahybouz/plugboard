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


CAPPED_PROVIDERS = [
    {
        "id": "claude-cli",
        "name": "Claude CLI",
        "base_url": "subprocess://claude",
        "auth_source": "subprocess:claude",
    },
    {
        "id": "anthropic-direct",
        "name": "Anthropic Direct",
        "base_url": "https://api.anthropic.com",
        "provider_type": "cloud",
    },
]


@pytest.mark.asyncio
async def test_bulk_check_stays_within_each_provider_cap(monkeypatch):
    """D-43: a CLI provider takes one request at a time. The bulk check runs
    its models one after the other, so it never fills the provider's queue
    with its own probes (which would answer each other "busy"); an uncapped
    provider keeps the bulk concurrency."""
    import asyncio

    models = [
        {"id": f"{pid}:model-{n}", "owned_by": pid, "provider": pid}
        for pid in ("claude-cli", "anthropic-direct")
        for n in (1, 2)
    ]
    running = {"claude-cli": 0, "anthropic-direct": 0}
    peak = dict(running)

    async def _list_models():
        return [dict(m) for m in models]

    async def _probe(body, _=None):
        pid = body["model_id"].split(":", 1)[0]
        running[pid] += 1
        peak[pid] = max(peak[pid], running[pid])
        await asyncio.sleep(0.05)
        running[pid] -= 1
        return {"model_id": body["model_id"], "internet_access": False, "status": "ok"}

    monkeypatch.setattr(app_module, "_BULK_INTERNET_CONCURRENCY", 3)
    monkeypatch.setattr(app_module, "list_models", _list_models)
    monkeypatch.setattr(app_module, "load_providers", lambda: CAPPED_PROVIDERS)
    monkeypatch.setattr(app_module, "check_model_internet_access", _probe)
    summary = await app_module.check_all_models_internet_access({}, _=None)

    assert summary["total"] == 4
    assert peak == {"claude-cli": 1, "anthropic-direct": 2}
