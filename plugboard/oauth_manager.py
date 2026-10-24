"""
OAuth flow orchestration — Authorization Code + PKCE and Device Code.

Supported providers (see OAUTH_CONFIGS):
- google  → Authorization Code + PKCE (Gemini via OpenAI-compat endpoint)
- github  → Device Code (Copilot + Models)
- openai  → Authorization Code + PKCE (ChatGPT via Codex CLI public client_id)

Each flow:
1. start_flow(source)      → returns auth_url or user_code to show user
2. complete_callback(...)  → exchange code for tokens (PKCE)
   poll_device(state)       → poll Device Flow until user authorizes
3. refresh(source)          → refresh expired access_token using refresh_token

Tokens are stored via oauth_store.save_credential().
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import aiohttp

from plugboard.oauth_store import (
    OAuthCredential,
    OAuthFlow,
    credential_version,
    delete_flow,
    get_credential,
    get_flow,
    replace_credential,
    save_credential,
    save_flow,
)

log = logging.getLogger(__name__)


# ── Provider OAuth configs ──────────────────────────────────────────────────

# Default callback URL. For prod, set PLUGBOARD_OAUTH_CALLBACK_BASE=https://<domain>
_CALLBACK_BASE = os.environ.get(
    "PLUGBOARD_OAUTH_CALLBACK_BASE",
    "http://localhost:8765",
).rstrip("/")


def callback_url(source: str) -> str:
    return f"{_CALLBACK_BASE}/oauth/callback/{source}"


OAUTH_CONFIGS: Dict[str, Dict[str, Any]] = {
    "google": {
        "flow": "authorization_code_pkce",
        # Google OAuth desktop client (loopback redirect). Not shipped: set
        # GEMINI_OAUTH_CLIENT_ID / GEMINI_OAUTH_CLIENT_SECRET (see the guide).
        "client_id": os.environ.get("GEMINI_OAUTH_CLIENT_ID", ""),
        "client_secret": os.environ.get("GEMINI_OAUTH_CLIENT_SECRET", ""),
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": [
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/cloud-platform",
        ],
        "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
        "account_field": "email",
        "provider_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-flash",
    },
    "github": {
        "flow": "device_code",
        # Public client_id used by GitHub Copilot CLI / gh-copilot
        "client_id": "Iv1.b507a08c87ecfe98",
        "device_code_url": "https://github.com/login/device/code",
        "token_url": "https://github.com/login/oauth/access_token",
        "scopes": ["read:user"],
        "userinfo_url": "https://api.github.com/user",
        "account_field": "login",
    },
    "openai": {
        "flow": "authorization_code_pkce",
        # Public client_id of the OpenAI Codex CLI desktop app.
        # Loopback redirect is supported.
        "client_id": "pdlLIX2Y72MIl2rhLhTE9VV9bN905kBh",
        "auth_url": "https://auth.openai.com/oauth/authorize",
        "token_url": "https://auth.openai.com/oauth/token",
        "scopes": ["openid", "profile", "email", "offline_access"],
        "userinfo_url": "https://api.openai.com/v1/me",
        "account_field": "email",
        "provider_base_url": "https://chatgpt.com/backend-api/codex",
        "default_model": "gpt-5.4",
    },
}


class OAuthClientNotConfigured(ValueError, RuntimeError):
    """The OAuth client for this source is not configured (env vars unset).

    A RuntimeError too, so the router's fallback chain moves on to the next
    provider instead of failing the request.
    """


def get_config(source: str) -> Dict[str, Any]:
    cfg = OAUTH_CONFIGS.get(source)
    if cfg is None:
        raise ValueError(f"Unknown OAuth source: {source}")
    if not cfg.get("client_id"):
        raise OAuthClientNotConfigured(
            f"OAuth source {source!r} has no client configured "
            "(set GEMINI_OAUTH_CLIENT_ID and GEMINI_OAUTH_CLIENT_SECRET)"
        )
    return cfg


# ── PKCE helpers ─────────────────────────────────────────────────────────────


def _gen_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636."""
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


# ── Flow start ───────────────────────────────────────────────────────────────


@dataclass
class FlowStartResult:
    flow: str  # "authorization_code" | "device"
    state: str
    auth_url: str = ""  # for Authorization Code
    user_code: str = ""  # for Device Flow
    verification_uri: str = ""  # for Device Flow
    interval: int = 5  # for Device Flow polling
    expires_in: int = 600


async def start_flow(source: str) -> FlowStartResult:
    """Start an OAuth flow. Returns data for the UI to display."""
    cfg = get_config(source)
    state = secrets.token_urlsafe(32)
    expires_at = time.time() + 600

    if cfg["flow"] == "authorization_code_pkce":
        verifier, challenge = _gen_pkce()
        save_flow(
            OAuthFlow(
                state=state,
                source=source,
                code_verifier=verifier,
                flow_type="authorization_code",
                expires_at=expires_at,
            )
        )
        params = {
            "client_id": cfg["client_id"],
            "redirect_uri": callback_url(source),
            "response_type": "code",
            "scope": " ".join(cfg["scopes"]),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",  # Google: get refresh_token
            "prompt": "consent",  # force new refresh_token
        }
        auth_url = cfg["auth_url"] + "?" + urlencode(params)
        return FlowStartResult(
            flow="authorization_code",
            state=state,
            auth_url=auth_url,
        )

    if cfg["flow"] == "device_code":
        async with aiohttp.ClientSession() as session:
            async with session.post(
                cfg["device_code_url"],
                data={"client_id": cfg["client_id"], "scope": " ".join(cfg["scopes"])},
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"Device code request failed: {resp.status} {text[:200]}"
                    )
                data = await resp.json()
        device_code = data.get("device_code", "")
        user_code = data.get("user_code", "")
        verification_uri = data.get("verification_uri", "")
        interval = int(data.get("interval", 5))
        expires_in = int(data.get("expires_in", 900))
        save_flow(
            OAuthFlow(
                state=state,
                source=source,
                device_code=device_code,
                flow_type="device",
                expires_at=time.time() + expires_in,
            )
        )
        return FlowStartResult(
            flow="device",
            state=state,
            user_code=user_code,
            verification_uri=verification_uri,
            interval=interval,
            expires_in=expires_in,
        )

    raise ValueError(f"Unsupported flow: {cfg['flow']}")


