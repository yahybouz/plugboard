"""C2 (2026-06-01) — Ollama Cloud catalog auto-sync: discover whitelisted-family
cloud models from the public catalogue so new versions (e.g. minimax-m3) surface
without a manual `ollama pull`."""

from __future__ import annotations

import pytest

from plugboard.ollama import (
    _to_cloud_id,
    cloud_autosync_enabled,
    cloud_ids_for_whitelist,
)


@pytest.mark.parametrize(
    "catalog_name,expected",
    [
        ("minimax-m3", "minimax-m3:cloud"),  # untagged → :cloud
        ("glm-5.1", "glm-5.1:cloud"),
        ("deepseek-v4-pro", "deepseek-v4-pro:cloud"),
        ("nemotron-3-super", "nemotron-3-super:cloud"),
        ("qwen3.5:397b", "qwen3.5:397b-cloud"),  # tagged → -cloud
        ("gpt-oss:120b", "gpt-oss:120b-cloud"),
        ("qwen3-vl:235b", "qwen3-vl:235b-cloud"),
        ("minimax-m3:cloud", "minimax-m3:cloud"),  # already cloud → unchanged
        ("qwen3.5:397b-cloud", "qwen3.5:397b-cloud"),
    ],
)
def test_to_cloud_id_matches_real_naming(catalog_name, expected):
    assert _to_cloud_id(catalog_name) == expected


def test_whitelist_filters_to_known_families():
    catalog = [
        "minimax-m3",
        "minimax-m2.7",
        "glm-5.1",
        "deepseek-v4-pro",
        "kimi-k2.6",
        "qwen3.5:397b",
        "gpt-oss:120b",
        "nemotron-3-super",
        "gemma3:27b",
        # NOT whitelisted families:
        "ministral-3:8b",
        "llama4:70b",
        "phi-5:14b",
    ]
    ids = cloud_ids_for_whitelist(catalog)
    assert "minimax-m3:cloud" in ids  # the new version surfaces
    assert "qwen3.5:397b-cloud" in ids
    assert "gpt-oss:120b-cloud" in ids
    assert "gemma3:27b-cloud" in ids
    # non-whitelisted families excluded
    assert not any("ministral" in x or "llama4" in x or "phi-5" in x for x in ids)


def test_whitelist_dedups():
    ids = cloud_ids_for_whitelist(["minimax-m3", "minimax-m3", "minimax-m3:cloud"])
    assert ids == ["minimax-m3:cloud"]


def test_model_denylist_keeps_removed_model_out(monkeypatch):
    """C2 BLOCKER-2: an admin-removed model stays out via PLUGBOARD_MODEL_DENYLIST
    even though auto-sync would re-surface it from the upstream catalogue."""
    from plugboard.router import _model_denylist

    monkeypatch.delenv("PLUGBOARD_MODEL_DENYLIST", raising=False)
    assert "claude-opus-4" in _model_denylist()  # static stays
    assert "minimax-m2.5:cloud" not in _model_denylist()

    monkeypatch.setenv("PLUGBOARD_MODEL_DENYLIST", "minimax-m2.5:cloud, glm-4.6:cloud")
    dl = _model_denylist()
    assert "minimax-m2.5:cloud" in dl and "glm-4.6:cloud" in dl
    assert "claude-opus-4" in dl  # static + env merged


def test_router_ollama_whitelist_uses_shared_families():
    """P3.1: the router ollama whitelist and C2 discovery share ONE family list."""
    from plugboard.router import _is_whitelisted_model

    assert _is_whitelisted_model({"id": "ollama:minimax-m3:cloud"}, None) is True
    assert "minimax-m3:cloud" in cloud_ids_for_whitelist(["minimax-m3"])
    # a non-family cloud model is rejected by BOTH
    assert _is_whitelisted_model({"id": "ollama:llama4:70b-cloud"}, None) is False
    assert cloud_ids_for_whitelist(["llama4:70b"]) == []


@pytest.mark.asyncio
async def test_refresh_merges_cloud_catalog(monkeypatch):
    """C2 integration (P3.3): refresh_native_provider_models for ollama merges the
    cloud catalogue into the local list (autosync enabled for this case)."""
    from plugboard import model_availability as ma
    from plugboard import model_quarantine as mq
    from plugboard import ollama as omod
    from plugboard import router

    monkeypatch.setenv("PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC", "1")

    async def fake_discover(_p):
        return [{"id": "localonly:latest", "name": "localonly:latest"}]

    async def fake_cloud():
        return ["minimax-m3:cloud", "glm-5.1:cloud"]

    monkeypatch.setattr(router, "discover_models", fake_discover)
    monkeypatch.setattr(omod, "discover_cloud_whitelisted_ids", fake_cloud)
    # Isolate the merge under test from environmental filter state. The
    # quarantine list (data/quarantined_models.json) and the denylist are
    # mutated at runtime by probe runs, so reading the real ones makes this
    # test flaky (a probe that quarantines minimax-m3:cloud would silently
    # filter it out of the merge). The P3.2 filter behaviour is covered by
    # test_model_denylist_keeps_removed_model_out; here we test ONLY the union.
    monkeypatch.setattr(mq, "get_quarantined_suffixes", lambda *a, **k: set())
    monkeypatch.setattr(router, "_model_denylist", lambda: set())

    prov = {
        "id": "ollama",
        "name": "Ollama",
        "provider_type": "sovereign",
        "base_url": "http://localhost:11434",
        "default_model": "localonly:latest",
        "models": [],
    }
    rep = await ma.refresh_native_provider_models(prov)
    ids = [m["id"] for m in rep["models"]]
    assert "minimax-m3:cloud" in ids and "glm-5.1:cloud" in ids
    assert "localonly:latest" in ids


def test_autosync_gate(monkeypatch):
    monkeypatch.delenv("PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC", raising=False)
    assert cloud_autosync_enabled() is True  # default ON
    monkeypatch.setenv("PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC", "0")
    assert cloud_autosync_enabled() is False
    monkeypatch.setenv("PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC", "false")
    assert cloud_autosync_enabled() is False
