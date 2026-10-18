"""Provider configuration store. Reads/writes providers.json."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import stat
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from cryptography.fernet import Fernet
from plugboard.storage import (
    StoreUnreadableError,
    atomic_write_json,
    atomic_write_text,
    data_dir,
)

# Load `.env` as early as possible so the encryption-key resolver (and any
# other module-level constant that reads the environment) sees the same
# values regardless of the boot path (run.py, `uvicorn plugboard.app`,
# scripts/*). Without this, a direct `python -m uvicorn ...` boots with an
# empty env, falls back to the auto-generated `data/.encryption_key` file,
# and then cannot decrypt tokens that were written with the env-derived key.
#
# `PLUGBOARD_SKIP_DOTENV=1` is an explicit escape hatch for test suites
# that inject their own environment — without it, the real `.env` leaks
# values like `API_KEY=""` and `CORS_ORIGINS=*` into every test process,
# breaking fixtures that depend on a clean environment.
_DOTENV_PATH = ""
if os.environ.get("PLUGBOARD_SKIP_DOTENV") != "1":
    try:
        from dotenv import find_dotenv, load_dotenv

        # Same lookup load_dotenv() runs from this module; kept so the
        # startup check below can audit the file that was actually loaded.
        _DOTENV_PATH = find_dotenv()
        load_dotenv()
        # .env may still use the pre-rename variable names.
        from plugboard import envcompat

        envcompat.apply()
    except ImportError:  # pragma: no cover — python-dotenv is a direct dep
        pass

log = logging.getLogger(__name__)


def dotenv_permission_warning(path: Optional[str] = None) -> Optional[str]:
    """Return a warning when `.env` is accessible beyond its owner.

    `.env` holds ADMIN_KEY and ENCRYPTION_KEY: any other local account (or
    process running as one) that can read it owns the gateway. Expected mode
    is 0600. Defaults to the `.env` loaded at import; returns None when there
    is none, or on Windows where POSIX modes do not apply.
    """
    target = path or _DOTENV_PATH
    if not target or os.name == "nt":
        return None
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        return None
    if not mode & 0o077:
        return None
    return (
        f"{target} is accessible to group/other users (mode {mode:04o}) but holds "
        f"ADMIN_KEY and ENCRYPTION_KEY. Restrict it: chmod 600 {target}"
    )


# ── Token encryption ─────────────────────────────────────────────────────────
#
# Behaviour (audit 2026-04-21 fix) :
#
# 1. If ENCRYPTION_KEY is set in env → use it (highest priority, allows
#    operators to manage the key via their secret manager).
# 2. Else → look for `data/.encryption_key`. If absent, auto-generate a
#    fresh Fernet key and write it with perms 0600. The key is then
#    reused on every restart so tokens encrypted on write remain
#    readable.
# 3. Legacy plain-text tokens (no `enc:` prefix) are still readable for
#    backward compat; they become encrypted on the next write.
#
# Net result : provider tokens are ALWAYS encrypted on disk once the
# server has booted once, without requiring operator action.

_DATA_DIR = data_dir()
PROVIDERS_FILE = _DATA_DIR / "providers.json"
PROVIDERS_LOCK = _DATA_DIR / "providers.json.lock"
_KEY_FILE = _DATA_DIR / ".encryption_key"


def _load_or_create_encryption_key() -> str:
    """Resolve the encryption key per the three-tier strategy above.

    Returns the raw key string. Never returns empty — we always either
    honour the env var or materialise a fresh key on disk.
    """
    env_key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key
    try:
        if _KEY_FILE.exists():
            stored = _KEY_FILE.read_text(encoding="utf-8").strip()
            if stored:
                return stored
    except OSError as exc:  # pragma: no cover — FS oddities
        log.warning("Cannot read %s (%s) — regenerating", _KEY_FILE, exc)

    # Generate a fresh Fernet key (already urlsafe base64) and persist it.
    new_key = Fernet.generate_key().decode("ascii")
    try:
        atomic_write_text(_KEY_FILE, new_key, mode=0o600)
        log.info(
            "Generated encryption key at %s (perms 0600). "
            "Set ENCRYPTION_KEY env to override or back it up to your secret manager.",
            _KEY_FILE,
        )
    except OSError as exc:
        log.error(
            "Failed to persist auto-generated encryption key to %s: %s. "
            "Tokens will be encrypted for THIS process only and lost on restart.",
            _KEY_FILE,
            exc,
        )
    return new_key


_ENCRYPTION_KEY = _load_or_create_encryption_key()


def _get_fernet() -> Fernet:
    """Return a Fernet instance. Always non-None post-boot."""
    # If the env key isn't already a valid Fernet key (legacy string),
    # derive one via SHA-256 (same as before). An auto-generated key
    # is already Fernet-shaped and decodes directly.
    try:
        return Fernet(_ENCRYPTION_KEY.encode("ascii"))
    except Exception:
        derived = base64.urlsafe_b64encode(
            hashlib.sha256(_ENCRYPTION_KEY.encode()).digest()
        )
        return Fernet(derived)


def _encrypt_token(token: str) -> str:
    """Encrypt a token for at-rest storage. Always encrypts post-boot."""
    return "enc:" + _get_fernet().encrypt(token.encode()).decode()


def _decrypt_token(stored: str) -> str:
    """Decrypt a stored token, tolerating legacy plain-text values."""
    if not stored.startswith("enc:"):
        return stored  # legacy plain text — will be re-encrypted on next write
    try:
        return _get_fernet().decrypt(stored[4:].encode()).decode()
    except Exception as exc:
        log.warning(
            "Failed to decrypt stored token (key rotated? corrupted value?): %s",
            exc,
        )
        return stored


try:
    from filelock import FileLock as _FileLock

    def _lock():
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        return _FileLock(str(PROVIDERS_LOCK))
except ImportError:
    from contextlib import contextmanager

    @contextmanager
    def _lock():
        yield


def _slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ── Provider destination allowlist (TM-004) ────────────────────────────────
#
# Environment-driven. When PLUGBOARD_PROVIDER_DOMAINS_ALLOWED is non-empty
# every create/update of a provider is gated: the base_url host must match
# an entry in the list. This caps the blast radius of a compromised admin
# (or a buggy operator) who would otherwise redirect client prompts to an
# attacker-controlled endpoint.
#
# Syntax:
#   PLUGBOARD_PROVIDER_DOMAINS_ALLOWED="api.openai.com,.anthropic.com,localhost"
#   - bare hostname         → exact match
#   - leading-dot wildcard  → matches the domain and any subdomain
#   - localhost / 127.0.0.1 must be listed explicitly if you run local
#     providers (e.g. OpenClaw on :18789, Ollama on :11434).
#
# Empty/unset → feature disabled (backward compatible default).


class ProviderDomainNotAllowed(ValueError):
    """Raised when a provider base_url points to a non-allowlisted host."""


def _allowed_provider_domains() -> List[str]:
    raw = os.environ.get("PLUGBOARD_PROVIDER_DOMAINS_ALLOWED", "").strip()
    if not raw:
        return []
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def check_provider_domain_allowed(base_url: str) -> None:
    """Validate a base_url against the allowlist. No-op when unset."""
    allowed = _allowed_provider_domains()
    if not allowed:
        return
    from urllib.parse import urlparse

    host = ""
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        raise ProviderDomainNotAllowed(f"base_url {base_url!r} has no parseable host")
    for entry in allowed:
        if entry.startswith("."):
            suffix = entry  # ".example.com"
            root = entry[1:]  # "example.com"
            if host == root or host.endswith(suffix):
                return
        elif host == entry:
            return
    raise ProviderDomainNotAllowed(
        f"provider host {host!r} not in PLUGBOARD_PROVIDER_DOMAINS_ALLOWED"
    )


def _read() -> Dict[str, Any]:
    if not PROVIDERS_FILE.exists():
        return {"providers": []}
    try:
        return json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Failed to read providers.json: %s", exc)
        return {"providers": []}


def _write(data: Dict[str, Any]) -> None:
    # Atomic, owner-only (provider tokens live here), and never over an
    # unreadable file that _read() reported as empty.
    atomic_write_json(PROVIDERS_FILE, data, mode=0o600)


# ── Public API ────────────────────────────────────────────────────────────────


def load_providers() -> List[Dict[str, Any]]:
    with _lock():
        data = _read()
        providers = data.get("providers", [])
        if not providers:
            oc_url = os.environ.get("OPENCLAW_URL", "").strip()
            oc_key = os.environ.get("OPENCLAW_KEY", "").strip()
            if oc_url and oc_key:
                oc_model = (
                    os.environ.get("OPENCLAW_MODEL") or "claude-sonnet-4-6"
                ).strip()
                oc_name = (
                    os.environ.get("OPENCLAW_NAME", "OpenClaw").strip() or "OpenClaw"
                )
                record: Dict[str, Any] = {
                    "id": _slugify(oc_name),
                    "name": oc_name,
                    "base_url": oc_url.rstrip("/"),
                    "bearer_token": _encrypt_token(oc_key) if oc_key else "",
                    "default_model": oc_model,
                    "is_active": True,
                    "provider_type": "cloud",
                    "auth_header_name": "Authorization",
                    "auth_header_prefix": "Bearer",
                    "auth_source": "",
                    "timeout": 60.0,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                data["providers"] = [record]
                try:
                    _write(data)
                except StoreUnreadableError:
                    # providers.json exists but cannot be parsed: keep it for
                    # the operator to fix and serve the bootstrap provider from
                    # memory instead of failing every request.
                    pass
                providers = data["providers"]
        for p in providers:
            if "bearer_token" in p:
                p["bearer_token"] = _decrypt_token(p["bearer_token"])
        return providers


def get_provider(provider_id: str) -> Optional[Dict[str, Any]]:
    for p in load_providers():
        if p.get("id") == provider_id:
            return p
    return None


def create_provider(
    name: str,
    base_url: str,
    bearer_token: str,
    default_model: str = "",
    is_active: bool = True,
    timeout: float = 60.0,
    provider_type: str = "cloud",
    auth_header_name: str = "Authorization",
    auth_header_prefix: str = "Bearer",
    auth_source: str = "",
) -> Dict[str, Any]:
    check_provider_domain_allowed(base_url)
    provider_id = _slugify(name)
    with _lock():
        data = _read()
        existing = {p["id"] for p in data.get("providers", [])}
        base, ctr = provider_id, 1
        while provider_id in existing:
            provider_id = f"{base}-{ctr}"
            ctr += 1
        record: Dict[str, Any] = {
            "id": provider_id,
            "name": name,
            "base_url": base_url.rstrip("/"),
            "bearer_token": _encrypt_token(bearer_token) if bearer_token else "",
            "default_model": default_model,
            "is_active": is_active,
            "provider_type": provider_type,
            "auth_header_name": auth_header_name,
            "auth_header_prefix": auth_header_prefix,
            "auth_source": auth_source,
            "timeout": timeout,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data.setdefault("providers", []).append(record)
        _write(data)
    return record


def update_provider(
    provider_id: str, updates: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    _IMMUTABLE = {"id", "created_at"}
    # Validate the new base_url BEFORE taking the lock so we surface
    # ProviderDomainNotAllowed without leaving the file partially written.
    if "base_url" in updates and isinstance(updates["base_url"], str):
        check_provider_domain_allowed(updates["base_url"])
    with _lock():
        data = _read()
        for i, p in enumerate(data.get("providers", [])):
            if p["id"] == provider_id:
                for k, v in updates.items():
                    if k not in _IMMUTABLE:
                        if k == "base_url" and isinstance(v, str):
                            v = v.rstrip("/")
                        if k == "bearer_token" and isinstance(v, str):
                            v = _encrypt_token(v)
                        data["providers"][i][k] = v
                _write(data)
                return data["providers"][i]
    return None


def is_cli_provider(provider: Dict[str, Any]) -> bool:
    """Provider served by a local CLI (subprocess or CLI token file)."""
    auth_source = provider.get("auth_source", "") or ""
    return auth_source.startswith(("subprocess:", "cli:"))


def modify_provider(
    provider_id: str, fn: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]
) -> Optional[Dict[str, Any]]:
    """Read-modify-write one provider under the store lock.

    ``fn`` receives a copy of the stored record (token still encrypted) and
    returns the fields to change, or None to leave the record as it is. Only
    use it for fields that are not secrets (models, default_model...).
    Returns the stored record after the change, or None if unknown.
    """
    with _lock():
        data = _read()
        for i, p in enumerate(data.get("providers", [])):
            if p.get("id") != provider_id:
                continue
            updates = fn(dict(p))
            if updates:
                for k, v in updates.items():
                    if k not in {"id", "created_at", "bearer_token", "base_url"}:
                        data["providers"][i][k] = v
                _write(data)
            return data["providers"][i]
    return None


def delete_provider(provider_id: str) -> bool:
    with _lock():
        data = _read()
        before = len(data.get("providers", []))
        data["providers"] = [
            p for p in data.get("providers", []) if p["id"] != provider_id
        ]
        if len(data["providers"]) == before:
            return False
        _write(data)
    return True


def mask_token(token: str) -> str:
    if len(token) <= 8:
        return "***"
    return token[:4] + "***" + token[-4:]
