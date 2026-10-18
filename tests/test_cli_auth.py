"""
Tests for CLI auth loaders — Codex, Gemini, gh, gh+copilot.

Validates:
- TokenBundle validity + expiry logic
- Each loader reads the correct config file / subprocess
- Router uses auth_source=cli:* when set
- OAuth presets (no api_key) work end-to-end
- /admin/direct-providers/{key}/check endpoint
"""

from __future__ import annotations

import json
import subprocess
import time

import pytest
from aioresponses import aioresponses

from tests.conftest import _setup_providers
from tests.factories import PROVIDER_A


# ══════════════════════════════════════════════════════════════════════════════
# TokenBundle
# ══════════════════════════════════════════════════════════════════════════════


def test_token_bundle_valid_no_expiry():
    from plugboard.cli_auth import TokenBundle

    b = TokenBundle(access_token="abc")
    assert b.is_valid() is True


def test_token_bundle_valid_future_expiry():
    from plugboard.cli_auth import TokenBundle

    b = TokenBundle(access_token="abc", expires_at=time.time() + 3600)
    assert b.is_valid() is True


def test_token_bundle_expired():
    from plugboard.cli_auth import TokenBundle

    b = TokenBundle(access_token="abc", expires_at=time.time() - 1)
    assert b.is_valid() is False


def test_token_bundle_empty_not_valid():
    from plugboard.cli_auth import TokenBundle

    b = TokenBundle(access_token="")
    assert b.is_valid() is False


def test_token_bundle_skew():
    from plugboard.cli_auth import TokenBundle

    # Expires in 30s, skew is 60s → treated as expired
    b = TokenBundle(access_token="abc", expires_at=time.time() + 30)
    assert b.is_valid(skew=60) is False
    assert b.is_valid(skew=10) is True


# ══════════════════════════════════════════════════════════════════════════════
# CodexAuthLoader
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_codex_loader_reads_auth_json(tmp_path, monkeypatch):
    from plugboard.cli_auth import CodexAuthLoader, reset_loaders

    reset_loaders()
    fake_auth = tmp_path / "auth.json"
    fake_auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "codex-xyz",
                    "id_token": "id-abc",
                    "refresh_token": "refresh-123",
                    "account_id": "user@example.com",
                },
                "last_refresh": "2026-04-16T12:00:00Z",
            }
        )
    )
    loader = CodexAuthLoader()
    monkeypatch.setattr(loader, "CONFIG_PATH", fake_auth)
    bundle = await loader.get_token()
    assert bundle is not None
    assert bundle.access_token == "codex-xyz"
    assert bundle.account == "user@example.com"


@pytest.mark.asyncio
async def test_codex_loader_missing_file(tmp_path):
    from plugboard.cli_auth import CodexAuthLoader

    loader = CodexAuthLoader()
    loader.CONFIG_PATH = tmp_path / "nonexistent.json"
    bundle = await loader.get_token()
    assert bundle is None


def test_codex_loader_available_ok(tmp_path):
    from plugboard.cli_auth import CodexAuthLoader

    fake = tmp_path / "auth.json"
    fake.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {"access_token": "x", "account_id": "me"},
            }
        )
    )
    loader = CodexAuthLoader()
    loader.CONFIG_PATH = fake
    status = loader.available()
    assert status["ok"] is True
    assert status["account"] == "me"


def test_codex_loader_available_missing(tmp_path):
    from plugboard.cli_auth import CodexAuthLoader

    loader = CodexAuthLoader()
    loader.CONFIG_PATH = tmp_path / "nope.json"
    status = loader.available()
    assert status["ok"] is False


# ══════════════════════════════════════════════════════════════════════════════
# GeminiAuthLoader
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_gemini_loader_reads_oauth_creds(tmp_path):
    from plugboard.cli_auth import GeminiAuthLoader

    fake = tmp_path / "oauth_creds.json"
    fake_accounts = tmp_path / "google_accounts.json"
    fake.write_text(
        json.dumps(
            {
                "access_token": "ya29.gemini-xyz",
                "refresh_token": "1//rt",
                "expiry_date": int((time.time() + 3600) * 1000),
                "scope": "openid",
            }
        )
    )
    fake_accounts.write_text(json.dumps({"active": "me@gmail.com"}))
    loader = GeminiAuthLoader()
    loader.CONFIG_PATH = fake
    loader.ACCOUNTS_PATH = fake_accounts
    bundle = await loader.get_token()
    assert bundle is not None
    assert bundle.access_token == "ya29.gemini-xyz"
    assert bundle.account == "me@gmail.com"


