"""Parent oracle V8: agy effort variants written by the official price table must not
become Claude CLI models (SPEC/producer V6c §9 « découverte CLI Claude »).

`claude-opus-5-5-medium` / `claude-sonnet-5-5-medium` are Antigravity (agy) effort
variants. The `claude` subprocess discovery harvests every `claude-*` grid key, so
since V6c each pricing refresh would advertise them as Claude Code CLI models.
Offline: grid written in tmp; no CLI, no network.
"""

import pytest

from plugboard import billing, model_catalog

MONTH = "2026_10"


@pytest.fixture(autouse=True)
def _grid(tmp_path, monkeypatch):
    pricing = tmp_path / "pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    monkeypatch.setattr(billing, "current_pricing_month", lambda: MONTH)
    models = {
        name: {"input_price": 1.0, "output_price": 5.0, "source": "official"}
        for name in (
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-opus-5-5-medium",
            "claude-sonnet-5-5-medium",
            "claude-haiku-5-5",
            "gemini-4-argon-high",
            "gemini-3.8-flash-low",
        )
    }
    billing.save_pricing(MONTH, {"models": models})


def test_claude_cli_discovery_excludes_agy_effort_variants():
    found = set(model_catalog.discover_subprocess_models_from_pricing("claude"))
    assert {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"} <= found
    leaked = sorted(m for m in found if m.endswith(("-low", "-medium", "-high")))
    assert leaked == [], leaked


def test_gemini_cli_discovery_unchanged_for_effort_variants():
    found = set(model_catalog.discover_subprocess_models_from_pricing("gemini"))
    assert not {"gemini-4-argon-high", "gemini-3.8-flash-low"} & found


def test_agy_variants_still_priced_for_agy():
    for model in ("claude-opus-5-5-medium", "claude-sonnet-5-5-medium"):
        match = billing.resolve_price(
            model, MONTH, provider={"id": "agy", "auth_source": "subprocess:agy"}
        )
        assert match is not None and match.entry["input_price"] > 0
