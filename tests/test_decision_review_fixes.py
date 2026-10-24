"""Additional F1/F2 regressions; synthetic providers, no model/provider calls."""

import json

import pytest


@pytest.mark.parametrize(
    "resolve", ["_resolve_all_providers", "resolve_provider_and_model"]
)
@pytest.mark.parametrize(
    "name,error",
    [
        ("ollama:kev-0.8b:latest", "decisions"),
        ("openclaw/typesafe--jev", "removed"),
        ("proxy:openclaw/convai--laya-multilingual:latest", "removed"),
        ("typesafe/jev", "decisions"),
        ("proxy:convai/laya-multilingual:latest", "decisions"),
    ],
)
def test_wrapped_decision_names_rejected_by_both_resolvers(
    monkeypatch, resolve, name, error
):
    from plugboard import router

    provider = {"id": "proxy", "default_model": "ordinary-chat", "models": []}
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    with pytest.raises(ValueError, match=error):
        getattr(router, resolve)(name)


@pytest.mark.parametrize(
    "resolve", ["_resolve_all_providers", "resolve_provider_and_model"]
)
@pytest.mark.parametrize(
    "default,error",
    [("openclaw/typesafe--jev", "removed"), ("typesafe/jev", "decisions")],
)
def test_selected_decision_default_cannot_fall_back_to_ordinary_provider(
    monkeypatch, resolve, default, error
):
    from plugboard import router

    providers = [
        {"id": "first", "default_model": default, "models": []},
        {"id": "second", "default_model": "ordinary-chat", "models": []},
    ]
    monkeypatch.setattr(router, "load_providers", lambda: providers)
    with pytest.raises(ValueError, match=error):
        getattr(router, resolve)("")


def test_final_exact_match_target_is_guarded_after_normalization(monkeypatch):
    from plugboard import router

    provider = {"id": "synthetic", "models": [{"id": "kev-0.8b"}]}
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    monkeypatch.setattr(router, "_clean_model_id", lambda name: "synthetic-alias")
    with pytest.raises(ValueError, match="decisions"):
        router.resolve_provider_and_model("synthetic-alias")


@pytest.mark.parametrize("stream", [False, True])
async def test_default_decision_never_opens_chat_transport(monkeypatch, stream):
    from plugboard import router, http_pool

    providers = [
        {"id": "first", "default_model": "kev-0.8b", "models": []},
        {"id": "second", "default_model": "ordinary-chat", "models": []},
    ]
    monkeypatch.setattr(router, "load_providers", lambda: providers)

    async def forbidden_transport():
        pytest.fail("Decision selected as default reached chat transport/fallback")

    monkeypatch.setattr(http_pool, "get_session", forbidden_transport)
    with pytest.raises(ValueError, match="decisions"):
        if stream:
            async for _ in router.forward_stream({"model": "", "messages": []}):
                pytest.fail("No chat stream may be produced")
        else:
            await router.forward({"model": "", "messages": []})


async def test_catalogue_filters_again_after_normalization(monkeypatch):
    from plugboard import router

    provider = {"id": "synthetic", "models": [{"id": "alias"}, {"id": "ordinary-chat"}]}
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    monkeypatch.setattr(
        router,
        "published_model_id",
        lambda owner, native, provider_ids: (
            "kev-0.8b" if native == "alias" else f"{owner}:{native}"
        ),
    )
    monkeypatch.setattr("plugboard.model_exposure.mode", lambda: "all")
    router.invalidate_models_cache()
    try:
        result = await router.list_models()
        assert [m["id"] for m in result] == ["synthetic:ordinary-chat"]
    finally:
        router.invalidate_models_cache()


@pytest.mark.parametrize(
    "name",
    [
        "synthetic:ordinary-chat:latest",
        "openclaw/openai--gpt-4o",
        "kevin",
        "kevlar",
        "synthetic:layer-model",
    ],
)
def test_ordinary_chat_names_remain_available(name):
    from plugboard.decisions import is_decision_model

    assert is_decision_model(name) is False


@pytest.mark.parametrize(("model", "head"), [("laya", 192), ("laya-multilingual", 256)])
def test_laya_head_metadata_uses_verified_pinned_configs(model, head):
    from plugboard.decision_api import _metadata
    from plugboard.decision_assets import laya_config

    cfg = laya_config(model)
    limits = _metadata(model)["limits"]
    assert cfg["head_max_len"] == head
    assert limits["head_tokens"] == head
    assert limits["question_branch_tokens"] == head
    assert limits["api_timeout_seconds"] == 45
    assert limits["timeout_seconds"] == 30
    assert (
        limits["instruction_tokens"] is None
    )  # Head is shared, not an independent instruction allowance.


def test_changed_bundled_laya_config_is_not_reported_as_verified(monkeypatch, tmp_path):
    from plugboard import decision_assets

    (tmp_path / "laya.json").write_text(json.dumps({"head_max_len": 999}))
    monkeypatch.setattr(decision_assets, "LAYA_CONFIGS", tmp_path)
    with pytest.raises(decision_assets.AssetError, match="integrity"):
        decision_assets.laya_config("laya")


@pytest.mark.parametrize(
    "resolve", ["_resolve_all_providers", "resolve_provider_and_model"]
)
def test_empty_explicit_model_cannot_delegate_to_decision_default(monkeypatch, resolve):
    from plugboard import router

    provider = {"id": "synthetic", "default_model": "kev-0.8b", "models": []}
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    with pytest.raises(ValueError, match="decisions"):
        getattr(router, resolve)("synthetic:")


@pytest.mark.parametrize(
    "resolve", ["_resolve_all_providers", "resolve_provider_and_model"]
)
@pytest.mark.parametrize(
    "target,error",
    [("openclaw/typesafe--jev", "removed"), ("typesafe/jev", "decisions")],
)
def test_alias_rewrite_to_wrapped_decision_is_rejected(
    monkeypatch, resolve, target, error
):
    from plugboard import router

    provider = {"id": "synthetic", "default_model": "ordinary-chat", "models": []}
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    monkeypatch.setattr(router, "_resolve_model_alias", lambda name: target)
    with pytest.raises(ValueError, match=error):
        getattr(router, resolve)("ordinary-looking-alias")
