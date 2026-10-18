"""OAuth Broker tests — Phase 5 of the broker implementation.

Covers:
- oauth_store: save/get/delete credentials (Fernet encrypted), flow TTL
- oauth_manager: PKCE generation, start_flow, complete_callback, poll_device, refresh
- Router integration: broker:* auth_source resolves to stored token
- Admin endpoints: /admin/oauth/start, callback, device poll, connections, disconnect
- CSRF protection via state
"""

from __future__ import annotations

import base64
import hashlib
import time
from pathlib import Path

import pytest
from aioresponses import aioresponses

from plugboard import oauth_manager, oauth_store
from plugboard.oauth_store import OAuthCredential, OAuthFlow


@pytest.fixture
def _oauth_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate oauth_store to a temp SQLite file per test."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    db_path = data_dir / "credentials.db"
    monkeypatch.setattr(oauth_store, "_DATA_DIR", data_dir)
    monkeypatch.setattr(oauth_store, "_DB_PATH", db_path)
    return db_path


@pytest.fixture(autouse=True)
def _google_client(monkeypatch: pytest.MonkeyPatch):
    """The Google OAuth client is not shipped: tests supply a fake one."""
    monkeypatch.setitem(
        oauth_manager.OAUTH_CONFIGS["google"], "client_id", "test-client.apps"
    )
    monkeypatch.setitem(
        oauth_manager.OAUTH_CONFIGS["google"], "client_secret", "test-secret"
    )


def test_google_flow_without_client_is_refused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(oauth_manager.OAUTH_CONFIGS["google"], "client_id", "")

    with pytest.raises(ValueError, match="GEMINI_OAUTH_CLIENT_ID"):
        oauth_manager.get_config("google")


@pytest.fixture
def _encryption_key(monkeypatch: pytest.MonkeyPatch):
    """Force a deterministic ENCRYPTION_KEY so _encrypt_token actually encrypts."""
    import plugboard.config as cfg

    monkeypatch.setattr(cfg, "_ENCRYPTION_KEY", "test-encryption-key-32chars-long!")


# ─── oauth_store (encryption + flow TTL) ────────────────────────────────────


def test_save_and_get_credential_roundtrip(_oauth_db):
    cred = OAuthCredential(
        source="google",
        account="alice@example.com",
        access_token="access-abc-123",
        refresh_token="refresh-xyz-456",
        expires_at=time.time() + 3600,
        scope="openid email",
        extra={"id_token": "idtok"},
    )
    oauth_store.save_credential(cred)
    got = oauth_store.get_credential("google")
    assert got is not None
    assert got.source == "google"
    assert got.account == "alice@example.com"
    assert got.access_token == "access-abc-123"
    assert got.refresh_token == "refresh-xyz-456"
    assert got.scope == "openid email"
    assert got.extra == {"id_token": "idtok"}


def test_tokens_are_encrypted_at_rest(_oauth_db, _encryption_key):
    """Raw SQLite rows must not contain plaintext access/refresh tokens."""
    import sqlite3

    oauth_store.save_credential(
        OAuthCredential(
            source="google",
            account="u@x.io",
            access_token="PLAINTEXT-SECRET-TOKEN",
            refresh_token="PLAINTEXT-REFRESH",
        )
    )
    raw = (
        sqlite3.connect(str(_oauth_db))
        .execute("SELECT access_token, refresh_token FROM oauth_credentials")
        .fetchone()
    )
    assert "PLAINTEXT-SECRET-TOKEN" not in raw[0]
    assert "PLAINTEXT-REFRESH" not in raw[1]


def test_delete_credential(_oauth_db):
    oauth_store.save_credential(
        OAuthCredential(
            source="github",
            account="gh",
            access_token="t",
        )
    )
    assert oauth_store.delete_credential("github") is True
    assert oauth_store.get_credential("github") is None
    # Second delete is a no-op
    assert oauth_store.delete_credential("github") is False


def test_list_credentials_no_tokens(_oauth_db):
    oauth_store.save_credential(
        OAuthCredential(
            source="google",
            account="a@x",
            access_token="tok",
            refresh_token="rtok",
            scope="openid",
        )
    )
    entries = oauth_store.list_credentials()
    assert len(entries) == 1
    # list_credentials must never expose token values
    assert "access_token" not in entries[0]
    assert "refresh_token" not in entries[0]
    assert entries[0]["source"] == "google"
    assert entries[0]["account"] == "a@x"


