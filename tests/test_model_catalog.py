"""Tests for model_catalog — MODEL-1 display_name, MODEL-2 dedupe."""

from __future__ import annotations

from plugboard.model_catalog import (
    build_alias_map,
    classify,
    dedupe_family_snapshots,
    discover_subprocess_models_from_pricing,
    enrich,
)


# ── classify — display_name, family, variant extraction ─────────────────────


def test_classify_ollama_name_tag():
    """Ollama IDs are `provider:name:tag`. Display must preserve `name:tag`."""
    r = classify("ollama:gemma4:latest", owned_by="ollama")
    assert r["display_name"] == "gemma4:latest"
    assert r["family"] == "gemma4"
    assert r["variant"] == "latest"


def test_classify_ollama_cloud_variant():
    r = classify("ollama:deepseek-v3.1:671b-cloud", owned_by="ollama")
    assert r["display_name"] == "deepseek-v3.1:671b-cloud"
    assert r["family"] == "deepseek-v3.1"
    assert r["variant"] == "671b-cloud"


def test_classify_ollama_single_segment_no_tag():
    r = classify("ollama:mistral", owned_by="ollama")
    assert r["display_name"] == "mistral"
    assert r["family"] == "mistral"
    assert r["variant"] == ""


def test_classify_claude_dated():
    r = classify(
        "claude-via-claude-cli:claude-3-5-haiku-20241022",
        owned_by="claude-via-claude-cli",
    )
    assert r["display_name"] == "claude-3-5-haiku-20241022"
    assert r["family"] == "claude-3-5-haiku"
    assert r["variant"] == "20241022"


def test_classify_claude_latest_alias():
    r = classify(
        "claude-via-claude-cli:claude-3-5-haiku-latest",
        owned_by="claude-via-claude-cli",
    )
    assert r["display_name"] == "claude-3-5-haiku-latest"
    assert r["family"] == "claude-3-5-haiku"
    assert r["variant"] == "latest"


def test_classify_github_models_namespaced_id():
    """GitHub Models IDs use `/` not `:` — treat the whole `rest` as family."""
    r = classify(
        "github-models-via-gh-cli:openai/gpt-4o",
        owned_by="github-models-via-gh-cli",
    )
    assert r["display_name"] == "openai/gpt-4o"
    assert r["family"] == "openai/gpt-4o"
    assert r["variant"] == ""


def test_classify_unprefixed_id_falls_back_to_raw():
    r = classify("bare-model-id")
    assert r["display_name"] == "bare-model-id"
    assert r["family"] == "bare-model-id"


# ── enrich — batch classification preserves metadata ───────────────────────


def test_enrich_preserves_existing_fields():
    input_models = [
        {
            "id": "ollama:llama3:8b",
            "object": "model",
            "owned_by": "ollama",
            "provider": "ollama",
        },
    ]
    out = enrich(input_models)
    assert len(out) == 1
    entry = out[0]
    # Original fields kept.
    assert entry["id"] == "ollama:llama3:8b"
    assert entry["object"] == "model"
    assert entry["provider"] == "ollama"
    # New fields added.
    assert entry["display_name"] == "llama3:8b"
    assert entry["family"] == "llama3"
    assert entry["variant"] == "8b"


# ── dedupe_family_snapshots — keep `-latest`, drop dated duplicates ────────


def test_dedupe_drops_dated_when_latest_exists():
    """claude-3-5-haiku: latest + 20241022 → only latest kept."""
    models = enrich(
        [
            {
                "id": "claude-via-claude-cli:claude-3-5-haiku-latest",
                "owned_by": "claude-via-claude-cli",
            },
            {
                "id": "claude-via-claude-cli:claude-3-5-haiku-20241022",
                "owned_by": "claude-via-claude-cli",
            },
        ]
    )
    kept = dedupe_family_snapshots(models)
    kept_ids = [m["id"] for m in kept]
    assert "claude-via-claude-cli:claude-3-5-haiku-latest" in kept_ids
    assert "claude-via-claude-cli:claude-3-5-haiku-20241022" not in kept_ids


