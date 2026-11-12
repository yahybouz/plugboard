"""Ollama switch (PLU-12, D-38): a disabled provider is never called (BUG-44).

The switch is the ``is_active`` field of the provider: turned off, a model
only that provider serves (``ollama:qwen3:8b``, or a bare name only it lists)
used to be sent to every other active provider. It now gets 503
``provider_disabled`` and nothing is called, on every surface.
"""

from __future__ import annotations

import pytest

from plugboard import router
from tests.test_model_not_found import ACME, CODEX, OPEN, _isolated_providers

OLLAMA = {
    "id": "ollama",
    "name": "Ollama",
    # Closed port: a test that slipped past the fakes reaches no real Ollama.
    "base_url": "http://127.0.0.1:9",
    "bearer_token": "",
    "default_model": "",
    "provider_type": "sovereign",
    "models": [{"id": "qwen3:8b"}, {"id": "llama3:latest"}],
    "is_active": False,
}
BETA = {
    **ACME,
    "id": "beta-cloud",
    "name": "Beta Cloud",
    "base_url": "http://beta.test",
    "default_model": "beta-1",
    "models": [{"id": "beta-1"}],
    "extra_model_ids": [],
}


@pytest.fixture(autouse=True)
def _no_real_ollama(monkeypatch):
    """Any call to the Ollama daemon fails the test."""
    from plugboard import ollama

    async def _refused(*_args, **_kwargs):
        raise AssertionError("no real Ollama call")

    monkeypatch.setattr(ollama, "check_available", _refused)
    monkeypatch.setattr(ollama, "list_models", _refused)


@pytest.fixture
def off(tmp_path, monkeypatch):
    """Ollama turned off, two active providers with a model list."""
    return _isolated_providers(tmp_path, monkeypatch, [OLLAMA, ACME, BETA])


# ── Which models belong to a disabled provider only ────────────────────────


@pytest.mark.parametrize(
    ("model", "native"),
    [
        ("ollama:qwen3:8b", "qwen3:8b"),
        ("qwen3:8b", "qwen3:8b"),
        ("llama3", "llama3:latest"),  # Ollama reads a bare name as :latest
        (" LLAMA3:latest ", "llama3:latest"),  # any case, surrounding spaces
        ("ollama:not-listed", "not-listed"),  # an explicit prefix is enough
    ],
)
def test_a_model_of_a_disabled_provider_is_recognised(off, model, native):
    provider, got = router.disabled_route(model)
    assert (provider["id"], got) == ("ollama", native)


@pytest.mark.parametrize(
    "model", ["acme-large", "acme-direct:x", "nope:model-x", "made-up", ""]
)
def test_other_models_are_not_disabled(off, model):
    assert router.disabled_route(model) is None


def test_a_model_that_is_not_a_string_is_not_disabled(off):
    assert router.disabled_route(None) is None  # type: ignore[arg-type]


def test_nothing_is_disabled_when_ollama_is_on(tmp_path, monkeypatch):
    _isolated_providers(
        tmp_path, monkeypatch, [{**OLLAMA, "is_active": True}, ACME, BETA]
    )
    assert router.disabled_route("ollama:qwen3:8b") is None
    assert router.disabled_route("qwen3:8b") is None


def test_bare_name_goes_on_when_an_active_provider_has_no_list(tmp_path, monkeypatch):
    """A provider without a model list may serve any name: routed as before."""
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, ACME, OPEN])
    assert router.disabled_route("qwen3:8b") is None
    provider, native = router.disabled_route("ollama:qwen3:8b")
    assert (provider["id"], native) == ("ollama", "qwen3:8b")


def test_longer_active_prefix_wins(tmp_path, monkeypatch):
    gpu = {**OLLAMA, "id": "ollama-gpu", "name": "Ollama GPU", "is_active": True}
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, gpu, ACME])
    assert router.disabled_route("ollama-gpu:qwen3:8b") is None
    assert router.disabled_route("qwen3:8b") is None  # an active provider lists it


def test_the_only_provider_disabled(tmp_path, monkeypatch):
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA])
    for model in ("ollama:qwen3:8b", "qwen3:8b"):
        provider, native = router.disabled_route(model)
        assert (provider["id"], native) == ("ollama", "qwen3:8b")


@pytest.mark.parametrize("model", ["Codex:gpt-6.1-sol", "openai:gpt-6.1-sol"])
def test_a_display_alias_of_a_disabled_cli_is_recognised(tmp_path, monkeypatch, model):
    _isolated_providers(
        tmp_path, monkeypatch, [{**CODEX, "is_active": False}, ACME, BETA]
    )
    provider, native = router.disabled_route(model)
    assert (provider["id"], native) == ("chatgpt-via-codex-cli", "gpt-6.1-sol")


@pytest.mark.parametrize("model", ["Codex:gpt-6.1-sol", "gpt-6.1-sol"])
def test_a_display_alias_of_an_active_cli_is_not_disabled(tmp_path, monkeypatch, model):
    _isolated_providers(tmp_path, monkeypatch, [OLLAMA, CODEX, ACME])
    assert router.disabled_route(model) is None


def test_unknown_model_rule_is_unchanged(off):
    assert router.is_unknown_model("ollama:qwen3:8b") is False
    assert router.is_unknown_model("nope:model-x") is True


def test_provider_disabled_error_names_the_provider():
    exc = router.ProviderDisabledError("ollama")
    assert exc.provider_id == "ollama"
    assert "Provider 'ollama' is turned off" in str(exc)