def test_flow_save_get_expiry_cleanup(_oauth_db):
    """Expired flows are auto-cleaned on lookup."""
    past = time.time() - 60
    oauth_store.save_flow(
        OAuthFlow(
            state="expired-state",
            source="google",
            code_verifier="v",
            flow_type="authorization_code",
            expires_at=past,
        )
    )
    # Expired flow not returned
    assert oauth_store.get_flow("expired-state") is None

    future = time.time() + 600
    oauth_store.save_flow(
        OAuthFlow(
            state="live-state",
            source="github",
            device_code="DEVICECODE",
            flow_type="device",
            expires_at=future,
        )
    )
    got = oauth_store.get_flow("live-state")
    assert got is not None
    assert got.source == "github"
    assert got.device_code == "DEVICECODE"


# ─── PKCE + start_flow ──────────────────────────────────────────────────────


def test_pkce_generates_valid_verifier_and_challenge():
    verifier, challenge = oauth_manager._gen_pkce()
    # RFC 7636: verifier length 43-128
    assert 43 <= len(verifier) <= 128
    # Challenge = base64url(SHA256(verifier)), no padding
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert challenge == expected


async def test_start_flow_google_returns_auth_url(_oauth_db):
    result = await oauth_manager.start_flow("google")
    assert result.flow == "authorization_code"
    assert result.state
    assert "accounts.google.com" in result.auth_url
    assert "code_challenge=" in result.auth_url
    assert "state=" + result.state in result.auth_url
    # Flow must be stored for later callback lookup
    stored = oauth_store.get_flow(result.state)
    assert stored is not None
    assert stored.source == "google"
    assert stored.code_verifier != ""


async def test_start_flow_github_device_code(_oauth_db):
    with aioresponses() as m:
        m.post(
            "https://github.com/login/device/code",
            status=200,
            payload={
                "device_code": "dcode-42",
                "user_code": "WXYZ-1234",
                "verification_uri": "https://github.com/login/device",
                "interval": 5,
                "expires_in": 900,
            },
        )
        result = await oauth_manager.start_flow("github")
    assert result.flow == "device"
    assert result.user_code == "WXYZ-1234"
    assert result.verification_uri == "https://github.com/login/device"
    assert result.interval == 5
    stored = oauth_store.get_flow(result.state)
    assert stored is not None
    assert stored.device_code == "dcode-42"


# ─── complete_callback (Authorization Code) ─────────────────────────────────


async def test_complete_callback_exchanges_code_for_tokens(_oauth_db):
    start = await oauth_manager.start_flow("google")

    with aioresponses() as m:
        m.post(
            "https://oauth2.googleapis.com/token",
            status=200,
            payload={
                "access_token": "atok",
                "refresh_token": "rtok",
                "expires_in": 3600,
                "scope": "openid email",
                "id_token": "idtok",
            },
        )
        m.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            status=200,
            payload={"email": "alice@example.com", "name": "Alice"},
        )
        cred = await oauth_manager.complete_callback(
            "google",
            start.state,
            "auth-code-abc",
        )

    assert cred.access_token == "atok"
    assert cred.refresh_token == "rtok"
    assert cred.account == "alice@example.com"
    # Credential persisted
    assert oauth_store.get_credential("google") is not None
    # Flow consumed
    assert oauth_store.get_flow(start.state) is None


async def test_complete_callback_rejects_unknown_state(_oauth_db):
    # CSRF protection: a bogus state must be rejected
    with pytest.raises(RuntimeError, match="Invalid or expired state"):
        await oauth_manager.complete_callback("google", "bogus-state", "code")


async def test_complete_callback_rejects_state_source_mismatch(_oauth_db):
    start = await oauth_manager.start_flow("google")
    # Attacker tries to redeem google state on openai endpoint
    with pytest.raises(RuntimeError, match="source mismatch"):
        await oauth_manager.complete_callback("openai", start.state, "code")


# ─── Device flow polling ────────────────────────────────────────────────────


async def test_poll_device_pending(_oauth_db):
    with aioresponses() as m:
        m.post(
            "https://github.com/login/device/code",
            status=200,
            payload={
                "device_code": "dc",
                "user_code": "UC",
                "verification_uri": "https://x",
                "interval": 5,
                "expires_in": 900,
            },
        )
        start = await oauth_manager.start_flow("github")

    with aioresponses() as m:
        m.post(
            "https://github.com/login/oauth/access_token",
            status=200,
            payload={"error": "authorization_pending"},
        )
        result = await oauth_manager.poll_device(start.state)
    assert result["status"] == "pending"


