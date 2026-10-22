"""Admin MFA — RFC 6238 TOTP with Fernet-encrypted secret store (TM-001).

Single-admin model (the product has one ADMIN_KEY). Secret lives in
``data/admin_mfa.json`` encrypted via the same Fernet key that protects
provider bearer tokens. Verification tolerates ±1 30-second window for
clock skew.

Public surface:
    generate_secret() -> str           # new base32 secret
    provisioning_uri(secret, ...)      # otpauth:// URI for authenticator apps
    is_enrolled() -> bool              # secret exists on disk
    enroll(secret) -> None             # persist a secret (one call closes it)
    verify(code) -> bool               # validate a 6-digit code
    disable() -> None                  # delete the stored secret
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets as _secrets
import struct
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from plugboard.config import _decrypt_token, _encrypt_token
from plugboard.storage import atomic_write_json, data_dir

log = logging.getLogger(__name__)

_DATA_DIR = data_dir()
_MFA_FILE = _DATA_DIR / "admin_mfa.json"

_DIGITS = 6
_PERIOD = 30
_WINDOW = 1  # accept codes from ±_WINDOW time steps (±30 s)


# ── TOTP primitives (RFC 6238 / HOTP RFC 4226) ──────────────────────────────


def _b32_normalize(secret: str) -> bytes:
    """Decode a base32 secret tolerant of casing, padding, and whitespace."""
    stripped = secret.strip().replace(" ", "").upper()
    pad = (-len(stripped)) % 8
    return base64.b32decode(stripped + "=" * pad)


def _hotp(secret_bytes: bytes, counter: int) -> str:
    msg = struct.pack(">Q", counter)
    # RFC 4226/6238 HOTP/TOTP is defined over HMAC-SHA1 and authenticator apps
    # default to it; HMAC-SHA1 is not affected by SHA-1 collision attacks.
    digest = hmac.new(secret_bytes, msg, hashlib.sha1).digest()  # NOSONAR
    offset = digest[-1] & 0x0F
    code_int = (
        ((digest[offset] & 0x7F) << 24)
        | (digest[offset + 1] << 16)
        | (digest[offset + 2] << 8)
        | digest[offset + 3]
    )
    return str(code_int % (10**_DIGITS)).zfill(_DIGITS)


def _generate_code(secret: str, at: Optional[float] = None) -> str:
    """Compute the current TOTP for a secret (for tests and CLI bootstrap)."""
    t = int((at if at is not None else time.time()) // _PERIOD)
    return _hotp(_b32_normalize(secret), t)


def generate_secret(byte_len: int = 20) -> str:
    """Return a fresh base32 secret (RFC 6238 recommends ≥ 128 bits → 16 B)."""
    return base64.b32encode(_secrets.token_bytes(byte_len)).decode("ascii").rstrip("=")


def provisioning_uri(secret: str, account: str, issuer: str = "Plugboard") -> str:
    """Build an otpauth:// URI that authenticator apps can read via QR or paste."""
    label = f"{issuer}:{account}"
    params = f"secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={_DIGITS}&period={_PERIOD}"
    return f"otpauth://totp/{quote(label)}?{params}"


# ── Persistence ─────────────────────────────────────────────────────────────


def _read() -> dict:
    if not _MFA_FILE.exists():
        return {}
    try:
        return json.loads(_MFA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("Failed to read %s: %s", _MFA_FILE, exc)
        return {}


def _write(data: dict) -> None:
    atomic_write_json(_MFA_FILE, data, mode=0o600)


def is_enrolled() -> bool:
    return bool(_read().get("secret_enc"))


def enroll(secret: str) -> None:
    """Persist the Fernet-encrypted secret. Overwrites any existing secret."""
    _write(
        {
            "secret_enc": _encrypt_token(secret),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_used_at": None,
        }
    )


def disable() -> None:
    """Remove the stored secret and its metadata. Idempotent."""
    try:
        _MFA_FILE.unlink()
    except FileNotFoundError:
        pass


def _stored_secret() -> Optional[str]:
    data = _read()
    enc = data.get("secret_enc")
    if not enc:
        return None
    try:
        return _decrypt_token(enc)
    except Exception as exc:  # pragma: no cover — decrypt already logs
        log.error("Failed to decrypt admin MFA secret: %s", exc)
        return None


def verify(code: str, at: Optional[float] = None) -> bool:
    """Constant-time compare the supplied code against the stored secret.

    Accepts codes from the current, previous, and next time-step to tolerate
    small clock skew between the admin's device and the server.
    """
    if not code or not code.isdigit() or len(code) != _DIGITS:
        return False
    secret = _stored_secret()
    if not secret:
        return False
    secret_bytes = _b32_normalize(secret)
    t_now = int((at if at is not None else time.time()) // _PERIOD)
    for step in range(-_WINDOW, _WINDOW + 1):
        candidate = _hotp(secret_bytes, t_now + step)
        if hmac.compare_digest(candidate, code):
            # Update last_used_at (best effort — failure must not reject)
            try:
                data = _read()
                data["last_used_at"] = datetime.now(timezone.utc).isoformat()
                _write(data)
            except Exception:  # pragma: no cover
                pass
            return True
    return False


def is_required() -> bool:
    """MFA is enforced when the operator opts in AND a secret is enrolled.

    Requiring enrolment before enforcement prevents a foot-gun: setting
    the flag on a fresh deploy with no secret would lock the admin out.
    """
    flag = os.environ.get("PLUGBOARD_ADMIN_MFA_REQUIRED", "").strip() == "1"
    return flag and is_enrolled()