def test_dedupe_promotes_newest_dated_when_no_latest_or_bare():
    """Rule 3 — when only dated snapshots exist (no -latest, no bare),
    keep the most recent as canonical with an undated display_name, and
    expose the bare family name as an alias so API callers can address
    the model either way."""
    models = enrich(
        [
            {
                "id": "claude-via-claude-cli:claude-3-opus-20240229",
                "owned_by": "claude-via-claude-cli",
            },
            {
                "id": "claude-via-claude-cli:claude-3-opus-20240301",
                "owned_by": "claude-via-claude-cli",
            },
        ]
    )
    kept = dedupe_family_snapshots(models)
    assert len(kept) == 1
    canonical = kept[0]
    # Newest date wins.
    assert canonical["id"] == "claude-via-claude-cli:claude-3-opus-20240301"
    # display_name rewritten to the undated family name.
    assert canonical["display_name"] == "claude-3-opus"
    assert canonical["variant"] == ""
    # Bare name exposed as an alias for the router.
    assert "claude-3-opus" in (canonical.get("aliases") or [])
    # Original variant retained for debugging / audit.
    assert canonical.get("promoted_from") == "20240301"


def test_dedupe_promotes_single_dated_entry():
    """One dated snapshot with no siblings still gets cleaned up: the picker
    should display the undated name even though the backing ID stays dated."""
    models = enrich(
        [
            {
                "id": "claude-via-claude-cli:claude-3-opus-20240229",
                "owned_by": "claude-via-claude-cli",
            },
        ]
    )
    kept = dedupe_family_snapshots(models)
    assert len(kept) == 1
    assert kept[0]["display_name"] == "claude-3-opus"
    assert kept[0]["id"] == "claude-via-claude-cli:claude-3-opus-20240229"
    assert "claude-3-opus" in (kept[0].get("aliases") or [])


def test_dedupe_never_drops_ollama_size_variants():
    """7b, 70b, 671b-cloud describe hardware sizes — they MUST all survive."""
    models = enrich(
        [
            {"id": "ollama:llama3:8b", "owned_by": "ollama"},
            {"id": "ollama:llama3:70b", "owned_by": "ollama"},
            {"id": "ollama:llama3:latest", "owned_by": "ollama"},
        ]
    )
    kept = dedupe_family_snapshots(models)
    variants = {m["variant"] for m in kept}
    # All three variants kept — "latest" alongside size tags.
    assert "8b" in variants
    assert "70b" in variants
    assert "latest" in variants


def test_dedupe_scopes_by_owned_by():
    """Two providers with the same family must not cross-pollute dedupe."""
    models = enrich(
        [
            {"id": "providerA:foo-latest", "owned_by": "providerA"},
            {"id": "providerA:foo-20240101", "owned_by": "providerA"},
            {"id": "providerB:foo-20240101", "owned_by": "providerB"},
        ]
    )
    kept = dedupe_family_snapshots(models)
    kept_ids = {m["id"] for m in kept}
    # providerA's dated snapshot dropped (has latest).
    assert "providerA:foo-latest" in kept_ids
    assert "providerA:foo-20240101" not in kept_ids
    # providerB's dated snapshot kept (no latest sibling in its scope).
    assert "providerB:foo-20240101" in kept_ids


def test_dedupe_drops_family_with_trailing_zero_when_bare_exists():
    """Anthropic lists both `claude-opus-4` and `claude-opus-4-0` —
    semantically the same version. Drop the `-0` entry so the picker
    shows one canonical name."""
    models = enrich(
        [
            {
                "id": "claude-via-claude-cli:claude-opus-4",
                "owned_by": "claude-via-claude-cli",
            },
            {
                "id": "claude-via-claude-cli:claude-opus-4-0",
                "owned_by": "claude-via-claude-cli",
            },
            {
                "id": "claude-via-claude-cli:claude-sonnet-4",
                "owned_by": "claude-via-claude-cli",
            },
            {
                "id": "claude-via-claude-cli:claude-sonnet-4-0",
                "owned_by": "claude-via-claude-cli",
            },
        ]
    )
    kept_ids = {m["id"] for m in dedupe_family_snapshots(models)}
    assert "claude-via-claude-cli:claude-opus-4" in kept_ids
    assert "claude-via-claude-cli:claude-opus-4-0" not in kept_ids
    assert "claude-via-claude-cli:claude-sonnet-4" in kept_ids
    assert "claude-via-claude-cli:claude-sonnet-4-0" not in kept_ids


