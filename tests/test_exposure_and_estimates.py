"""Model exposure policy, codex catalog cache, nearest-model price estimates."""

import json

import pytest

from plugboard import billing, model_exposure


# ── Exposure policy ──────────────────────────────────────────────────────────


def test_latest_ids_keeps_two_newest_versions_per_family():
    ids = [
        "claude-opus-5-5",
        "claude-opus-4-7",
        "claude-opus-4-5",
        "claude-sonnet-4-5-20250929",
        "claude-sonnet-4-5",
        "claude-sonnet-5-5",
        "codex-auto-review",
    ]
    kept = model_exposure.latest_ids(ids, 2)
    assert {"claude-opus-5-5", "claude-opus-4-7"} <= kept
    assert "claude-opus-4-5" not in kept
    # A dated snapshot is the same version as its base name.
    assert {
        "claude-sonnet-5-5",
        "claude-sonnet-4-5",
        "claude-sonnet-4-5-20250929",
    } <= kept
    assert "codex-auto-review" in kept  # no version: always listed


def test_hidden_ids_respects_mode_and_protection(monkeypatch):
    ids = ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol"]
    monkeypatch.delenv("PLUGBOARD_MODEL_EXPOSE", raising=False)
    assert model_exposure.hidden_ids(ids, set()) == ["gpt-5.6-sol"]
    assert model_exposure.hidden_ids(ids, {"gpt-5.6-sol"}) == []
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSE_VERSIONS", "1")
    assert model_exposure.hidden_ids(ids, set()) == ["gpt-6-sol", "gpt-5.6-sol"]
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSE", "all")
    assert model_exposure.hidden_ids(ids, set()) == []


def test_exposure_settings_fall_back_on_bad_values(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSE", "sometimes")
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSE_VERSIONS", "many")
    assert model_exposure.mode() == "latest"
    assert model_exposure.versions() == model_exposure.DEFAULT_VERSIONS
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSE_VERSIONS", "99")
    assert model_exposure.versions() == 10


def test_v1_models_hides_older_versions_but_keeps_default(monkeypatch):
    from plugboard import router

    providers = [
        {
            "id": "cli",
            "default_model": "gpt-5.6-sol",
            "models": [
                {"id": m}
                for m in ("gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-5.5-sol")
            ],
        }
    ]
    items = [{"id": f"cli:{m['id']}"} for m in providers[0]["models"]]
    monkeypatch.setattr("plugboard.usage.get_all_used_models", lambda: set())
    monkeypatch.delenv("PLUGBOARD_MODEL_EXPOSE", raising=False)
    shown = [m["id"] for m in router._apply_exposure(items, providers)]
    assert shown == ["cli:gpt-6.1-sol", "cli:gpt-6-sol", "cli:gpt-5.6-sol"]
    monkeypatch.setenv("PLUGBOARD_MODEL_EXPOSE", "all")
    assert len(router._apply_exposure(items, providers)) == 4


# ── codex catalog ────────────────────────────────────────────────────────────


def test_list_codex_models_reads_listed_slugs_by_priority(tmp_path):
    from plugboard.cli_subprocess import list_codex_models

    cache = tmp_path / "models_cache.json"
    cache.write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-6-sol", "visibility": "list", "priority": 3},
                    {"slug": "gpt-6.1-sol", "visibility": "list", "priority": 1},
                    {"slug": "gpt-reserve", "visibility": "hide", "priority": 2},
                    {"display_name": "no slug"},
                ]
            }
        )
    )
    assert list_codex_models(cache) == ["gpt-6.1-sol", "gpt-6-sol"]
    assert list_codex_models(tmp_path / "missing.json") == []
    cache.write_text("not json")
    assert list_codex_models(cache) == []


@pytest.mark.asyncio
async def test_discover_models_uses_codex_cache(monkeypatch):
    from plugboard import cli_subprocess, router

    monkeypatch.setattr(cli_subprocess, "list_codex_models", lambda: ["gpt-6.1-sol"])
    found = await router.discover_models(
        {"id": "codex", "auth_source": "subprocess:codex"}
    )
    assert [m["id"] for m in found] == ["gpt-6.1-sol"]
    assert router.last_discovery_trusted() is True


# ── Nearest-model price estimate ────────────────────────────────────────────


