"""
OAuth credential store — SQLite + Fernet encryption.

Two tables:
- oauth_credentials: long-lived access + refresh tokens per source
- oauth_flows: short-lived PKCE/device flow state (TTL 10 min)

Tokens are encrypted at rest with the project's ENCRYPTION_KEY (Fernet).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from plugboard.config import _decrypt_token, _encrypt_token
from plugboard.storage import data_dir

log = logging.getLogger(__name__)

_DATA_DIR = data_dir()
_DB_PATH = _DATA_DIR / "credentials.db"

_FLOW_TTL_SECONDS = 600  # 10 min


@dataclass
class OAuthCredential:
    source: str
    account: str
    access_token: str
    refresh_token: str = ""
    expires_at: Optional[float] = None  # unix timestamp
    scope: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self, skew: int = 60) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at < time.time() + skew


@dataclass
class OAuthFlow:
    state: str
    source: str
    code_verifier: str = ""
    device_code: str = ""
    flow_type: str = "authorization_code"
    expires_at: float = 0.0


# ── Schema ───────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_credentials (
    source        TEXT PRIMARY KEY,
    account       TEXT NOT NULL,
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    expires_at    REAL,
    scope         TEXT,
    extra         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_flows (
    state         TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    code_verifier TEXT,
    device_code   TEXT,
    flow_type     TEXT NOT NULL,
    expires_at    REAL NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_flows_expiry ON oauth_flows(expires_at);
"""


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        # Restrict file perms (0600)
        try:
            os.chmod(_DB_PATH, 0o600)
        except OSError:
            pass
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Credentials CRUD ─────────────────────────────────────────────────────────


def save_credential(cred: OAuthCredential) -> None:
    """Upsert a credential. Encrypts access + refresh tokens."""
    now = datetime.now(timezone.utc).isoformat()
    extra_json = json.dumps(cred.extra) if cred.extra else "{}"
    enc_access = _encrypt_token(cred.access_token) if cred.access_token else ""
    enc_refresh = _encrypt_token(cred.refresh_token) if cred.refresh_token else ""
    with _conn() as c:
        existing = c.execute(
            "SELECT created_at FROM oauth_credentials WHERE source = ?",
            (cred.source,),
        ).fetchone()
        created = existing["created_at"] if existing else now
        c.execute(
            """
            INSERT INTO oauth_credentials
                (source, account, access_token, refresh_token, expires_at,
                 scope, extra, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                account = excluded.account,
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                expires_at = excluded.expires_at,
                scope = excluded.scope,
                extra = excluded.extra,
                updated_at = excluded.updated_at
            """,
            (
                cred.source,
                cred.account,
                enc_access,
                enc_refresh,
                cred.expires_at,
                cred.scope,
                extra_json,
                created,
                now,
            ),
        )


def get_credential(source: str) -> Optional[OAuthCredential]:
    """Get a credential (decrypted)."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM oauth_credentials WHERE source = ?",
            (source,),
        ).fetchone()
    if not row:
        return None
    try:
        access = _decrypt_token(row["access_token"]) if row["access_token"] else ""
        refresh = _decrypt_token(row["refresh_token"]) if row["refresh_token"] else ""
    except Exception as exc:
        log.error("Failed to decrypt credential for %s: %s", source, exc)
        return None
    extra = json.loads(row["extra"] or "{}")
    return OAuthCredential(
        source=row["source"],
        account=row["account"],
        access_token=access,
        refresh_token=refresh,
        expires_at=row["expires_at"],
        scope=row["scope"] or "",
        extra=extra,
    )


def delete_credential(source: str) -> bool:
    """Delete a credential. Returns True if existed."""
    with _conn() as c:
        cur = c.execute("DELETE FROM oauth_credentials WHERE source = ?", (source,))
        return cur.rowcount > 0


def list_credentials() -> List[Dict[str, Any]]:
    """List all credentials (without tokens — for admin dashboard)."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT source, account, expires_at, scope, created_at, updated_at
            FROM oauth_credentials
            ORDER BY source
            """
        ).fetchall()
    return [dict(r) for r in rows]


# ── Flows (PKCE state + device codes) ────────────────────────────────────────


def save_flow(flow: OAuthFlow) -> None:
    """Save a pending flow (state, verifier, etc). TTL = _FLOW_TTL_SECONDS."""
    now = datetime.now(timezone.utc).isoformat()
    expires = flow.expires_at or (time.time() + _FLOW_TTL_SECONDS)
    with _conn() as c:
        c.execute(
            """
            INSERT INTO oauth_flows
                (state, source, code_verifier, device_code, flow_type,
                 expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                flow.state,
                flow.source,
                flow.code_verifier,
                flow.device_code,
                flow.flow_type,
                expires,
                now,
            ),
        )


def get_flow(state: str) -> Optional[OAuthFlow]:
    """Get a flow by state. Returns None if expired or not found."""
    _cleanup_expired_flows()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM oauth_flows WHERE state = ?",
            (state,),
        ).fetchone()
    if not row:
        return None
    if row["expires_at"] < time.time():
        return None
    return OAuthFlow(
        state=row["state"],
        source=row["source"],
        code_verifier=row["code_verifier"] or "",
        device_code=row["device_code"] or "",
        flow_type=row["flow_type"],
        expires_at=row["expires_at"],
    )


def delete_flow(state: str) -> None:
    """Remove a completed or cancelled flow."""
    with _conn() as c:
        c.execute("DELETE FROM oauth_flows WHERE state = ?", (state,))


def _cleanup_expired_flows() -> None:
    """Remove flows past their TTL."""
    with _conn() as c:
        c.execute("DELETE FROM oauth_flows WHERE expires_at < ?", (time.time(),))