def test_dedupe_keeps_family_with_trailing_zero_when_no_bare_sibling():
    """If only `claude-opus-4-0` is listed (no bare `claude-opus-4`),
    keep it — the user needs something to pin to."""
    models = enrich(
        [
            {
                "id": "claude-via-claude-cli:claude-opus-4-0",
                "owned_by": "claude-via-claude-cli",
            },
        ]
    )
    kept = dedupe_family_snapshots(models)
    assert len(kept) == 1
    assert kept[0]["id"] == "claude-via-claude-cli:claude-opus-4-0"


def test_dedupe_scopes_zero_suffix_by_owned_by():
    """Cross-owner collision — providerA's `foo-0` must survive when
    providerB has `foo` (different scopes)."""
    models = enrich(
        [
            {"id": "providerA:foo-0", "owned_by": "providerA"},
            {"id": "providerB:foo", "owned_by": "providerB"},
        ]
    )
    kept_ids = {m["id"] for m in dedupe_family_snapshots(models)}
    assert "providerA:foo-0" in kept_ids
    assert "providerB:foo" in kept_ids


def test_dedupe_drops_dated_when_bare_exists_without_latest():
    """When `-latest` is absent but bare + dated coexist, the bare pointer
    wins. Claude Opus 4.5+ stopped shipping `-latest` aliases — instead
    the bare family name (`claude-opus-4-6`) is the canonical current
    pointer and the dated snapshot is the pin."""
    models = enrich(
        [
            {
                "id": "claude-via-claude-cli:claude-opus-4-6",
                "owned_by": "claude-via-claude-cli",
            },
            {
                "id": "claude-via-claude-cli:claude-opus-4-6-20260205",
                "owned_by": "claude-via-claude-cli",
            },
        ]
    )
    kept = dedupe_family_snapshots(models)
    kept_ids = {m["id"] for m in kept}
    assert "claude-via-claude-cli:claude-opus-4-6" in kept_ids
    assert "claude-via-claude-cli:claude-opus-4-6-20260205" not in kept_ids


def test_dedupe_drops_bare_family_when_latest_present():
    """`gpt-5.4` and `gpt-5.4-latest` refer to the same model on most CLIs
    — keep only `-latest` (the canonical pointer). Size tags with a
    non-empty variant (`7b`, `671b-cloud`) are unaffected."""
    models = enrich(
        [
            {"id": "chatgpt-cli:gpt-5.4", "owned_by": "chatgpt-cli"},
            {"id": "chatgpt-cli:gpt-5.4-latest", "owned_by": "chatgpt-cli"},
            {"id": "ollama:foo:7b", "owned_by": "ollama"},
            {"id": "ollama:foo:latest", "owned_by": "ollama"},
        ]
    )
    kept = dedupe_family_snapshots(models)
    kept_ids = {m["id"] for m in kept}
    # Bare `gpt-5.4` dropped in favour of `-latest`.
    assert "chatgpt-cli:gpt-5.4-latest" in kept_ids
    assert "chatgpt-cli:gpt-5.4" not in kept_ids
    # Ollama size tag `7b` preserved alongside `latest` — it's a
    # distinct hardware variant, not an alias.
    assert "ollama:foo:7b" in kept_ids
    assert "ollama:foo:latest" in kept_ids


def test_dedupe_preserves_original_order():
    models = enrich(
        [
            {"id": "x:aaa-latest", "owned_by": "x"},
            {"id": "x:bbb", "owned_by": "x"},
            {"id": "x:aaa-20240101", "owned_by": "x"},  # dropped
            {"id": "x:ccc", "owned_by": "x"},
        ]
    )
    kept_ids = [m["id"] for m in dedupe_family_snapshots(models)]
    assert kept_ids == ["x:aaa-latest", "x:bbb", "x:ccc"]


# ── build_alias_map — canonical-name → canonical-ID router hints ────────────


