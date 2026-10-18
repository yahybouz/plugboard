"""
Response cache — avoid redundant LLM calls for identical prompts.

Strategy:
- Hash: SHA-256 of (model + messages + key params)
- Storage: SQLite (data/cache.db) with TTL
- Hit: return cached response, skip provider call
- Miss: call provider, cache response
- TTL: configurable per request or global default (env CACHE_TTL)

The cache is opt-in: requests with `cache: false` skip it.
Streaming requests are never cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time


from typing import Any, Dict, Optional
from plugboard.storage import data_dir

log = logging.getLogger(__name__)

_DATA_DIR = data_dir()
_DB_PATH = _DATA_DIR / "cache.db"
_DEFAULT_TTL = int(os.environ.get("CACHE_TTL", "300"))  # 5 min default
_MAX_CACHE_SIZE = int(os.environ.get("CACHE_MAX_SIZE", "10000"))


# ARCH-DEBT-023 closed: SEC-001 strict-tenant gate factored to
# ``cache_common`` so semantic_cache.py can reuse the same contract
# without diverging. Module-local aliases preserve backwards compat
# for any external caller that imported the underscored names.
from plugboard.cache_common import (  # noqa: E402
    check_client_id as _check_client_id,
)


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS response_cache (
    cache_key   TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    response    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    ttl         INTEGER NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    cost_saved  REAL NOT NULL DEFAULT 0
);
"""

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_cache_created ON response_cache(created_at);"
)
# ARCH-DEBT-024 (closed, LITE version): index on (client_id, created_at)
# so a future tenant-aware eviction can scan only one tenant's hot rows
# without a full table scan. The actual eviction policy still runs LRU
# globally — once a tenant SLA is contractualised, swap the eviction
# query in put() to:
#   DELETE FROM response_cache WHERE cache_key IN (
#     SELECT cache_key FROM response_cache
#     WHERE client_id = ?  -- evict only from this tenant's quota
#     ORDER BY created_at ASC LIMIT ?
#   )
# and add a per-tenant quota check before the global _MAX_CACHE_SIZE one.
# Tracking ARCH-DEBT-024-followup for the contractualised SLA work.
_CREATE_INDEX_TENANT = (
    "CREATE INDEX IF NOT EXISTS idx_cache_tenant_created "
    "ON response_cache(client_id, created_at);"
)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE for multi-tenant client_id (SEC-001 v2.8.2).

    Existing pre-v2.8.2 rows get '' as client_id. They remain accessible
    only to callers passing client_id="" (legacy behavior); new writes are
    always tenant-scoped.
    """
    try:
        conn.execute(
            "ALTER TABLE response_cache ADD COLUMN client_id TEXT NOT NULL DEFAULT ''"
        )
    except sqlite3.OperationalError:
        # column already exists — migration is idempotent
        pass


def _get_conn() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEX)
    conn.execute(_CREATE_INDEX_TENANT)  # ARCH-DEBT-024
    _migrate_schema(conn)
    conn.commit()
    return conn


def compute_cache_key(model: str, messages: list, client_id: str = "", **params) -> str:
    """Compute a deterministic cache key from client + model + messages + params.

    SEC-001 v2.8.2: ``client_id`` is folded into the hash so two tenants
    asking the same question never collide on the PRIMARY KEY. Empty
    string preserves legacy behavior for callers that haven't been wired
    up yet (defense in depth — the WHERE clause in get_cached also
    filters by client_id).
    """
    payload = {
        "client_id": _check_client_id(client_id),
        "model": model,
        "messages": messages,
    }
    # Include key params that affect output
    for k in ("temperature", "max_tokens", "top_p", "stop"):
        if k in params and params[k] is not None:
            payload[k] = params[k]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def get_cached(cache_key: str, client_id: str = "") -> Optional[Dict[str, Any]]:
    """Look up a cached response. Returns None if miss, expired, or wrong tenant.

    SEC-001 v2.8.2: filters by ``client_id`` even though the cache_key
    already encodes it — defense in depth so a hash collision or a caller
    bug cannot leak across tenants.
    """
    cid = _check_client_id(client_id)
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM response_cache WHERE cache_key = ? AND client_id = ?",
            (cache_key, cid),
        ).fetchone()
        if not row:
            return None
        # Check TTL
        age = time.time() - row["created_at"]
        if age > row["ttl"]:
            conn.execute(
                "DELETE FROM response_cache WHERE cache_key = ? AND client_id = ?",
                (cache_key, cid),
            )
            conn.commit()
            return None
        # Update hit count (scoped — never bump another tenant's row)
        conn.execute(
            "UPDATE response_cache SET hit_count = hit_count + 1 "
            "WHERE cache_key = ? AND client_id = ?",
            (cache_key, cid),
        )
        conn.commit()
        response = json.loads(row["response"])
        response["_cache"] = {
            "hit": True,
            "age_seconds": round(age, 1),
            "hit_count": row["hit_count"] + 1,
        }
        return response
    finally:
        conn.close()


def set_cached(
    cache_key: str,
    model: str,
    prompt_hash: str,
    response: Dict[str, Any],
    ttl: int = 0,
    cost_usd: float = 0.0,
    client_id: str = "",
) -> None:
    """Store a response in the cache, scoped to ``client_id`` (SEC-001 v2.8.2)."""
    if ttl <= 0:
        ttl = _DEFAULT_TTL
    cid = _check_client_id(client_id)
    conn = _get_conn()
    try:
        # Evict old entries if cache too large
        count = conn.execute("SELECT COUNT(*) FROM response_cache").fetchone()[0]
        if count >= _MAX_CACHE_SIZE:
            conn.execute(
                "DELETE FROM response_cache WHERE cache_key IN "
                "(SELECT cache_key FROM response_cache ORDER BY created_at ASC LIMIT ?)",
                (count - _MAX_CACHE_SIZE + 100,),
            )

        # Remove _cache metadata before storing
        clean = {k: v for k, v in response.items() if k != "_cache"}

        # ARCH-DEBT-022 closed: replace `INSERT OR REPLACE` (which resets
        # hit_count to 0 on every write) with an UPSERT that preserves the
        # existing hit_count if a refresh races against a write. Atomic in
        # SQLite. The conflict target is cache_key (PRIMARY KEY).
        conn.execute(
            """INSERT INTO response_cache
                   (cache_key, client_id, model, prompt_hash, response,
                    created_at, ttl, hit_count, cost_saved)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                   client_id = excluded.client_id,
                   model = excluded.model,
                   prompt_hash = excluded.prompt_hash,
                   response = excluded.response,
                   created_at = excluded.created_at,
                   ttl = excluded.ttl,
                   cost_saved = excluded.cost_saved
                   -- hit_count intentionally NOT reset on refresh""",
            (
                cache_key,
                cid,
                model,
                prompt_hash,
                json.dumps(clean),
                time.time(),
                ttl,
                cost_usd,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_cache_stats(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Return cache statistics. ARCH-DEBT-021 closed: tenant_id filter
    lets multi-tenant admin endpoints surface per-tenant numbers without
    leaking the global aggregate. None = global aggregate (legacy
    behavior preserved for callers that don't care about tenant scope)."""
    conn = _get_conn()
    try:
        if tenant_id:
            row = conn.execute(
                """SELECT COUNT(*) as entries,
                          COALESCE(SUM(hit_count), 0) as total_hits,
                          COALESCE(SUM(cost_saved * hit_count), 0) as total_cost_saved
                   FROM response_cache
                   WHERE client_id = ?""",
                (tenant_id,),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COUNT(*) as entries,
                          COALESCE(SUM(hit_count), 0) as total_hits,
                          COALESCE(SUM(cost_saved * hit_count), 0) as total_cost_saved
                   FROM response_cache"""
            ).fetchone()
        return {
            "tenant_id": tenant_id,  # echoed back so callers can audit scope
            "entries": row["entries"],
            "total_hits": row["total_hits"],
            "total_cost_saved_usd": round(row["total_cost_saved"], 4),
            "max_size": _MAX_CACHE_SIZE,
            "default_ttl": _DEFAULT_TTL,
        }
    finally:
        conn.close()


def clear_cache(tenant_id: Optional[str] = None) -> int:
    """Clear cached responses. ARCH-DEBT-021 closed: tenant_id filter
    scopes the deletion. None = global wipe (legacy behavior, requires
    admin auth at the call site). Returns number deleted."""
    conn = _get_conn()
    try:
        if tenant_id:
            cursor = conn.execute(
                "DELETE FROM response_cache WHERE client_id = ?",
                (tenant_id,),
            )
        else:
            cursor = conn.execute("DELETE FROM response_cache")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def purge_expired() -> int:
    """Remove expired entries. Returns number deleted."""
    conn = _get_conn()
    try:
        now = time.time()
        cursor = conn.execute(
            "DELETE FROM response_cache WHERE (? - created_at) > ttl", (now,)
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