GRID = {
    "models": {
        "claude-opus-5-5": {
            "input_price": 5.0,
            "output_price": 25.0,
            "source": "litellm",
        },
        "claude-opus-4-7": {
            "input_price": 15.0,
            "output_price": 75.0,
            "source": "litellm",
        },
        "claude-sonnet-5-5": {
            "input_price": 3.0,
            "output_price": 15.0,
            "source": "litellm",
        },
        "gpt-6-sol": {"input_price": 2.0, "output_price": 8.0, "source": "litellm"},
    },
    "models_by_provider": {
        "agy": {
            "gemini-4-argon": {
                "input_price": 1.0,
                "output_price": 4.0,
                "source": "manual",
            }
        },
    },
}


def test_nearest_price_picks_closest_version_of_same_family():
    match = billing.nearest_price("claude-opus-5-6", GRID, [])
    assert match is not None
    assert match.entry["estimated_from"] == "claude-opus-5-5"
    assert match.entry["source"] == "estimate"
    assert match.entry["input_price"] == 5.0
    assert match.key == "claude-opus-5-6"


def test_nearest_price_strips_effort_suffix_and_prefers_provider_space():
    match = billing.nearest_price("gemini-4-argon-high", GRID, ["agy"])
    assert match is not None and match.space == "agy"
    assert match.entry["estimated_from"] == "gemini-4-argon"


def test_nearest_price_keeps_ollama_tags():
    grid = {
        "models": {
            "nemotron-3-nano:30b-cloud": {"input_price": 0.06, "output_price": 0.24}
        }
    }
    match = billing.nearest_price("nemotron-3-nano:31b-cloud", grid, [])
    assert match is not None and match.key == "nemotron-3-nano:31b-cloud"


def test_nearest_price_never_crosses_families():
    assert billing.nearest_price("claude-haiku-5-5", GRID, []) is None
    assert billing.nearest_price("unversioned-model", GRID, []) is None


def test_resolve_price_estimates_and_queues_review(tmp_path, monkeypatch):
    from plugboard import pricing_suggestions

    monkeypatch.setattr(billing, "_PRICING_DIR", tmp_path)
    monkeypatch.setattr(billing, "_GRID_CACHE", {})
    monkeypatch.setattr(billing, "_estimate_notified", set())
    monkeypatch.setattr(billing, "_builtin_match", lambda candidates: None)
    month = billing.current_pricing_month()
    (tmp_path / f"{month}.json").write_text(json.dumps(GRID))
    journal = []
    monkeypatch.setattr(
        "plugboard.change_events.ChangeBatch.flush_in_background",
        lambda self, deadline_s=120.0: journal.extend(self.events) or {},
    )

    match = billing.resolve_price("claude-opus-5-6", month)
    assert match is not None and match.source == "estimate"
    billing.resolve_price("claude-opus-5-6", month)  # notified once only

    pending = pricing_suggestions.list_suggestions()
    assert len(pending) == 1
    item = pending[0]
    assert item["type"] == "estimate" and item["model"] == "claude-opus-5-6"
    assert item["source"] == "claude-opus-5-5"
    assert [e for e, _ in journal] == ["price.estimated"]

    pricing_suggestions.apply(item["id"])
    grid = billing.load_pricing(month)
    assert grid["models"]["claude-opus-5-6"]["input_price"] == 5.0
    assert grid["models"]["claude-opus-5-6"]["source"] == "manual"


# ── Connection test headers ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connection_test_uses_preset_auth_header(monkeypatch):
    from plugboard import app as app_mod
    from plugboard.direct_providers import PRESETS

    seen = {}

    class Resp:
        status = 200

        async def json(self, content_type=None):
            return {"data": [{"id": "claude-opus-5-5"}]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, headers=None):
            seen["url"], seen["headers"] = url, headers
            return Resp()

    monkeypatch.setattr("aiohttp.ClientSession", Session)
    result = await app_mod._test_openai_endpoint(
        "https://api.anthropic.com/v1", "sk-test", False, preset=PRESETS["anthropic"]
    )
    assert result["status"] == "ok"
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in seen["headers"]

    await app_mod._test_openai_endpoint("https://api.example.com/v1", "k", False)
    assert seen["headers"]["Authorization"] == "Bearer k"