async def test_poll_device_success_stores_credential(_oauth_db):
    with aioresponses() as m:
        m.post(
            "https://github.com/login/device/code",
            status=200,
            payload={
                "device_code": "dc",
                "user_code": "UC",
                "verification_uri": "https://x",
                "interval": 5,
                "expires_in": 900,
            },
        )
        start = await oauth_manager.start_flow("github")

    with aioresponses() as m:
        m.post(
            "https://github.com/login/oauth/access_token",
            status=200,
            payload={"access_token": "gh-tok", "scope": "read:user"},
        )
        m.get(
            "https://api.github.com/user",
            status=200,
            payload={"login": "octocat", "id": 1},
        )
        result = await oauth_manager.poll_device(start.state)

    assert result["status"] == "success"
    assert result["account"] == "octocat"
    assert result["source"] == "github"
    stored = oauth_store.get_credential("github")
    assert stored is not None
    assert stored.access_token == "gh-tok"


# ─── Refresh ────────────────────────────────────────────────────────────────


async def test_refresh_uses_refresh_token(_oauth_db):
    oauth_store.save_credential(
        OAuthCredential(
            source="google",
            account="a@x",
            access_token="old",
            refresh_token="refresh-me",
            expires_at=time.time() - 10,
        )
    )
    with aioresponses() as m:
        m.post(
            "https://oauth2.googleapis.com/token",
            status=200,
            payload={"access_token": "new-tok", "expires_in": 3600},
        )
        new_cred = await oauth_manager.refresh("google")
    assert new_cred is not None
    assert new_cred.access_token == "new-tok"
    # Refresh token preserved when provider omits it in response
    assert new_cred.refresh_token == "refresh-me"


async def test_refresh_returns_none_when_no_refresh_token(_oauth_db):
    oauth_store.save_credential(
        OAuthCredential(
            source="google",
            account="a@x",
            access_token="t",
            refresh_token="",
        )
    )
    result = await oauth_manager.refresh("google")
    assert result is None


# ─── Router integration (broker:* auth_source) ──────────────────────────────


async def test_router_uses_broker_token_for_auth(_oauth_db):
    """Provider with auth_source=broker:google resolves via oauth_store."""
    from plugboard.router import _build_auth_header_async

    oauth_store.save_credential(
        OAuthCredential(
            source="google",
            account="a@x",
            access_token="broker-access-token",
            expires_at=time.time() + 3600,
        )
    )
    provider = {
        "id": "google-broker",
        "name": "Google (Broker)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "bearer_token": "",
        "auth_source": "broker:google",
    }
    headers = await _build_auth_header_async(provider)
    assert headers.get("Authorization") == "Bearer broker-access-token"


async def test_router_broker_raises_when_disconnected(_oauth_db):
    """No stored credential for the broker source must raise a clear error."""
    from plugboard.router import _build_auth_header_async

    provider = {
        "id": "gh-broker",
        "name": "GH",
        "base_url": "https://x",
        "bearer_token": "",
        "auth_source": "broker:github",
    }
    with pytest.raises(RuntimeError, match="Auth not available"):
        await _build_auth_header_async(provider)


# ─── Admin HTTP endpoints ───────────────────────────────────────────────────


async def test_oauth_connections_list_endpoint(admin_client, _oauth_db):
    oauth_store.save_credential(
        OAuthCredential(
            source="google",
            account="a@x",
            access_token="tok",
            expires_at=time.time() + 3600,
            scope="openid",
        )
    )
    res = await admin_client.get(
        "/admin/oauth/connections",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    data = res.json()
    sources = [c["source"] for c in data["connections"]]
    assert "google" in sources


async def test_oauth_disconnect_endpoint(admin_client, _oauth_db):
    oauth_store.save_credential(
        OAuthCredential(
            source="github",
            account="gh",
            access_token="t",
        )
    )
    res = await admin_client.delete(
        "/admin/oauth/github",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 204
    assert oauth_store.get_credential("github") is None


async def test_oauth_start_rejects_non_broker_preset(admin_client, _oauth_db):
    """Asking to start OAuth for a CLI or API-key preset must 422."""
    res = await admin_client.post(
        "/admin/oauth/start",
        json={"preset": "anthropic"},  # API-key preset
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 422


async def test_oauth_start_github_returns_device_code(admin_client, _oauth_db):
    from aioresponses import aioresponses

    with aioresponses() as m:
        m.post(
            "https://github.com/login/device/code",
            status=200,
            payload={
                "device_code": "dc",
                "user_code": "ABCD-1234",
                "verification_uri": "https://github.com/login/device",
                "interval": 5,
                "expires_in": 900,
            },
        )
        res = await admin_client.post(
            "/admin/oauth/start",
            json={"preset": "github-broker"},
            headers={"Authorization": "Bearer adminkey"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["flow"] == "device"
    assert data["user_code"] == "ABCD-1234"
    assert data["state"]
