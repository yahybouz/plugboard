from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from plugboard.model_catalog import has_internet_access_default, enrich
from plugboard.app import app


def test_has_internet_access_default():
    # Major cloud providers: always True by name
    assert has_internet_access_default("claude-opus-4-7") is True
    assert has_internet_access_default("gpt-5.5") is True
    assert has_internet_access_default("gemini-3-pro") is True

    # Ollama-hosted models (cloud OR local) are pure inference with NO live web,
    # even with a ":cloud" tag — cloud-HOSTING is not web-ACCESS. Confirmed by
    # the live D3 internet probe across 3 surfaces (2026-06-01).
    assert has_internet_access_default("kimi-k2.5:cloud") is False
    assert has_internet_access_default("ollama:kimi-k2.6:cloud") is False
    assert has_internet_access_default("glm-5:cloud") is False

    # Local Ollama variants: also False (unchanged)
    assert has_internet_access_default("ollama:kimi-k2:7b") is False
    assert has_internet_access_default("ollama:glm-4-9b") is False
    assert has_internet_access_default("ollama:llama3.2-thinking:3b") is False
    assert has_internet_access_default("ollama:llama3.2:3b") is False

    # Ollama-hosted gpt-oss is offline whether ":20b" or ":cloud" — the
    # "ollama:" provider prefix short-circuits BEFORE the "gpt" name match.
    assert has_internet_access_default("ollama:gpt-oss:20b") is False
    assert has_internet_access_default("ollama:gpt-oss:cloud") is False

    # Provider-aware: the SAME family is online via its web CLI but offline via
    # Ollama. gemini-3-flash through gemini-cli has web; through Ollama it does not.
    assert has_internet_access_default("claude-code:claude-opus-4-7") is True
    assert has_internet_access_default("Codex:gpt-5.4") is True
    assert has_internet_access_default("Gemini:gemini-3-flash-preview") is True
    assert has_internet_access_default("ollama:gemini-3-flash-preview:cloud") is False

    # Real production provider ids (the actual prefixes in providers.json) — the
    # web CLIs stay online via their vendor token in the long provider id.
    assert has_internet_access_default("claude-via-claude-cli:claude-opus-4-7") is True
    assert has_internet_access_default("chatgpt-via-codex-cli:gpt-5.5") is True
    assert (
        has_internet_access_default("google-gemini-via-gemini-cli:gemini-3-pro-preview")
        is True
    )
    # …and the same offline-provider rule holds for the real ollama provider id.
    assert has_internet_access_default("ollama:deepseek-v4-pro:cloud") is False


def test_enrich_adds_internet_access():
    models = [
        {"id": "kimi-k2.5:cloud", "owned_by": "ollama"},
        {"id": "gpt-5.5", "owned_by": "openai", "internet_access": True},
        {"id": "claude-opus-4-7", "owned_by": "anthropic"},
        {"id": "ollama:llama3:8b", "owned_by": "ollama"},
        # A real probe persisted internet_access=False — must be PRESERVED, never
        # re-coerced to True by the heuristic (probe is the authority).
        {
            "id": "ollama:deepseek-v3.1:671b-cloud",
            "owned_by": "ollama",
            "internet_access": False,
        },
    ]
    enriched = enrich(models)

    assert enriched[0]["id"] == "kimi-k2.5:cloud"
    assert enriched[0]["internet_access"] is False  # ollama family: no web by default

    assert enriched[1]["id"] == "gpt-5.5"
    assert enriched[1]["internet_access"] is True  # preserved existing flag

    assert enriched[2]["id"] == "claude-opus-4-7"
    assert enriched[2]["internet_access"] is True

    # Local Ollama model: must default to False, not be coerced by name
    assert enriched[3]["id"] == "ollama:llama3:8b"
    assert enriched[3]["internet_access"] is False

    # Persisted probe=False is preserved (not overwritten by the heuristic)
    assert enriched[4]["id"] == "ollama:deepseek-v3.1:671b-cloud"
    assert enriched[4]["internet_access"] is False


@pytest.mark.asyncio
async def test_check_internet_endpoint_unauthorized():
    client = TestClient(app)
    # Anonymous requests without admin key are rejected with 401/403
    resp = client.post("/admin/models/check-internet", json={"model_id": "foo"})
    assert resp.status_code in {401, 403}


