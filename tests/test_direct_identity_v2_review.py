"""Interaction regressions found by the fresh V2 review."""

import pytest

from plugboard import model_exposure
from tests.test_decision_routing_v5 import observe, provider
from tests.test_decision_routing_v5 import routing as routing


@pytest.mark.parametrize(
    "canonical,generic,native",
    [
        ("chatgpt-via-codex-cli", "openai", "gpt-5"),
        ("claude-via-claude-cli", "anthropic", "claude-sonnet-4-6"),
        ("google-gemini-via-gemini-cli", "google", "gemini-3-pro"),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("mode", ["single", "multiple", "forward", "stream"])
async def test_generic_owner_publication_cannot_borrow_cli_account(
    routing, monkeypatch, canonical, generic, native, reverse, warm, mode
):
    accounts = [
        provider(canonical, native, [native]),
        provider(generic, native, [native]),
    ]
    if reverse:
        accounts.reverse()
    monkeypatch.setattr(routing, "load_providers", lambda: accounts)
    rows = await routing.list_models()
    assert len({row["id"] for row in rows}) == 2
    if not warm:
        routing.invalidate_models_cache()
    for row in rows:
        await observe(routing, monkeypatch, mode, row["id"], (row["owned_by"], native))


@pytest.mark.parametrize("reverse", [False, True])
async def test_curated_catalogue_uses_native_family_for_long_owner(
    routing, monkeypatch, reverse
):
    accounts = [
        provider("claude-via-claude-cli", "claude-opus-4-7", ["claude-opus-4-7"]),
        provider(
            "claude-via-claude-cli:lane", "claude-sonnet-4-6", ["claude-sonnet-4-6"]
        ),
    ]
    if reverse:
        accounts.reverse()
    monkeypatch.setattr(routing, "load_providers", lambda: accounts)
    monkeypatch.setattr(model_exposure, "mode", lambda: "latest")
    monkeypatch.setattr(routing, "_model_filter_mode", lambda: "curated")
    rows = await routing.list_models()
    assert {row["owned_by"] for row in rows} == {
        "claude-via-claude-cli",
        "claude-via-claude-cli:lane",
    }
    for row in rows:
        assert routing.resolve_provider_and_model(row["id"])[0]["id"] == row["owned_by"]
