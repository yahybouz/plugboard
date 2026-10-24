"""Admin MFA — RFC 6238 TOTP with Fernet-encrypted secret store (TM-001).

Single-admin model (the product has one ADMIN_KEY). Secret lives in
``data/admin_mfa.json`` encrypted via the same Fernet key that protects
provider bearer tokens. Verification tolerates ±1 30-second window for
clock skew.

Public surface:
    generate_secret() -> str           # new base32 secret
    provisioning_uri(secret, ...)      # otpauth:// URI for authenticator apps
    is_enrolled() -> bool              # secret exists on disk
    enroll(secret, used_step) -> None  # persist a secret, revoke admin sessions
    setup_code_step(secret, code)      # time-step of a code confirming a secret
    verify(code) -> bool               # validate a 6-digit code, once
    disable() -> None                  # delete the secret, revoke admin sessions
    generation() -> Optional[str]      # changes on every enroll and disable
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
import threading
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
_STEP_LOCK = threading.Lock()


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


def _load() -> tuple[dict, bool]:
    """(stored data, readable): a missing file is readable and empty."""
    if not _MFA_FILE.exists():
        return {}, True
    try:
        data = json.loads(_MFA_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # The type only: a decoding error can quote the file's bytes.
        log.error("Failed to read %s: %s", _MFA_FILE, type(exc).__name__)
        return {}, False
    if not isinstance(data, dict):
        log.error("Failed to read %s: not a JSON object", _MFA_FILE)
        return {}, False
    return data, True


def _read() -> dict:
    return _load()[0]


def _write(data: dict) -> None:
    atomic_write_json(_MFA_FILE, data, mode=0o600)


def is_enrolled() -> bool:
    return bool(_read().get("secret_enc"))


def generation() -> Optional[str]:
    """Identifier of the current MFA state; every enroll and disable changes it.

    Admin sessions record it when they are opened and are refused once it
    differs, so a session never outlives the secret it was opened under, even
    when the session store cannot be rewritten.
    """
    value = _read().get("generation")
    return value if isinstance(value, str) else None


def _revoke_admin_sessions() -> None:
    from plugboard import admin_sessions

    admin_sessions.revoke_all()


def enroll(secret: str, *, used_step: Optional[int] = None) -> None:
    """Persist the Fernet-encrypted secret. Overwrites any existing secret.

    ``used_step`` is the time-step of the code that confirmed the secret: it
    is recorded as used, so that code cannot be replayed. Every admin session
    is revoked: they were opened under the old factor. A failure to rewrite
    the session store is raised after the new secret is in place; older
    sessions are already refused by their generation.
    """
    data = {
        "secret_enc": _encrypt_token(secret),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_used_at": None,
        "generation": _secrets.token_hex(16),
    }
    if used_step is not None:
        data["last_step"] = used_step
    _write(data)
    _revoke_admin_sessions()


def disable() -> None:
    """Remove the stored secret and its metadata, revoke every admin session.

    Only a new generation is kept, so no earlier session becomes valid again.
    Idempotent.
    """
    _write({"generation": _secrets.token_hex(16)})
    _revoke_admin_sessions()


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
    small clock skew between the admin's device and the server. A code is
    single-use: the last accepted time-step is stored and no step at or before
    it is accepted again (login, machine header and debug helper alike).
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
            return _consume_step(t_now + step)
    return False


def setup_code_step(secret: str, code: str) -> Optional[int]:
    """The current time-step when ``code`` is the current code of ``secret``.

    Used to confirm a secret before it is stored (enrolment or rotation).
    """
    counter = int(time.time() // _PERIOD)
    candidate = _hotp(_b32_normalize(secret), counter)
    return counter if hmac.compare_digest(candidate, code) else None


def _consume_step(counter: int) -> bool:
    """Record ``counter`` as used; False if it (or a later step) already was."""
    with _STEP_LOCK:
        data = _read()
        last = data.get("last_step")
        if isinstance(last, int) and counter <= last:
            log.warning("Admin TOTP code replayed (time-step already used)")
            return False
        data["last_step"] = counter
        data["last_used_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _write(data)
        except Exception as exc:
            # Without the record the code could be replayed: refuse it.
            log.error("Failed to record admin TOTP use: %s", exc)
            return False
    return True


def is_required() -> bool:
    """MFA is enforced when the operator opts in AND a secret is enrolled.

    Requiring enrolment before enforcement prevents a foot-gun: setting
    the flag on a fresh deploy with no secret would lock the admin out.
    With the flag set, a store that exists but cannot be read counts as
    enrolled: no code verifies, so admin access is refused until the file is
    repaired or removed (never MFA silently off).
    """
    if os.environ.get("PLUGBOARD_ADMIN_MFA_REQUIRED", "").strip() != "1":
        return False
    data, readable = _load()
    return not readable or bool(data.get("secret_enc"))
