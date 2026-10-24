"""Server-side admin sessions.

The dashboard cookie stays an HMAC-signed ``issued_at.nonce.signature`` token,
but it is honoured only while its nonce is registered here. Logout revokes one
session; any MFA change (enrolment, rotation, disable) revokes them all. Each
record also keeps the MFA generation it was opened under, so an MFA change
refuses older sessions even if this store cannot be rewritten.

Records live in ``data/admin_sessions.json`` (mode 0600) so they survive a
restart and are shared by every worker reading the same data directory. Only
a SHA-256 of the nonce is stored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from plugboard.storage import StoreUnreadableError, atomic_write_json, data_dir

log = logging.getLogger(__name__)

# Most recent sessions kept; older ones are dropped when a new one is created.
_MAX_SESSIONS = 32
_LOCK = threading.Lock()


def _path() -> Path:
    return data_dir() / "admin_sessions.json"


def _digest(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def _load() -> Dict[str, Dict[str, Any]]:
    path = _path()
    if not path.exists():
        return {}
    try:
        sessions = json.loads(path.read_text(encoding="utf-8")).get("sessions")
    except (OSError, ValueError, AttributeError) as exc:
        # Unreadable store: no session is valid (fail closed).
        log.error("Failed to read %s: %s", path, exc)
        return {}
    return sessions if isinstance(sessions, dict) else {}


def _save(sessions: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_json(_path(), {"sessions": sessions}, mode=0o600)


def register(
    nonce: str,
    issued_at: int,
    *,
    mfa: bool,
    max_age: int,
    mfa_generation: Optional[str] = None,
) -> None:
    """Record a new session; ``mfa`` tells whether a TOTP code was checked.

    ``mfa_generation`` is the MFA state the session was opened under (see
    ``admin_mfa.generation``); the caller refuses the session once it changes.
    """
    now = int(time.time())
    with _LOCK:
        sessions = {
            key: rec
            for key, rec in _load().items()
            if now - int(rec.get("issued_at", 0)) <= max_age
        }
        sessions[_digest(nonce)] = {
            "issued_at": issued_at,
            "mfa": mfa,
            "mfa_generation": mfa_generation,
        }
        newest = sorted(sessions.items(), key=lambda kv: kv[1]["issued_at"])
        _save(dict(newest[-_MAX_SESSIONS:]))


def lookup(nonce: str) -> Optional[Dict[str, Any]]:
    """The session record, or None when it was never issued or is revoked."""
    return _load().get(_digest(nonce))


def revoke(nonce: str) -> bool:
    """Revoke one session. Returns True when it existed."""
    with _LOCK:
        sessions = _load()
        if sessions.pop(_digest(nonce), None) is None:
            return False
        _save(sessions)
        return True


def revoke_all() -> None:
    """Revoke every admin session (MFA change)."""
    with _LOCK:
        if not _path().exists():
            return
        try:
            _save({})
        except StoreUnreadableError:
            # An unreadable store already validates no session (see _load).
            log.error("Admin sessions not rewritten: %s is unreadable", _path())
