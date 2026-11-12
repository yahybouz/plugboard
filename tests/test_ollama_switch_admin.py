"""Ollama switch, admin side (PLU-12): background calls, the switch itself.

While Ollama is off, nothing in the background calls it (the boot check and
model sync, the semantic cache's embeddings). The switch is the ``is_active``
field of the ``ollama`` provider, set from GET/PUT /admin/ollama/switch and
audited on every PUT.
"""

from __future__ import annotations

import pytest

from plugboard import ollama, semantic_cache
from tests.test_model_not_found import ACME, _isolated_providers
from tests.test_ollama_switch import OLLAMA


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


async def test_semantic_embed_skips_a_disabled_provider(tmp_path, monkeypatch):
    from plugboard import netguard

    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, ACME])
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
        raise RuntimeError("no embedding call to a disabled provider")

    monkeypatch.setattr(netguard, "client_session", _no_session)
    assert await semantic_cache._embed("a prompt long enough to be embedded") is None
    assert opened == [], "no session is opened for a disabled provider"
