"""V8: agy effort variants in the price grid stay out of the Claude CLI catalogue.

The official price table writes ``claude-opus-5-5-medium`` / ``claude-sonnet-5-5-medium``
(Antigravity effort variants) into the grid. Pricing-driven discovery of the
``claude`` CLI must not harvest them, nor anything fed from it (CLI sync, the
availability refresh, ``/v1/models``). Offline: grid written in tmp, no CLI.
"""

import pytest

from plugboard import billing, model_catalog, router
from plugboard import model_availability as ma
from plugboard.direct_providers import PRESETS

MONTH = "2026_10"
EFFORT = ("-low", "-medium", "-high")

GRID = (
    # Claude CLI models, including a dated snapshot and a -latest alias.
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-opus-4-7-20260416",
    "claude-3-5-haiku-latest",
    # agy effort variants: the two the official table writes, plus -low/-high.
    "claude-opus-5-5-medium",
    "claude-sonnet-5-5-medium",
    "claude-opus-5-5-low",
    "claude-haiku-5-5-high",
    # Vendor-namespaced, regional and Bedrock copies.
    "azure_ai/claude-opus-5-5",
    "us.anthropic.claude-opus-5-5",
    "claude-sonnet-4-5-20250929-v1:0",
    # Codex and Gemini rows.
    "gpt-5.5",
    "gpt-5.4-pro",
    "o3",
    "gpt-4.1",
    "gemini-3.1-pro-preview",
    "gemini-2.5-flash",
    "gemini-4-argon-high",
    "gemini-3.8-flash-low",
)


@pytest.fixture(autouse=True)
def _grid(tmp_path, monkeypatch):
    pricing = tmp_path / "pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    monkeypatch.setattr(billing, "current_pricing_month", lambda: MONTH)
    rows = {name: {"input_price": 1.0, "output_price": 5.0} for name in GRID}
    billing.save_pricing(MONTH, {"models": rows})


def _claude_provider():
    return {
        "id": "claude-via-claude-cli",
        "name": "Claude (via claude CLI)",
        "base_url": "subprocess://claude",
        "auth_source": "subprocess:claude",
        "provider_type": "cloud",
        "default_model": "claude-sonnet-5-5",
        "models": [],
        "is_active": True,
    }


def test_claude_discovery_keeps_only_cli_models():
    assert model_catalog.discover_subprocess_models_from_pricing("claude") == [
        "claude-3-5-haiku-latest",
        "claude-haiku-5-5",
        "claude-opus-4-7-20260416",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]


def test_codex_and_gemini_discovery_unchanged():
    assert model_catalog.discover_subprocess_models_from_pricing("codex") == [
        "gpt-5.4-pro",
        "gpt-5.5",
        "o3",
    ]
    assert model_catalog.discover_subprocess_models_from_pricing("gemini") == [
        "gemini-2.5-flash",
        "gemini-3.1-pro-preview",
    ]


async def test_claude_cli_sync_keeps_known_models_without_variants():
    known = PRESETS["claude-cli-subprocess"]["known_models"]
    ids = [m["id"] for m in await router.discover_models(_claude_provider())]
    assert ids[: len(known)] == known
    assert ids[len(known) :] == ["claude-3-5-haiku-latest"]


async def test_agy_fallback_still_lists_its_effort_variants(monkeypatch):
    async def agy_absent(timeout_s=30.0):
        return []

    monkeypatch.setattr("plugboard.cli_subprocess.list_agy_models", agy_absent)
    agy = {
        "id": "agy",
        "auth_source": "subprocess:agy",
        "default_model": "gemini-3.8-flash-medium",
    }
    ids = [m["id"] for m in await router.discover_models(agy)]
    assert {"claude-opus-5-5-medium", "claude-sonnet-5-5-medium"} <= set(ids)


async def test_refresh_and_catalogue_publish_no_claude_effort_variant(
    tmp_path, monkeypatch
):
    async def accepted(kind, model_id, *, timeout_s):
        return {"ok": True, "terminal": False, "reason": ""}

    monkeypatch.setattr(ma, "probe_subprocess_model", accepted)
    provider = _claude_provider()
    report = await ma.refresh_subprocess_provider_models(
        provider,
        probe_kinds={"claude"},
        max_probes=100,
        quarantine_path=tmp_path / "quarantine.json",
    )
    refreshed = [m["id"] for m in report["models"]]
    assert "claude-opus-5-5" in refreshed
    assert [m for m in refreshed if m.endswith(EFFORT)] == []

    provider["models"] = report["models"]
    monkeypatch.setattr(router, "load_providers", lambda: [provider])
    monkeypatch.setattr(router, "_models_cache", None)
    monkeypatch.setattr(router, "_model_status_mtime_changed", lambda: False)
    published = [m["id"] for m in await router.list_models()]
    assert "claude-code:claude-opus-5-5" in published
    assert [m for m in published if m.endswith(EFFORT)] == []