# ── Callback (Authorization Code) ────────────────────────────────────────────


async def complete_callback(source: str, state: str, code: str) -> OAuthCredential:
    """Exchange an authorization code for tokens. Stores and returns credential."""
    flow = get_flow(state)
    if flow is None:
        raise RuntimeError("Invalid or expired state")
    if flow.source != source:
        raise RuntimeError("State source mismatch")

    cfg = get_config(source)
    payload = {
        "client_id": cfg["client_id"],
        "code": code,
        "redirect_uri": callback_url(source),
        "grant_type": "authorization_code",
        "code_verifier": flow.code_verifier,
    }
    if cfg.get("client_secret"):
        payload["client_secret"] = cfg["client_secret"]

    async with aiohttp.ClientSession() as session:
        async with session.post(
            cfg["token_url"],
            data=payload,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(
                    f"Token exchange failed ({resp.status}): {text[:200]}"
                )
            tokens = await resp.json()

    account = await _fetch_account(source, tokens.get("access_token", ""))
    cred = _build_credential(source, account, tokens)
    save_credential(cred)
    delete_flow(state)
    return cred


# ── Device poll ──────────────────────────────────────────────────────────────


async def poll_device(state: str) -> Dict[str, Any]:
    """Poll device flow. Returns {status: pending|success|denied|expired, ...}."""
    flow = get_flow(state)
    if flow is None:
        return {"status": "expired"}
    if flow.flow_type != "device":
        return {"status": "error", "error": "Not a device flow"}

    cfg = get_config(flow.source)
    async with aiohttp.ClientSession() as session:
        async with session.post(
            cfg["token_url"],
            data={
                "client_id": cfg["client_id"],
                "device_code": flow.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()

    if "access_token" in data:
        account = await _fetch_account(flow.source, data["access_token"])
        cred = _build_credential(flow.source, account, data)
        save_credential(cred)
        delete_flow(state)
        return {"status": "success", "account": account, "source": flow.source}

    error = data.get("error", "unknown")
    if error == "authorization_pending":
        return {"status": "pending"}
    if error == "slow_down":
        return {"status": "pending", "slow_down": True}
    if error == "expired_token":
        delete_flow(state)
        return {"status": "expired"}
    if error == "access_denied":
        delete_flow(state)
        return {"status": "denied"}
    return {"status": "error", "error": error}


# ── Refresh ──────────────────────────────────────────────────────────────────


async def refresh(source: str) -> Optional[OAuthCredential]:
    """Refresh an expired access_token using refresh_token.

    The result is saved only if the stored credential did not change during
    the request: a disconnect or a new account wins over a refresh in flight.
    """
    # Version first: a write between the two reads makes the refresh discarded.
    version = credential_version(source)
    cred = get_credential(source)
    if cred is None or not cred.refresh_token:
        return None
    cfg = get_config(source)
    payload = {
        "client_id": cfg["client_id"],
        "refresh_token": cred.refresh_token,
        "grant_type": "refresh_token",
    }
    if cfg.get("client_secret"):
        payload["client_secret"] = cfg["client_secret"]

    async with aiohttp.ClientSession() as session:
        async with session.post(
            cfg["token_url"],
            data=payload,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.warning(
                    "Refresh failed for %s: HTTP %d — %s",
                    source,
                    resp.status,
                    text[:200],
                )
                return None
            tokens = await resp.json()

    # Some providers don't return a new refresh_token on refresh — preserve old one
    tokens.setdefault("refresh_token", cred.refresh_token)
    new_cred = _build_credential(source, cred.account, tokens)
    if not replace_credential(new_cred, version):
        log.info("Refresh for %s discarded: the credential changed meanwhile", source)
        return None
    return new_cred


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _fetch_account(source: str, access_token: str) -> str:
    """Fetch the user's email/login from the provider's userinfo endpoint."""
    cfg = get_config(source)
    url = cfg.get("userinfo_url")
    if not url or not access_token:
        return ""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
        return str(data.get(cfg.get("account_field", "email"), ""))
    except Exception as exc:
        log.debug("userinfo fetch failed for %s: %s", source, exc)
        return ""


def _build_credential(
    source: str, account: str, tokens: Dict[str, Any]
) -> OAuthCredential:
    """Construct an OAuthCredential from a token response."""
    expires_in = tokens.get("expires_in")
    expires_at = (time.time() + int(expires_in)) if expires_in else None
    extra = {
        k: v
        for k, v in tokens.items()
        if k not in {"access_token", "refresh_token", "expires_in", "scope"}
    }
    return OAuthCredential(
        source=source,
        account=account or "",
        access_token=tokens.get("access_token", ""),
        refresh_token=tokens.get("refresh_token", ""),
        expires_at=expires_at,
        scope=tokens.get("scope", ""),
        extra=extra,
    )