# ── Preset extra headers reach every call ───────────────────────────────────


def test_auth_header_carries_preset_extra_headers():
    from plugboard.router import _build_auth_header

    anthropic = {
        "preset_key": "anthropic",
        "auth_header_name": "x-api-key",
        "auth_header_prefix": "",
        "bearer_token": "sk-test",
    }
    headers = _build_auth_header(anthropic)
    assert headers == {"anthropic-version": "2023-06-01", "x-api-key": "sk-test"}

    own = {
        **anthropic,
        "extra_headers": {"anthropic-version": "2099-01-01", "X-Extra": "1"},
    }
    headers = _build_auth_header(own)
    assert headers["anthropic-version"] == "2099-01-01" and headers["X-Extra"] == "1"

    plain = {"bearer_token": "k"}
    assert _build_auth_header(plain) == {"Authorization": "Bearer k"}


# ── /v1/models ids keep the serving provider ────────────────────────────────


@pytest.mark.parametrize(
    "raw,clean",
    [
        # Configured providers keep their own id (no vendor guess).
        ("anthropic-direct:claude-opus-5-5", "anthropic-direct:claude-opus-5-5"),
        (
            "antigravity-via-agy-cli:gpt-oss-120b-medium",
            "antigravity-via-agy-cli:gpt-oss-120b-medium",
        ),
        ("openrouter:openai/gpt-6", "openrouter:openai/gpt-6"),
        # The historical aliases still apply to the providers they stand for.
        ("claude-via-claude-cli:claude-opus-5-5", "claude-code:claude-opus-5-5"),
        ("chatgpt-via-codex-cli:gpt-6.1-sol", "Codex:gpt-6.1-sol"),
        ("direct:anthropic--claude-haiku-4-5", "direct:anthropic--claude-haiku-4-5"),
    ],
)
def test_clean_model_id_never_reassigns_a_configured_provider(raw, clean):
    from plugboard.router import _clean_model_id

    assert _clean_model_id(raw) == clean


# ── CLI calls are counted in /metrics ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cli_calls_are_recorded_in_metrics(tmp_path, monkeypatch):
    from plugboard import cli_subprocess, metrics, router
    from tests.conftest import _setup_providers

    _setup_providers(
        tmp_path,
        monkeypatch,
        [
            {
                "id": "agy-test",
                "name": "agy",
                "base_url": "subprocess://agy",
                "auth_source": "subprocess:agy",
                "default_model": "m1",
                "models": [{"id": "m1"}],
                "is_active": True,
            }
        ],
    )
    monkeypatch.setattr(metrics, "_stats", {})
    calls = {"n": 0}

    async def fake_complete(kind, body, timeout_s=None):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    body = {"model": "agy-test:m1", "messages": [{"role": "user", "content": "hi"}]}
    await router.forward(body)
    chunks = [c async for c in router.forward_stream(dict(body, stream=True))]
    assert chunks[-1] == b"data: [DONE]\n\n"
    with pytest.raises(Exception):
        await router.forward(body)
    stats = metrics.get_stats()["agy-test"]
    assert stats["requests"] == 3
    assert stats["errors"] == 1


def test_clean_alias_pins_the_cli_provider(tmp_path, monkeypatch):
    from plugboard import router
    from tests.conftest import _setup_providers

    def cli(pid, kind):
        return {
            "id": pid,
            "name": pid,
            "base_url": f"subprocess://{kind}",
            "auth_source": f"subprocess:{kind}",
            "default_model": "m",
            "is_active": True,
        }

    _setup_providers(
        tmp_path,
        monkeypatch,
        [
            cli("claude-via-claude-cli", "claude"),
            cli("chatgpt-via-codex-cli", "codex"),
        ],
    )
    found = router._resolve_all_providers("Codex:gpt-6-luna")
    assert [(p["id"], m) for p, m in found] == [("chatgpt-via-codex-cli", "gpt-6-luna")]
    found = router._resolve_all_providers("claude-code:claude-haiku-5-5")
    assert [(p["id"], m) for p, m in found] == [
        ("claude-via-claude-cli", "claude-haiku-5-5")
    ]


# ── Ollama: free by default, configurable cloud families ────────────────────