def test_build_alias_map_exposes_promoted_families():
    """After Rule 3 promotion the catalog entry ships an alias list; the map
    must convert the undated clean name into the canonical dated ID so the
    router can accept either form."""
    models = dedupe_family_snapshots(
        enrich(
            [
                {
                    "id": "claude-via-claude-cli:claude-3-opus-20240229",
                    "owned_by": "claude-via-claude-cli",
                },
                {
                    "id": "claude-via-claude-cli:claude-opus-4-1-20250805",
                    "owned_by": "claude-via-claude-cli",
                },
            ]
        )
    )
    aliases = build_alias_map(models)
    assert (
        aliases.get("claude-via-claude-cli:claude-3-opus")
        == "claude-via-claude-cli:claude-3-opus-20240229"
    )
    assert (
        aliases.get("claude-via-claude-cli:claude-opus-4-1")
        == "claude-via-claude-cli:claude-opus-4-1-20250805"
    )


def test_build_alias_map_ignores_entries_without_aliases():
    """Non-promoted entries (bare, latest, size tags) do not contribute."""
    models = dedupe_family_snapshots(
        enrich(
            [
                {"id": "provider:foo-latest", "owned_by": "provider"},
                {"id": "ollama:llama3:8b", "owned_by": "ollama"},
                {"id": "provider:bar", "owned_by": "provider"},
            ]
        )
    )
    aliases = build_alias_map(models)
    assert aliases == {}


def test_build_alias_map_scopes_by_owner():
    """Two owners exposing dated-only families with colliding bare names
    must produce two independent entries in the alias map."""
    models = dedupe_family_snapshots(
        enrich(
            [
                {"id": "owner-a:foo-20240101", "owned_by": "owner-a"},
                {"id": "owner-b:foo-20240215", "owned_by": "owner-b"},
            ]
        )
    )
    aliases = build_alias_map(models)
    assert aliases.get("owner-a:foo") == "owner-a:foo-20240101"
    assert aliases.get("owner-b:foo") == "owner-b:foo-20240215"


# ── discover_subprocess_models_from_pricing — dynamic Claude/Codex/Gemini ──


def test_discover_subprocess_unknown_kind_returns_empty(monkeypatch):
    """An unknown CLI kind must return an empty list, not raise."""
    assert discover_subprocess_models_from_pricing("nope") == []


def test_discover_subprocess_codex_surfaces_bare_gpt_models(monkeypatch):
    """Codex discovery may use pricing, but only bare CLI-style model IDs."""
    fake_pricing = {
        "models": {
            "gpt-5.5": {"input_price": 5.0, "output_price": 20.0},
            "gpt-5.5-2026-04-23": {"input_price": 5.0, "output_price": 20.0},
            "gpt-5.4-pro": {"input_price": 10.0, "output_price": 40.0},
            "gpt-4.1": {"input_price": 2.0, "output_price": 8.0},
            "o3": {"input_price": 2.0, "output_price": 8.0},
            "openai/gpt-5.5": {"input_price": 5.0, "output_price": 20.0},
            "azure/gpt-5.5": {"input_price": 5.0, "output_price": 20.0},
            "gemini-3.1-pro-preview": {"input_price": 2.0, "output_price": 12.0},
        }
    }
    monkeypatch.setattr("plugboard.billing.load_pricing", lambda _month: fake_pricing)
    monkeypatch.setattr("plugboard.billing.current_pricing_month", lambda: "2026_05")

    result = discover_subprocess_models_from_pricing("codex")
    assert "gpt-5.5" in result
    assert "gpt-5.5-2026-04-23" not in result
    assert "gpt-5.4-pro" in result
    assert "o3" in result
    assert "gpt-4.1" not in result
    assert "openai/gpt-5.5" not in result
    assert "azure/gpt-5.5" not in result
    assert "gemini-3.1-pro-preview" not in result


