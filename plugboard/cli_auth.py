"""
CLI auth loaders — delegate OAuth to official CLIs (Codex, Gemini, gh, Claude).

Instead of re-implementing OAuth flows, we read the tokens that the user's
installed CLIs manage. This matches OpenClaw's strategy: the CLIs handle
login/refresh, we just consume their tokens.

Supported sources:
- cli:codex      → ~/.codex/auth.json         (ChatGPT via OpenAI Codex CLI)
- cli:gemini     → ~/.gemini/oauth_creds.json (Google Gemini CLI)
- cli:gh         → `gh auth token`            (GitHub Models — uses PAT as-is)
- cli:gh+copilot → GH PAT + exchange          (GitHub Copilot — ephemeral token)
- cli:claude     → macOS Keychain             (roadmap — claude-cli OAuth)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """Raised when authentication fails with a specific HTTP status."""

    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


# ── Token bundle ─────────────────────────────────────────────────────────────


@dataclass
class TokenBundle:
    """A resolved access token + metadata."""

    access_token: str
    expires_at: Optional[float] = None  # unix timestamp (seconds)
    account: str = ""  # email or username for display
    meta: Dict[str, Any] = field(default_factory=dict)

    def is_valid(self, skew: int = 60) -> bool:
        """True if token is non-empty and not expired (with safety skew)."""
        if not self.access_token:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > time.time() + skew


# ── Base class ───────────────────────────────────────────────────────────────


# A token without expires_at is re-read from its source after this delay, so
# a revocation made elsewhere (another process, a manual edit) is seen.
NO_EXPIRY_CACHE_TTL = 300.0


class CLIAuthLoader:
    """Base class for all CLI auth loaders."""

    source_id: str = ""
    # Credential-store source this loader reads ("" for external CLIs).
    _broker_source: str = ""

    def __init__(self) -> None:
        self._cache: Optional[TokenBundle] = None
        self._cached_at = 0.0

    def available(self) -> Dict[str, Any]:
        """Return availability info: {ok: bool, account: str, error: str}."""
        raise NotImplementedError

    def _fetch_token(self) -> Optional[TokenBundle]:
        """Read fresh token from source. Override in subclass."""
        raise NotImplementedError

    def _store(self, bundle: Optional[TokenBundle]) -> Optional[TokenBundle]:
        self._cache = bundle
        self._cached_at = time.monotonic()
        return bundle

    def _cached(self, skew: int = 60) -> Optional[TokenBundle]:
        """The cached token while it is valid and, without expiry, fresh."""
        cache = self._cache
        if not cache or not cache.is_valid(skew=skew):
            return None
        if (
            cache.expires_at is None
            and time.monotonic() - self._cached_at >= NO_EXPIRY_CACHE_TTL
        ):
            return None
        return cache

    def invalidate(self) -> None:
        """Forget the cached token: the next call reads the source again."""
        self._cache = None
        self._cached_at = 0.0

    async def get_token(self) -> Optional[TokenBundle]:
        """Return a valid token, refreshing if expired. Cached in memory."""
        cached = self._cached()
        if cached:
            return cached
        self._store(self._fetch_token())
        if self._cache and not self._cache.is_valid():
            refreshed = await self._refresh(self._cache)
            if refreshed:
                self._store(refreshed)
        return self._cache if (self._cache and self._cache.is_valid()) else None

    async def _refresh(self, expired: TokenBundle) -> Optional[TokenBundle]:
        """Refresh an expired token. Default: return None (subclass overrides)."""
        return None


# ── Codex (ChatGPT) ──────────────────────────────────────────────────────────


class CodexAuthLoader(CLIAuthLoader):
    """Read tokens from ~/.codex/auth.json (OpenAI Codex CLI)."""

    source_id = "codex"
    CONFIG_PATH = Path.home() / ".codex" / "auth.json"

    def available(self) -> Dict[str, Any]:
        if not self.CONFIG_PATH.exists():
            return {"ok": False, "error": "Codex CLI not logged in. Run 'codex login'."}
        try:
            data = json.loads(self.CONFIG_PATH.read_text())
            tokens = data.get("tokens", {})
            if not tokens.get("access_token"):
                return {"ok": False, "error": "No access_token in auth.json"}
            account_id = tokens.get("account_id", "")
            return {
                "ok": True,
                "account": account_id or "chatgpt-user",
                "mode": data.get("auth_mode", "chatgpt"),
            }
        except Exception as exc:
            return {"ok": False, "error": f"Read error: {exc}"}

    def _fetch_token(self) -> Optional[TokenBundle]:
        if not self.CONFIG_PATH.exists():
            return None
        try:
            data = json.loads(self.CONFIG_PATH.read_text())
            tokens = data.get("tokens", {})
            access = tokens.get("access_token")
            if not access:
                return None
            last_refresh = data.get("last_refresh", "")
            # Codex tokens last ~1h by default; be conservative
            expires_at = time.time() + 3000  # 50 min
            return TokenBundle(
                access_token=access,
                expires_at=expires_at,
                account=tokens.get("account_id", ""),
                meta={
                    "id_token": tokens.get("id_token"),
                    "refresh_token": tokens.get("refresh_token"),
                    "last_refresh": last_refresh,
                },
            )
        except Exception as exc:
            log.warning("Codex auth read failed: %s", exc)
            return None


# ── Gemini (Google) ──────────────────────────────────────────────────────────


class GeminiAuthLoader(CLIAuthLoader):
    """Read tokens from ~/.gemini/oauth_creds.json (Google Gemini CLI)."""

    source_id = "gemini"
    CONFIG_PATH = Path.home() / ".gemini" / "oauth_creds.json"
    ACCOUNTS_PATH = Path.home() / ".gemini" / "google_accounts.json"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
    # OAuth client the Gemini CLI login was made with; needed only to refresh
    # an expired token. Not shipped: set it in the environment.
    CLIENT_ID = os.environ.get("GEMINI_OAUTH_CLIENT_ID", "")
    CLIENT_SECRET = os.environ.get("GEMINI_OAUTH_CLIENT_SECRET", "")

    def available(self) -> Dict[str, Any]:
        if not self.CONFIG_PATH.exists():
            return {
                "ok": False,
                "error": "Gemini CLI not logged in. Run 'gemini auth login'.",
            }
        try:
            data = json.loads(self.CONFIG_PATH.read_text())
            if not data.get("access_token"):
                return {"ok": False, "error": "No access_token in oauth_creds.json"}
            account = ""
            if self.ACCOUNTS_PATH.exists():
                acc_data = json.loads(self.ACCOUNTS_PATH.read_text())
                account = acc_data.get("active", "")
            return {"ok": True, "account": account or "gemini-user"}
        except Exception as exc:
            return {"ok": False, "error": f"Read error: {exc}"}

    def _fetch_token(self) -> Optional[TokenBundle]:
        if not self.CONFIG_PATH.exists():
            return None
        try:
            data = json.loads(self.CONFIG_PATH.read_text())
            access = data.get("access_token")
            if not access:
                return None
            expiry_ms = data.get("expiry_date", 0)
            expires_at = expiry_ms / 1000 if expiry_ms else None
            account = ""
            if self.ACCOUNTS_PATH.exists():
                try:
                    account = json.loads(self.ACCOUNTS_PATH.read_text()).get(
                        "active", ""
                    )
                except Exception:
                    pass
            return TokenBundle(
                access_token=access,
                expires_at=expires_at,
                account=account,
                meta={
                    "refresh_token": data.get("refresh_token"),
                    "scope": data.get("scope", ""),
                },
            )
        except Exception as exc:
            log.warning("Gemini auth read failed: %s", exc)
            return None

    async def _refresh(self, expired: TokenBundle) -> Optional[TokenBundle]:
        refresh_token = expired.meta.get("refresh_token")
        if not refresh_token:
            return None
        if not self.CLIENT_ID:
            log.warning(
                "Gemini token expired and GEMINI_OAUTH_CLIENT_ID is not set: "
                "run 'gemini' once to refresh it, or set the client variables"
            )
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.TOKEN_ENDPOINT,
                    data={
                        "client_id": self.CLIENT_ID,
                        "client_secret": self.CLIENT_SECRET,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        log.warning("Gemini refresh failed: HTTP %d", resp.status)
                        return None
                    data = await resp.json()
                    new_access = data.get("access_token")
                    if not new_access:
                        return None
                    expires_in = data.get("expires_in", 3600)
                    # Persist back to oauth_creds.json
                    try:
                        creds = json.loads(self.CONFIG_PATH.read_text())
                        creds["access_token"] = new_access
                        creds["expiry_date"] = int((time.time() + expires_in) * 1000)
                        # Atomic: a crash mid-write must not lose the refresh token.
                        from plugboard.storage import atomic_write_json

                        atomic_write_json(self.CONFIG_PATH, creds, mode=0o600)
                    except Exception as exc:
                        log.debug("Gemini creds write-back failed: %s", exc)
                    return TokenBundle(
                        access_token=new_access,
                        expires_at=time.time() + expires_in,
                        account=expired.account,
                        meta={**expired.meta, "refreshed_at": time.time()},
                    )
        except Exception as exc:
            log.warning("Gemini refresh exception: %s", exc)
            return None


# ── GitHub (gh CLI) ──────────────────────────────────────────────────────────


class GHAuthLoader(CLIAuthLoader):
    """Read GitHub PAT via `gh auth token` subprocess."""

    source_id = "gh"

    def available(self) -> Dict[str, Any]:
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return {"ok": False, "error": "gh not logged in. Run 'gh auth login'."}
            # Extract account from output
            account = ""
            for line in result.stderr.split("\n") + result.stdout.split("\n"):
                line = line.strip()
                if "Logged in to" in line and "account" in line:
                    parts = line.split("account")
                    if len(parts) > 1:
                        account = parts[1].strip().split()[0].rstrip("(")
                        break
            return {"ok": True, "account": account or "gh-user"}
        except FileNotFoundError:
            return {
                "ok": False,
                "error": "gh CLI not installed. Install from https://cli.github.com/",
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "gh command timed out"}
        except Exception as exc:
            return {"ok": False, "error": f"Check error: {exc}"}

    def _fetch_token(self) -> Optional[TokenBundle]:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            token = result.stdout.strip()
            if not token:
                return None
            # PATs don't expire (or have long expiry), check with 24h cache
            return TokenBundle(
                access_token=token,
                expires_at=time.time() + 86400,  # 24h cache
                account="gh-user",
                meta={"source": "gh auth token"},
            )
        except Exception as exc:
            log.warning("gh auth token failed: %s", exc)
            return None


# ── GitHub Copilot (ephemeral token from PAT) ────────────────────────────────


class GHCopilotAuthLoader(GHAuthLoader):
    """GitHub Copilot: exchange GH PAT for ephemeral Copilot token."""

    source_id = "gh+copilot"
    COPILOT_TOKEN_ENDPOINT = "https://api.github.com/copilot_internal/v2/token"

    async def get_token(self) -> Optional[TokenBundle]:
        """Override to always exchange PAT for fresh Copilot token."""
        cached = self._cached(skew=60)
        if cached:
            return cached
        # Get GH PAT first
        pat = self._fetch_token()
        if not pat:
            return None
        # Exchange for Copilot token
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.COPILOT_TOKEN_ENDPOINT,
                    headers={
                        "Authorization": f"token {pat.access_token}",
                        "User-Agent": "GitHubCopilotChat/0.26.7",
                        "Editor-Version": "vscode/1.99.3",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 403:
                        body_text = await resp.text()
                        log.warning(
                            "Copilot token exchange denied: HTTP %d — %s",
                            resp.status,
                            body_text[:200],
                        )
                        raise AuthError(
                            "GitHub PAT valid but Copilot license missing or not active.",
                            status_code=403,
                        )
                    if resp.status != 200:
                        log.warning(
                            "Copilot token exchange failed: HTTP %d", resp.status
                        )
                        return None
                    data = await resp.json()
                    copilot_token = data.get("token")
                    if not copilot_token:
                        return None
                    expires_at = data.get("expires_at", time.time() + 1800)
                    return self._store(
                        TokenBundle(
                            access_token=copilot_token,
                            expires_at=float(expires_at),
                            account=pat.account,
                            meta={
                                "refresh_in": data.get("refresh_in", 1500),
                                "endpoints": data.get("endpoints", {}),
                            },
                        )
                    )
        except AuthError:
            raise
        except Exception as exc:
            log.warning("Copilot token exchange exception: %s", exc)
            return None


# ── Claude (roadmap) ─────────────────────────────────────────────────────────


class ClaudeAuthLoader(CLIAuthLoader):
    """Read Claude CLI OAuth token from macOS Keychain (roadmap)."""

    source_id = "claude"

    def available(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": "Claude OAuth loader is on roadmap (use API key for now)",
        }

    def _fetch_token(self) -> Optional[TokenBundle]:
        return None


# ── Stored OAuth Broker (tokens managed by Plugboard itself) ───────────────


class StoredOAuthLoader(CLIAuthLoader):
    """Read tokens stored in credentials.db by the OAuth broker.

    Unlike CLI loaders that read from external CLI files, this loader reads
    tokens obtained via plugboard's own OAuth flows (dashboard "Connect"
    buttons). Auto-refreshes via oauth_manager.refresh() when expired.
    """

    def __init__(self, source: str) -> None:
        super().__init__()
        self.source_id = source
        self._broker_source = source

    def available(self) -> Dict[str, Any]:
        from plugboard.oauth_store import get_credential

        cred = get_credential(self._broker_source)
        if cred is None:
            return {
                "ok": False,
                "error": f"Not connected to {self._broker_source}. "
                "Connect via dashboard.",
            }
        return {"ok": True, "account": cred.account}

    def _fetch_token(self) -> Optional[TokenBundle]:
        from plugboard.oauth_store import get_credential

        cred = get_credential(self._broker_source)
        if cred is None or not cred.access_token:
            return None
        return TokenBundle(
            access_token=cred.access_token,
            expires_at=cred.expires_at,
            account=cred.account,
            meta={"refresh_token": cred.refresh_token, "extra": cred.extra},
        )

    async def _refresh(self, expired: TokenBundle) -> Optional[TokenBundle]:
        from plugboard.oauth_manager import refresh

        new_cred = await refresh(self._broker_source)
        if new_cred is None:
            return None
        return TokenBundle(
            access_token=new_cred.access_token,
            expires_at=new_cred.expires_at,
            account=new_cred.account,
            meta={"refresh_token": new_cred.refresh_token, "extra": new_cred.extra},
        )


class StoredOAuthCopilotLoader(StoredOAuthLoader):
    """GitHub OAuth broker + Copilot ephemeral token exchange.

    Reads the GitHub OAuth access_token from credentials.db (source='github'),
    then exchanges it for an ephemeral Copilot token via
    api.github.com/copilot_internal/v2/token (same as GHCopilotAuthLoader).
    """

    COPILOT_TOKEN_ENDPOINT = "https://api.github.com/copilot_internal/v2/token"

    def __init__(self) -> None:
        super().__init__("github")
        self.source_id = "broker:github+copilot"

    async def get_token(self) -> Optional[TokenBundle]:
        """Exchange stored GitHub OAuth token for Copilot ephemeral token."""
        cached = self._cached(skew=60)
        if cached:
            return cached
        from plugboard.oauth_store import get_credential
        from plugboard.oauth_manager import refresh

        cred = get_credential("github")
        if cred is None:
            return None
        if cred.is_expired():
            refreshed = await refresh("github")
            if refreshed is None:
                return None
            cred = refreshed

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.COPILOT_TOKEN_ENDPOINT,
                    headers={
                        "Authorization": f"token {cred.access_token}",
                        "User-Agent": "GitHubCopilotChat/0.26.7",
                        "Editor-Version": "vscode/1.99.3",
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 403:
                        body_text = await resp.text()
                        log.warning(
                            "Copilot token exchange denied: HTTP %d — %s",
                            resp.status,
                            body_text[:200],
                        )
                        raise AuthError(
                            "GitHub OAuth token valid but Copilot license missing or not active.",
                            status_code=403,
                        )
                    if resp.status != 200:
                        log.warning("Copilot exchange failed: HTTP %d", resp.status)
                        return None
                    data = await resp.json()
            copilot_token = data.get("token")
            if not copilot_token:
                return None
            # The credential may have been saved or deleted while the exchange
            # was in flight: never keep a token derived from a stale one.
            current = get_credential("github")
            if current is None or current.access_token != cred.access_token:
                return None
            expires_at = data.get("expires_at", time.time() + 1800)
            return self._store(
                TokenBundle(
                    access_token=copilot_token,
                    expires_at=float(expires_at),
                    account=cred.account,
                    meta={"refresh_in": data.get("refresh_in", 1500)},
                )
            )
        except AuthError:
            raise
        except Exception as exc:
            log.warning("Copilot exchange exception: %s", exc)
            return None


# ── Registry ─────────────────────────────────────────────────────────────────


_LOADERS: Dict[str, CLIAuthLoader] = {}


def get_loader(source: str) -> Optional[CLIAuthLoader]:
    """Get or create a loader for the given source (cached singleton).

    Sources:
    - cli:<name> style → subprocess-based CLI loaders (codex, gemini, gh, ...)
    - broker:<name> style → plugboard OAuth broker stored credentials

    For backward compat, the bare names (codex, gemini, gh, ...) are kept as
    aliases for the CLI loaders.
    """
    if source in _LOADERS:
        return _LOADERS[source]

    # Broker (plugboard stored OAuth)
    if source.startswith("broker:"):
        inner = source.removeprefix("broker:")
        if inner == "github+copilot":
            _LOADERS[source] = StoredOAuthCopilotLoader()
        elif inner in {"google", "github", "openai"}:
            _LOADERS[source] = StoredOAuthLoader(inner)
        else:
            return None
        return _LOADERS[source]

    # CLI-based (external CLIs)
    cls = {
        "codex": CodexAuthLoader,
        "gemini": GeminiAuthLoader,
        "gh": GHAuthLoader,
        "gh+copilot": GHCopilotAuthLoader,
        "claude": ClaudeAuthLoader,
    }.get(source)
    if cls is None:
        return None
    _LOADERS[source] = cls()
    return _LOADERS[source]


def reset_loaders() -> None:
    """Clear loader cache (useful for tests)."""
    _LOADERS.clear()


def invalidate_broker_source(source: str) -> None:
    """Drop cached tokens read from one credential-store source.

    Called on every write to the store, so a disconnect or a new account is
    used at once by ``broker:<source>`` and the loaders derived from it
    (``broker:github+copilot`` for ``github``). Other sources keep their cache.
    """
    for loader in list(_LOADERS.values()):
        if loader._broker_source == source:
            loader.invalidate()