OLLAMA_HTML = """
<h2>Model pricing</h2><table><thead><tr><th>Model</th><th>Input</th><th>Cached input</th><th>Output</th></tr></thead>
<tbody>
<tr><td><a href="/library/clef">clef</a></td><td>$0.24</td><td>-</td><td>-</td></tr>
<tr><td><a href="/library/gemma4">gemma4</a></td><td>$0.14</td><td>$0.05</td><td>$0.40</td></tr>
<tr><td><a href="/library/gpt-oss">gpt-oss:120b</a></td><td>$0.15</td><td>$0.014</td><td>$0.60</td></tr>
<tr><td><a href="/library/gpt-oss">gpt-oss:120b (Off-Peak)</a></td><td>$0.07</td><td>$0.007</td><td>$0.30</td></tr>
<tr><td><a href="/library/nemotron-3-super">nemotron-3-super</a></td><td>$0.015</td><td>$0.015</td><td>$0.60</td></tr>
</tbody></table>
"""


def test_parse_ollama_pricing_reads_peak_rows():
    from plugboard.pricing_scraper import parse_ollama_pricing

    prices = parse_ollama_pricing(OLLAMA_HTML)
    assert prices == {
        "gemma4:cloud": {
            "input_price": 0.14,
            "output_price": 0.40,
            "cached_input_price": 0.05,
        },
        "gpt-oss:120b-cloud": {
            "input_price": 0.15,
            "output_price": 0.60,
            "cached_input_price": 0.014,
        },
        "nemotron-3-super:cloud": {
            "input_price": 0.015,
            "output_price": 0.60,
            "cached_input_price": 0.015,
        },
    }
    assert parse_ollama_pricing("<html>no table</html>") == {}


def test_ollama_models_use_ollama_grid(tmp_path, monkeypatch):
    from plugboard import pricing_merge

    monkeypatch.setattr(billing, "_PRICING_DIR", tmp_path)
    monkeypatch.setattr(billing, "_GRID_CACHE", {})
    monkeypatch.setattr(billing, "_builtin_match", lambda candidates: None)
    month = billing.current_pricing_month()
    grid = {
        "models": {
            "gpt-oss-120b": {
                "input_price": 9.0,
                "output_price": 9.0,
                "source": "litellm",
            }
        },
        "models_by_provider": {
            "ollama": {
                "gpt-oss:120b-cloud": {
                    "input_price": 0.15,
                    "output_price": 0.60,
                    "source": "provider_native",
                },
                "gemma4:cloud": {
                    "input_price": 0.14,
                    "output_price": 0.40,
                    "source": "provider_native",
                },
            }
        },
    }
    (tmp_path / f"{month}.json").write_text(json.dumps(grid))
    ollama = {"id": "ollama", "base_url": "http://localhost:11434/v1"}

    own = billing.resolve_price("gpt-oss:120b-cloud", month, provider=ollama)
    assert own.entry["input_price"] == 0.15 and own.space == "ollama"
    family = billing.resolve_price("gemma4:31b-cloud", month, provider=ollama)
    assert family.entry["output_price"] == 0.40
    local = billing.resolve_price("nimble:latest", month, provider=ollama)
    assert local.entry["input_price"] == 0 and local.source == "local"
    unlisted = billing.resolve_price("beam:cloud", month, provider=ollama)
    assert unlisted is None  # not published by Ollama: the usual lookup finds nothing
    # A published 40x output/input ratio passes the sanity check.
    assert pricing_merge.MAX_RATIO >= 40


def test_ollama_cloud_families_are_configurable(monkeypatch):
    from plugboard import ollama

    names = ["beam", "gpt-oss:120b", "mistral-large-3:675b"]
    monkeypatch.delenv("PLUGBOARD_OLLAMA_CLOUD_FAMILIES", raising=False)
    assert ollama.cloud_ids_for_whitelist(names) == ["beam:cloud", "gpt-oss:120b-cloud"]
    monkeypatch.setenv("PLUGBOARD_OLLAMA_CLOUD_FAMILIES", "all")
    assert "mistral-large-3:675b-cloud" in ollama.cloud_ids_for_whitelist(names)
    monkeypatch.setenv("PLUGBOARD_OLLAMA_CLOUD_FAMILIES", "mistral")
    assert ollama.cloud_ids_for_whitelist(names) == ["mistral-large-3:675b-cloud"]