def test_discover_subprocess_gemini_surfaces_text_models(monkeypatch):
    """Gemini pricing discovery keeps text/chat model IDs and filters noise."""
    fake_pricing = {
        "models": {
            "gemini-3.1-pro-preview": {"input_price": 2.0, "output_price": 12.0},
            "gemini-3.1-flash-preview": {"input_price": 1.0, "output_price": 4.0},
            "gemini-2.5-pro": {"input_price": 2.0, "output_price": 12.0},
            "gemini-2.5-flash": {"input_price": 1.0, "output_price": 4.0},
            "gemini-flash-latest": {"input_price": 1.0, "output_price": 4.0},
            "gemini-2.5-flash-native-audio-preview-09-2025": {
                "input_price": 1.0,
                "output_price": 4.0,
            },
            "gemini-3.1-pro-preview-customtools": {
                "input_price": 2.0,
                "output_price": 12.0,
            },
            "vertex_ai/gemini-3.1-pro-preview": {
                "input_price": 2.0,
                "output_price": 12.0,
            },
            "claude-sonnet-4-6": {"input_price": 3.0, "output_price": 15.0},
        }
    }
    monkeypatch.setattr("plugboard.billing.load_pricing", lambda _month: fake_pricing)
    monkeypatch.setattr("plugboard.billing.current_pricing_month", lambda: "2026_05")

    result = discover_subprocess_models_from_pricing("gemini")
    assert "gemini-3.1-pro-preview" in result
    assert "gemini-3.1-flash-preview" in result
    assert "gemini-2.5-pro" in result
    assert "gemini-2.5-flash" in result
    assert "gemini-flash-latest" in result
    assert "gemini-2.5-flash-native-audio-preview-09-2025" not in result
    assert "gemini-3.1-pro-preview-customtools" not in result
    assert "vertex_ai/gemini-3.1-pro-preview" not in result
    assert "claude-sonnet-4-6" not in result


def test_discover_subprocess_claude_surfaces_new_models(monkeypatch):
    """A freshly-shipped model added to the LiteLLM pricing snapshot must
    surface in the Claude CLI's dynamic catalog without any preset edit
    — this is the whole point of pricing-driven discovery."""
    fake_pricing = {
        "models": {
            "claude-opus-4-7": {"input_price": 5.0, "output_price": 25.0},
            "claude-opus-4-7-20260416": {"input_price": 5.0, "output_price": 25.0},
            "claude-opus-4-6": {"input_price": 5.0, "output_price": 25.0},
            "claude-sonnet-4-6": {"input_price": 3.0, "output_price": 15.0},
            # Vendor-namespaced variants must NOT leak — the CLI cannot
            # call them.
            "azure_ai/claude-opus-4-7": {"input_price": 5.0, "output_price": 25.0},
            "us.anthropic.claude-opus-4-7": {
                "input_price": 5.0,
                "output_price": 25.0,
            },
            # Bedrock-style ``-v1:0`` suffix even without regional prefix.
            "claude-sonnet-4-5-20250929-v1:0": {
                "input_price": 3.0,
                "output_price": 15.0,
            },
            # Non-Claude model must be filtered out by prefix.
            "gpt-5": {"input_price": 5.0, "output_price": 20.0},
        }
    }
    monkeypatch.setattr("plugboard.billing.load_pricing", lambda _month: fake_pricing)
    monkeypatch.setattr("plugboard.billing.current_pricing_month", lambda: "2026_05")
    result = discover_subprocess_models_from_pricing("claude")
    assert "claude-opus-4-7" in result
    assert "claude-opus-4-7-20260416" in result
    assert "claude-opus-4-6" in result
    assert "claude-sonnet-4-6" in result
    # Filters
    assert "azure_ai/claude-opus-4-7" not in result
    assert "us.anthropic.claude-opus-4-7" not in result
    assert "claude-sonnet-4-5-20250929-v1:0" not in result
    assert "gpt-5" not in result
    # Deterministic order so the dashboard's render is stable.
    assert result == sorted(result)


def test_discover_subprocess_handles_missing_pricing(monkeypatch):
    """An empty / missing pricing snapshot must degrade to an empty list,
    not raise — the subprocess provider's preset baseline is the
    safety net here."""
    monkeypatch.setattr("plugboard.billing.load_pricing", lambda _month: {})
    monkeypatch.setattr("plugboard.billing.current_pricing_month", lambda: "1999_01")
    assert discover_subprocess_models_from_pricing("claude") == []


def test_discover_subprocess_handles_corrupt_models_field(monkeypatch):
    """Defensive: pricing JSON with a wrong shape for ``models`` must not
    crash the call site."""
    monkeypatch.setattr(
        "plugboard.billing.load_pricing",
        lambda _month: {"models": "not-a-dict"},
    )
    monkeypatch.setattr("plugboard.billing.current_pricing_month", lambda: "2026_05")
    assert discover_subprocess_models_from_pricing("claude") == []
