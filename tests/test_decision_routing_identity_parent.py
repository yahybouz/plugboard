"""Parent repros: direct identity and owner survive a real warm catalogue."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "model", ["qwen3:kev", "qwen3:laya", "qwen3:jev", "gpt-5:laya"]
)
def test_parent_direct_preserves_ordinary_model_and_tag(monkeypatch, model):
    from plugboard import router

    provider = {
        "id": "direct",
        "name": "Synthetic direct",
        "is_active": True,
        "default_model": model,
        "models": [],
        "base_url": "https://synthetic.invalid/v1",
    }
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    monkeypatch.setattr(router, "_models_alias_cache", {})
    selected, upstream = router.resolve_provider_and_model(model)
    assert selected["id"] == "direct"
    assert upstream == model


@pytest.mark.parametrize(
    "model",
    ["laya:latest", "jev:latest", "kev-0.8b:latest", "convaiinnovations/laya:latest"],
)
@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
def test_parent_direct_decision_default_is_refused_before_rewriting(
    monkeypatch, model, resolve
):
    from plugboard import router

    provider = {
        "id": "direct",
        "name": "Synthetic direct",
        "is_active": True,
        "default_model": model,
        "models": [],
        "base_url": "https://synthetic.invalid/v1",
    }
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    monkeypatch.setattr(router, "_models_alias_cache", {})
    with pytest.raises(ValueError, match="decision"):
        getattr(router, resolve)("")


@pytest.mark.parametrize(
    "canonical,homonym,stem",
    [
        ("chatgpt-via-codex-cli", "Codex", "gpt-5"),
        ("google-gemini-via-gemini-cli", "Gemini", "gemini-2.5-pro"),
        ("claude-via-claude-cli", "claude-code", "claude-sonnet-4-5"),
    ],
)
@pytest.mark.parametrize(
    "resolve", ["resolve_provider_and_model", "_resolve_all_providers"]
)
async def test_parent_real_warm_catalogue_never_reassigns_explicit_provider(
    monkeypatch, canonical, homonym, stem, resolve
):
    from plugboard import router, model_exposure

    dated = stem + "-20260101"
    providers = [
        {
            "id": canonical,
            "name": "Synthetic canonical",
            "is_active": True,
            "default_model": dated,
            "models": [{"id": dated}],
            "base_url": "https://synthetic.invalid/v1",
        },
        {
            "id": homonym,
            "name": "Synthetic homonym",
            "is_active": True,
            "default_model": "qwen3:kev",
            "models": [{"id": "qwen3:kev"}],
            "base_url": "https://synthetic.invalid/v1",
        },
    ]
    monkeypatch.setattr(router, "load_providers", lambda: providers)
    monkeypatch.setattr(router, "_models_alias_cache", {})
    monkeypatch.setattr(router, "_models_cache", None)
    monkeypatch.setattr(router, "_models_cache_ts", 0)
    monkeypatch.setattr(router, "_model_filter_mode", lambda: "all")
    monkeypatch.setattr(model_exposure, "mode", lambda: "all")
    request = canonical + ":" + stem
    cold = getattr(router, resolve)(request)
    cold_selected = cold[0] if resolve == "_resolve_all_providers" else cold
    assert cold_selected[0]["id"] == canonical
    await router.list_models()
    assert request in router._models_alias_cache, (
        "The real catalogue must actually generate the alias"
    )
    warm = getattr(router, resolve)(request)
    warm_selected = warm[0] if resolve == "_resolve_all_providers" else warm
    assert warm_selected[0]["id"] == canonical
    assert warm_selected[1] == dated