# ── Probe v2 (2026-05-31): drop nonce, add weekday + double-date safeguards ──


def _build_probe_response(content: str) -> dict:
    """Helper: shape an OpenAI chat completion response payload."""
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 80, "total_tokens": 110},
    }


@pytest.mark.asyncio
async def test_probe_passes_when_date_weekday_yesterday_match(monkeypatch):
    """Online verdict requires the date of today + at least one safeguard
    (weekday or yesterday). With all three matching, confidence=medium.
    """
    import datetime as _dt
    import plugboard.app as app_module

    today = _dt.datetime.now(_dt.timezone.utc)
    yesterday = today - _dt.timedelta(days=1)
    weekdays_fr = [
        "lundi",
        "mardi",
        "mercredi",
        "jeudi",
        "vendredi",
        "samedi",
        "dimanche",
    ]
    months_fr = [
        "janvier",
        "février",
        "mars",
        "avril",
        "mai",
        "juin",
        "juillet",
        "août",
        "septembre",
        "octobre",
        "novembre",
        "décembre",
    ]
    today_str = f"{today.day} {months_fr[today.month - 1]} {today.year}"
    yesterday_str = f"{yesterday.day} {months_fr[yesterday.month - 1]} {yesterday.year}"
    weekday_str = weekdays_fr[today.weekday()]
    response_text = (
        f"Aujourd'hui c'est {weekday_str}, le {today_str}. "
        f"Hier c'était le {yesterday_str}."
    )

    async def fake_forward(body, timeout=20.0):
        return _build_probe_response(response_text)

    monkeypatch.setattr(app_module, "forward", fake_forward)

    # Resolve a real model from the catalog so resolve_provider_and_model passes.
    monkeypatch.setattr(
        app_module,
        "resolve_provider_and_model",
        lambda mid: ({"id": "fake", "models": []}, "claude-opus-4-6"),
    )

    out = await app_module.check_model_internet_access(
        {"model_id": "claude-opus-4-6", "persist": False}, _=None
    )
    assert out["internet_access"] is True
    assert out["status"] == "online"
    # date + weekday + yesterday (no URL) → confidence = medium
    assert out["internet_access_confidence"] in {"medium", "high"}


@pytest.mark.asyncio
async def test_probe_fails_when_only_year_matches(monkeypatch):
    """A model that just says "in 2026..." without the exact day must NOT
    pass — regression guard against the previous year-only false positive.
    """
    import plugboard.app as app_module

    async def fake_forward(body, timeout=20.0):
        return _build_probe_response("En 2026, l'IA a beaucoup progressé.")

    monkeypatch.setattr(app_module, "forward", fake_forward)
    monkeypatch.setattr(
        app_module,
        "resolve_provider_and_model",
        lambda mid: ({"id": "fake", "models": []}, "fake-model"),
    )

    out = await app_module.check_model_internet_access(
        {"model_id": "fake-model", "persist": False}, _=None
    )
    assert out["internet_access"] is False
    assert out["status"] == "offline"


@pytest.mark.asyncio
async def test_probe_fails_when_date_present_but_no_safeguards(monkeypatch):
    """Probe v2 garde-fou: today's date alone is not enough — at least
    one of (weekday, yesterday) must also match.
    """
    import datetime as _dt
    import plugboard.app as app_module

    today = _dt.datetime.now(_dt.timezone.utc)
    today_iso = f"{today.year}-{today.month:02d}-{today.day:02d}"

    async def fake_forward(body, timeout=20.0):
        # Only today's date, no weekday, no yesterday — must be rejected.
        return _build_probe_response(f"La date est {today_iso}.")

    monkeypatch.setattr(app_module, "forward", fake_forward)
    monkeypatch.setattr(
        app_module,
        "resolve_provider_and_model",
        lambda mid: ({"id": "fake", "models": []}, "fake-model"),
    )

    out = await app_module.check_model_internet_access(
        {"model_id": "fake-model", "persist": False}, _=None
    )
    assert out["internet_access"] is False
    assert "weekday" in (out.get("error") or "") or "yesterday" in (
        out.get("error") or ""
    )
