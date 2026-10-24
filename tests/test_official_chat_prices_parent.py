"""Parent oracles: official vendor prices for previously unpriced chat models.

User decision (registre #112): apply official tariffs. Prices checked 2026-10-24
on the vendors' official pages (platform.claude.com/docs/en/about-claude/pricing,
ai.google.dev/gemini-api/docs/pricing, developers.openai.com/api/docs/pricing).
Data file: scratch official-prices-2026-10-24.json. Offline, built-in table only.
"""

import pytest

from plugboard import billing
from plugboard.direct_providers import PRESETS

MONTH = "2026_10"
OFFICIAL = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),  # standard tier, prompts <= 100k tokens
    "claude-fable-5-1": (10.0, 50.0),
    "gemini-4-argon-low": (2.0, 10.0),
    "gemini-4-argon-medium": (2.0, 10.0),
    "gemini-4-argon-high": (2.0, 10.0),
    "gemini-3.8-flash-low": (0.75, 3.75),
    "gemini-3.8-flash-medium": (0.75, 3.75),
    "gemini-3.8-flash-high": (0.75, 3.75),
    "claude-opus-5-5-medium": (4.0, 20.0),
    "claude-sonnet-5-5-medium": (2.0, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
}
NO_OFFICIAL_PRICE = {"gpt-oss-120b-medium", "meta-llama-3.1-405b-instruct"}


@pytest.fixture(autouse=True)
def _empty_grid(tmp_path, monkeypatch):
    pricing = tmp_path / "pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)


def _cases():
    for key, preset in sorted(PRESETS.items()):
        src = str(preset.get("auth_source") or "")
        if not src.startswith(("subprocess:", "broker:", "cli:")):
            continue
        prov = {
            "id": key,
            "preset_key": key,
            "auth_source": src,
            "base_url": preset.get("base_url", ""),
        }
        for model in preset.get("known_models") or [preset.get("default_model", "")]:
            if model in OFFICIAL or model in NO_OFFICIAL_PRICE:
                yield pytest.param(prov, model, id=f"{key}:{model}")


CASES = list(_cases())


def test_every_target_model_is_exercised():
    seen = {case.values[1] for case in CASES}
    assert set(OFFICIAL) | NO_OFFICIAL_PRICE <= seen, sorted(
        set(OFFICIAL) | NO_OFFICIAL_PRICE - seen
    )


@pytest.mark.parametrize(("provider", "model"), CASES)
def test_official_price_or_explicit_unknown(provider, model):
    match = billing.resolve_price(model, MONTH, provider=provider)
    if model in NO_OFFICIAL_PRICE:
        assert (
            match is None
        )  # no official price: stays unknown, budgeted keys refused 402
        return
    assert match is not None, f"{model} still unpriced"
    expected_in, expected_out = OFFICIAL[model]
    assert match.entry["input_price"] == pytest.approx(expected_in)
    assert match.entry["output_price"] == pytest.approx(expected_out)


@pytest.mark.parametrize("model", ["llama3", "qwen3:8b"])
def test_local_zero_entries_do_not_apply_to_non_ollama_hosts(model):
    custom = {"id": "my-gateway", "base_url": "http://llm.internal.test/v1"}
    match = billing.resolve_price(model, MONTH, provider=custom)
    free = (
        match is not None
        and not match.entry.get("input_price")
        and not match.entry.get("output_price")
    )
    assert not free, f"{model} billed free on a non-Ollama host"


@pytest.mark.parametrize("model", ["llama3", "qwen3:8b"])
def test_local_ollama_stays_free(model):
    ollama = {"id": "ollama", "base_url": "http://localhost:11434"}
    match = billing.resolve_price(model, MONTH, provider=ollama)
    assert match is not None
    assert not match.entry.get("input_price") and not match.entry.get("output_price")


def test_known_api_prices_unchanged():
    match = billing.resolve_price(
        "claude-haiku-4-5", MONTH, provider="anthropic-direct"
    )
    assert match is not None and match.entry["input_price"] > 0
