"""BUG-12: a CLI discovery that finds nothing names the root cause.

UAT: Codex signed out and model probes off -> the dashboard only said
"no candidate model to test".
"""

from __future__ import annotations

import pytest

from plugboard import cli_subprocess as cs
import json

from plugboard.router import invalidate_models_cache

AUTH = {"Authorization": "Bearer testkey"}


def _codex() -> dict:
    return {
        "id": "codex-cli",
        "name": "ChatGPT (via codex CLI)",
        "is_active": True,
        "auth_source": "subprocess:codex",
        "provider_type": "cloud",
        "default_model": "gpt-5.5",
        "models": [],
    }


async def _empty_report(provider, **kwargs):
    return {"models": [], "candidate_count": 0, "verified_count": 0, "dropped": []}


@pytest.mark.asyncio
async def test_signed_out_cli_is_the_reason(client, _providers_file, monkeypatch):
    _providers_file.write_text(json.dumps({"providers": [_codex()]}))
    invalidate_models_cache()
    monkeypatch.setattr(
        "plugboard.model_availability.refresh_subprocess_provider_models", _empty_report
    )

    async def signed_out(kind, timeout_s=10.0):
        return {"signed_in": False, "error": cs.CODEX_SIGN_IN_HINT}

    monkeypatch.setattr(cs, "probe_cli_signed_in", signed_out)
    resp = await client.post("/providers/codex-cli/discover", headers=AUTH)
    assert resp.status_code == 502
    assert "codex login" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_probes_off_is_the_reason(client, _providers_file, monkeypatch):
    _providers_file.write_text(json.dumps({"providers": [_codex()]}))
    invalidate_models_cache()
    monkeypatch.setattr(
        "plugboard.model_availability.refresh_subprocess_provider_models", _empty_report
    )
    monkeypatch.setenv("PLUGBOARD_MODEL_REFRESH_PROBE_KINDS", "claude")

    async def signed_in(kind, timeout_s=10.0):
        return {"signed_in": True, "error": ""}

    monkeypatch.setattr(cs, "probe_cli_signed_in", signed_in)
    resp = await client.post("/providers/codex-cli/discover", headers=AUTH)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "PLUGBOARD_MODEL_REFRESH_PROBE_KINDS" in detail and "codex" in detail