@pytest.mark.asyncio
async def test_gemini_loader_refresh_on_expiry(tmp_path, monkeypatch):
    from plugboard.cli_auth import GeminiAuthLoader

    monkeypatch.setattr(GeminiAuthLoader, "CLIENT_ID", "test-client.apps")
    monkeypatch.setattr(GeminiAuthLoader, "CLIENT_SECRET", "test-secret")

    fake = tmp_path / "oauth_creds.json"
    # Expired token
    fake.write_text(
        json.dumps(
            {
                "access_token": "old-token",
                "refresh_token": "rt-xyz",
                "expiry_date": int((time.time() - 100) * 1000),
            }
        )
    )
    loader = GeminiAuthLoader()
    loader.CONFIG_PATH = fake
    loader.ACCOUNTS_PATH = tmp_path / "nonexistent.json"

    with aioresponses() as m:
        m.post(
            loader.TOKEN_ENDPOINT,
            payload={
                "access_token": "new-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
        bundle = await loader.get_token()

    assert bundle is not None
    assert bundle.access_token == "new-token"
    assert bundle.is_valid() is True


# ══════════════════════════════════════════════════════════════════════════════
# GHAuthLoader
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_gh_loader_subprocess_ok(monkeypatch):
    from plugboard.cli_auth import GHAuthLoader, reset_loaders

    reset_loaders()

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "auth", "token"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="ghp_fake_token\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="nope")

    monkeypatch.setattr(subprocess, "run", fake_run)
    loader = GHAuthLoader()
    bundle = await loader.get_token()
    assert bundle is not None
    assert bundle.access_token == "ghp_fake_token"


def test_gh_loader_not_installed(monkeypatch):
    from plugboard.cli_auth import GHAuthLoader

    def raise_fnf(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", raise_fnf)
    loader = GHAuthLoader()
    status = loader.available()
    assert status["ok"] is False
    assert "not installed" in status["error"].lower()


def test_gh_loader_not_logged_in(monkeypatch):
    from plugboard.cli_auth import GHAuthLoader

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)
    loader = GHAuthLoader()
    status = loader.available()
    assert status["ok"] is False


# ══════════════════════════════════════════════════════════════════════════════
# GHCopilotAuthLoader — PAT exchange
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_copilot_token_exchange(monkeypatch):
    from plugboard.cli_auth import GHCopilotAuthLoader, reset_loaders

    reset_loaders()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="ghp_pat_xyz\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    loader = GHCopilotAuthLoader()

    with aioresponses() as m:
        m.get(
            loader.COPILOT_TOKEN_ENDPOINT,
            payload={
                "token": "tid=ABC123;ts=999",
                "expires_at": time.time() + 1800,
                "refresh_in": 1500,
            },
        )
        bundle = await loader.get_token()

    assert bundle is not None
    assert bundle.access_token == "tid=ABC123;ts=999"


@pytest.mark.asyncio
async def test_copilot_token_exchange_fails_gracefully(monkeypatch):
    from plugboard.cli_auth import GHCopilotAuthLoader, AuthError, reset_loaders

    reset_loaders()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="ghp_pat_xyz\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    loader = GHCopilotAuthLoader()

    with aioresponses() as m:
        m.get(loader.COPILOT_TOKEN_ENDPOINT, status=403)
        with pytest.raises(AuthError) as exc_info:
            await loader.get_token()

    assert exc_info.value.status_code == 403
    assert "Copilot license missing" in str(exc_info.value)


# ══════════════════════════════════════════════════════════════════════════════
# get_loader registry
# ══════════════════════════════════════════════════════════════════════════════


def test_get_loader_returns_same_instance():
    from plugboard.cli_auth import get_loader, reset_loaders

    reset_loaders()
    loader1 = get_loader("gh")
    loader2 = get_loader("gh")
    assert loader1 is loader2


def test_get_loader_unknown_source():
    from plugboard.cli_auth import get_loader

    assert get_loader("unknown") is None


def test_get_loader_all_sources():
    from plugboard.cli_auth import get_loader, reset_loaders

    reset_loaders()
    for source in ["codex", "gemini", "gh", "gh+copilot", "claude"]:
        loader = get_loader(source)
        assert loader is not None, f"Missing loader for {source}"


# ══════════════════════════════════════════════════════════════════════════════
# Router integration — auth_source=cli:*
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_router_fallback_to_bearer_token_when_no_auth_source(
    tmp_path, monkeypatch
):
    """Provider without auth_source → uses bearer_token (current behavior)."""
    _setup_providers(tmp_path, monkeypatch, [PROVIDER_A])
    from plugboard.router import _build_auth_header_async

    header = await _build_auth_header_async(PROVIDER_A)
    assert header == {"Authorization": f"Bearer {PROVIDER_A['bearer_token']}"}


@pytest.mark.asyncio
async def test_router_uses_cli_token_when_auth_source_set(tmp_path, monkeypatch):
    """Provider with auth_source=cli:gh → fetches token from gh CLI."""
    from plugboard import cli_auth

    cli_auth.reset_loaders()

    def fake_run(cmd, **kwargs):
        if "token" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="ghp_test_token\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    provider = {
        "id": "gh-models",
        "bearer_token": "",
        "auth_source": "cli:gh",
        "auth_header_name": "Authorization",
        "auth_header_prefix": "Bearer",
    }
    from plugboard.router import _build_auth_header_async

    header = await _build_auth_header_async(provider)
    assert header == {"Authorization": "Bearer ghp_test_token"}


@pytest.mark.asyncio
async def test_router_raises_when_cli_unavailable(tmp_path, monkeypatch):
    """Provider with auth_source=cli:X but CLI unavailable → RuntimeError."""
    from plugboard import cli_auth

    cli_auth.reset_loaders()

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    provider = {
        "id": "gh-models",
        "bearer_token": "",
        "auth_source": "cli:gh",
    }
    from plugboard.router import _build_auth_header_async

    with pytest.raises(RuntimeError, match="Auth not available"):
        await _build_auth_header_async(provider)


# ══════════════════════════════════════════════════════════════════════════════
# OAuth presets — direct_providers
# ══════════════════════════════════════════════════════════════════════════════


def test_oauth_preset_is_detected():
    from plugboard.direct_providers import get_preset, is_oauth_preset

    assert is_oauth_preset(get_preset("gemini-oauth")) is True
    assert is_oauth_preset(get_preset("github-models-oauth")) is True
    assert is_oauth_preset(get_preset("openai")) is False


def test_create_from_preset_oauth_no_api_key(tmp_path, monkeypatch):
    """OAuth preset creates provider with empty bearer_token + auth_source set."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.direct_providers import create_from_preset

    record = create_from_preset("github-models-oauth", "")
    assert record["auth_source"] == "cli:gh"
    assert record["bearer_token"] == "(via CLI)"  # displayed masked
    assert "models.github.ai/inference" in record["base_url"]


def test_create_from_preset_oauth_ignores_provided_key(tmp_path, monkeypatch):
    """OAuth preset ignores api_key if provided."""
    _setup_providers(tmp_path, monkeypatch, [])
    from plugboard.direct_providers import create_from_preset

    record = create_from_preset("gemini-oauth", "ignored-key")
    assert record["auth_source"] == "cli:gemini"


def test_list_presets_includes_oauth_metadata():
    from plugboard.direct_providers import list_presets

    presets = list_presets()
    gemini = next(p for p in presets if p["key"] == "gemini-oauth")
    assert gemini["auth_source"] == "cli:gemini"
    assert gemini["requires_cli"] == "gemini"


# ══════════════════════════════════════════════════════════════════════════════
# /admin/direct-providers/{key}/check endpoint
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_check_endpoint_api_key_preset(admin_client):
    """/check for API-key preset returns auth_type=api_key."""
    resp = await admin_client.get(
        "/admin/direct-providers/openai/check",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["auth_type"] == "api_key"


@pytest.mark.asyncio
async def test_check_endpoint_oauth_preset_cli_missing(admin_client, monkeypatch):
    """/check for OAuth preset when CLI missing → ok=false."""
    from plugboard import cli_auth

    cli_auth.reset_loaders()

    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    resp = await admin_client.get(
        "/admin/direct-providers/github-models-oauth/check",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["auth_type"] == "oauth"
    assert data["ok"] is False


@pytest.mark.asyncio
async def test_check_endpoint_unknown_preset(admin_client):
    """/check for unknown preset returns 404."""
    resp = await admin_client.get(
        "/admin/direct-providers/bogus/check",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_oauth_preset_without_api_key(admin_client, monkeypatch):
    """POST /admin/direct-providers/gemini-oauth with empty body → 201 created."""
    resp = await admin_client.post(
        "/admin/direct-providers/gemini-oauth",
        json={},
        headers={"Authorization": "Bearer adminkey"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["auth_source"] == "cli:gemini"
