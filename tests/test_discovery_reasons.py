"""BUG-12: a discovery that finds no model says why."""

from __future__ import annotations

import json

import pytest

from plugboard import config
from plugboard import model_availability as ma
from plugboard.router import invalidate_models_cache
from tests.conftest import _setup_providers

AUTH = {"Authorization": "Bearer testkey"}
SIGNED_OUT = "CLI 'claude' exited 1: claude is not signed in"


@pytest.fixture(autouse=True)
def _account_status_unknown(monkeypatch):
    """Never run the host's real claude/codex for the account check."""

    async def unknown(kind, timeout_s=10.0):
        return {"signed_in": None, "error": ""}

    monkeypatch.setattr("plugboard.cli_subprocess.probe_cli_signed_in", unknown)


def _cli_provider() -> dict:
    return {
        "id": "claude-cli",
        "name": "Claude (via claude CLI)",
        "is_active": True,
        "auth_source": "subprocess:claude",
        "provider_type": "cloud",
        "default_model": "claude-sonnet-5-5",
        "models": [],
    }


@pytest.mark.asyncio
async def test_probe_failures_keep_their_reason(tmp_path, monkeypatch):
    _setup_providers(tmp_path, monkeypatch, [_cli_provider()])

    async def listed(provider):
        return [{"id": "claude-sonnet-5-5"}]

    async def probe(kind, mid, *, timeout_s):
        return {"ok": False, "terminal": False, "reason": SIGNED_OUT}

    monkeypatch.setattr("plugboard.router.discover_models", listed)
    monkeypatch.setattr(ma, "probe_subprocess_model", probe)
    report = await ma.refresh_subprocess_provider_models(
        config.get_provider("claude-cli"),
        probe_kinds={"claude"},
        quarantine_path=tmp_path / "quarantine.json",
    )
    assert report["models"] == []
    assert report["dropped"] == [
        {
            "id": "claude-sonnet-5-5",
            "reason": "probe_failed",
            "failures": 1,
            "detail": SIGNED_OUT,
        }
    ]


@pytest.mark.asyncio
async def test_discover_says_why_no_cli_model_answered(
    client, _providers_file, monkeypatch
):
    _providers_file.write_text(json.dumps({"providers": [_cli_provider()]}))
    invalidate_models_cache()

    async def report(provider, **kwargs):
        return {
            "models": [],
            "candidate_count": 3,
            "verified_count": 2,
            "dropped": [
                {"id": "a", "reason": "probe_failed", "detail": SIGNED_OUT},
                {"id": "b", "reason": "probe_failed", "detail": SIGNED_OUT},
            ],
        }

    monkeypatch.setattr(ma, "refresh_subprocess_provider_models", report)
    resp = await client.post("/providers/claude-cli/discover", headers=AUTH)
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail.startswith("Could not discover models from 'claude-cli'")
    assert "2 of 3 models tested" in detail
    assert detail.count("claude is not signed in") == 1


@pytest.mark.asyncio
async def test_discover_says_when_there_was_nothing_to_test(
    client, _providers_file, monkeypatch
):
    _providers_file.write_text(json.dumps({"providers": [_cli_provider()]}))
    invalidate_models_cache()

    async def report(provider, **kwargs):
        return {"models": [], "candidate_count": 0, "verified_count": 0, "dropped": []}

    monkeypatch.setattr(ma, "refresh_subprocess_provider_models", report)
    resp = await client.post("/providers/claude-cli/discover", headers=AUTH)
    assert resp.status_code == 502
    assert "no candidate model" in resp.json()["detail"]
