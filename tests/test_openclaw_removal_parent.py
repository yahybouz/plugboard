"""Parent acceptance for approved removal of the optional OpenClaw bridge."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _provider(pid, model="gpt-5"):
    return {
        "id": pid,
        "name": pid,
        "is_active": True,
        "models": [],
        "default_model": model,
        "base_url": "https://synthetic.invalid/v1",
        "auth_source": "",
    }


def test_parent_retired_environment_never_bootstraps_or_writes_provider(
    monkeypatch, tmp_path
):
    from plugboard import config

    writes = []
    monkeypatch.setattr(config, "PROVIDERS_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(config, "_write", lambda data: writes.append(data))
    monkeypatch.setattr(config, "_encrypt_token", lambda token: "synthetic-opaque")
    monkeypatch.setenv("OPENCLAW_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("OPENCLAW_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("OPENCLAW_NAME", "Old bridge")
    assert config.load_providers() == []
    assert writes == []


@pytest.mark.parametrize("legacy", ["openclaw", "openclaw-llm-proxy"])
@pytest.mark.parametrize(
    "resolver", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_parent_explicit_retired_provider_never_redirects(
    monkeypatch, legacy, resolver
):
    from plugboard import router

    monkeypatch.setattr(
        router, "load_providers", lambda: [_provider(legacy), _provider("direct")]
    )
    monkeypatch.setattr(router, "_models_alias_cache", {})
    with pytest.raises(ValueError, match="(?i)removed|unsupported|legacy"):
        getattr(router, resolver)(legacy + ":gpt-5")


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "resolver", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_parent_retired_provider_is_never_a_fallback(monkeypatch, reverse, resolver):
    from plugboard import router

    providers = [_provider("openclaw-llm-proxy"), _provider("direct")]
    if reverse:
        providers.reverse()
    monkeypatch.setattr(router, "load_providers", lambda: providers)
    monkeypatch.setattr(router, "_models_alias_cache", {})
    result = getattr(router, resolver)("")
    candidates = result if resolver == "_resolve_all_providers" else [result]
    assert candidates
    assert all(
        provider["id"] == "direct" and model == "gpt-5"
        for provider, model in candidates
    )


@pytest.mark.parametrize(
    "resolver", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_parent_unrelated_provider_name_keeps_native_id_opaque(monkeypatch, resolver):
    from plugboard import router

    provider = _provider("team-openclaw-notes", "qwen3:kev")
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    monkeypatch.setattr(router, "_models_alias_cache", {})
    result = getattr(router, resolver)("")
    candidates = result if resolver == "_resolve_all_providers" else [result]
    assert candidates == [(provider, "qwen3:kev")]


def test_parent_watchdog_does_not_execute_retired_launcher(monkeypatch, tmp_path):
    from plugboard import provider_watchdog

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "openclaw_launcher.sh").write_text("# synthetic retired launcher\n")
    calls = []
    monkeypatch.setattr(provider_watchdog, "_PROJECT_DIR", tmp_path)
    monkeypatch.setattr(provider_watchdog, "_log_event", lambda *args, **kwargs: None)

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="synthetic", stderr="")

    monkeypatch.setattr(provider_watchdog.subprocess, "run", run)
    result = provider_watchdog.restart_provider("openclaw-llm-proxy")
    assert calls == []
    assert result["success"] is False
