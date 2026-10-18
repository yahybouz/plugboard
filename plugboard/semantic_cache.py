"""
Semantic response cache — catch paraphrased prompts that miss the exact cache.

Two layers:
  - Phase 1 (always-on): MinHash over word-trigrams + LSH bands. Catches
    near-duplicates (typos, ponctuation, mots ajoutés). Zero deps, ~3ms/call.
  - Phase 2 (opt-in): embedding cosine similarity. Catches true paraphrases
    ("how do I reset my password" ≈ "I need to recover my login"). Requires
    a configured embedding provider + numpy at runtime; falls back silently
    to phase 1 if either is missing.

Flow in chat_completions (async path):
    exact-match cache (cache.py)        → hit? return.
    MinHash semantic cache (sync)       → hit? return.
    embedding semantic cache (async)    → hit? return.
    provider call                        → on success, populate everything.

Opt-in via PLUGBOARD_SEMANTIC_CACHE=1. Embedding layer additionally requires:
    PLUGBOARD_SEMANTIC_EMBED=1
    PLUGBOARD_EMBED_PROVIDER_ID=<provider id from providers.json>
    PLUGBOARD_EMBED_MODEL=<embedding model name>
    (legacy EMBED_PROVIDER_ID / EMBED_MODEL still accepted with warn)

Schema: semantic_cache.db
    cache_key     TEXT PRIMARY KEY   -- SHA-256(client_id || model || prompt_norm)
    client_id     TEXT NOT NULL DEFAULT ''  -- SEC-001 multi-tenant scope
    model         TEXT NOT NULL
    signature     BLOB NOT NULL      -- MinHash, 128 × uint32 packed
    band_keys     TEXT NOT NULL      -- JSON array of 32 band hashes (for LSH)
    prompt_hash   TEXT NOT NULL      -- to spot exact re-inserts
    response      TEXT NOT NULL      -- JSON
    created_at    REAL NOT NULL
    ttl           INTEGER NOT NULL
    hit_count     INTEGER NOT NULL DEFAULT 0
    cost_saved    REAL NOT NULL DEFAULT 0
    embed_model   TEXT               -- phase 2: model used for the embedding
    embedding     BLOB               -- phase 2: big-endian float32 vector

Plus `semantic_bands` (band_id, band_hash, cache_key, client_id, model) for
O(1) candidate lookup, scoped per tenant.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import struct
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from plugboard.storage import data_dir

log = logging.getLogger(__name__)


# ARCH-DEBT-039 closed: rename EMBED_* -> PLUGBOARD_EMBED_* for naming
# convention consistency. Backwards compat: read new var first, then legacy
# with deprecation warn (once per process per legacy var).
_LEGACY_WARN_LOGGED: set[str] = set()


def _env_embed(canonical: str, legacy: str, default: str = "") -> str:
    """Resolve an embed env var with backwards compat for the legacy name.

    Order: PLUGBOARD_EMBED_<X> first; if absent, fall back to legacy
    EMBED_<X> with a one-time deprecation warning. Returns the default
    if neither is set.
    """
    value = os.environ.get(canonical, "").strip()
    if value:
        return value
    legacy_value = os.environ.get(legacy, "").strip()
    if legacy_value:
        if legacy not in _LEGACY_WARN_LOGGED:
            log.warning(
                "%s is deprecated (ARCH-DEBT-039). Use %s instead. "
                "Legacy var still honoured for now.",
                legacy,
                canonical,
            )
            _LEGACY_WARN_LOGGED.add(legacy)
        return legacy_value
    return default


_DATA_DIR = data_dir()
_DB_PATH = _DATA_DIR / "semantic_cache.db"
_DEFAULT_TTL = int(os.environ.get("SEMANTIC_CACHE_TTL", "3600"))
_MAX_CACHE_SIZE = int(os.environ.get("SEMANTIC_CACHE_MAX_SIZE", "5000"))
_DEFAULT_THRESHOLD = float(os.environ.get("SEMANTIC_CACHE_THRESHOLD", "0.85"))
_EMBED_THRESHOLD = float(
    _env_embed(
        "PLUGBOARD_EMBED_SIMILARITY_THRESHOLD", "EMBED_SIMILARITY_THRESHOLD", "0.92"
    )
)
_EMBED_TIMEOUT = float(_env_embed("PLUGBOARD_EMBED_TIMEOUT", "EMBED_TIMEOUT", "10"))
_MIN_PROMPT_CHARS = 20  # skip short prompts (unreliable matching)

# MinHash parameters
_NUM_HASHES = 128
_NUM_BANDS = 32
_ROWS_PER_BAND = _NUM_HASHES // _NUM_BANDS  # 4

# Pre-computed hash seeds (stable across processes — all derived from a fixed master).
# A real Jaccard-approximating MinHash needs independent hash functions; we
# approximate by prepending a per-seed byte to the shingle before md5.
_SEEDS = tuple(i.to_bytes(4, "big") for i in range(_NUM_HASHES))


def is_enabled() -> bool:
    return os.environ.get("PLUGBOARD_SEMANTIC_CACHE", "").strip() in {
        "1",
        "true",
        "yes",
        "on",
    }


def embed_is_enabled() -> bool:
    """Phase 2 needs both the master flag and a provider+model configured."""
    if not is_enabled():
        return False
    if os.environ.get("PLUGBOARD_SEMANTIC_EMBED", "").strip() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False
    return bool(
        _env_embed("PLUGBOARD_EMBED_PROVIDER_ID", "EMBED_PROVIDER_ID").strip()
        and _env_embed("PLUGBOARD_EMBED_MODEL", "EMBED_MODEL").strip()
    )


def _validate_config() -> None:
    """Validate env vars at module import. Log warnings (never crash).

    The semantic cache is opt-in; misconfiguration must degrade silently
    rather than break the proxy. We log loudly so ops notice.
    """
    if not is_enabled():
        return

    # Numeric sanity (env vars come as strings).
    if not 0.0 <= _DEFAULT_THRESHOLD <= 1.0:
        log.warning(
            "SEMANTIC_CACHE_THRESHOLD=%s out of [0,1] — hits will be unreliable",
            _DEFAULT_THRESHOLD,
        )
    if not 0.0 <= _EMBED_THRESHOLD <= 1.0:
        log.warning("EMBED_SIMILARITY_THRESHOLD=%s out of [0,1]", _EMBED_THRESHOLD)
    if _DEFAULT_TTL <= 0:
        log.warning("SEMANTIC_CACHE_TTL=%s must be > 0", _DEFAULT_TTL)
    if _MAX_CACHE_SIZE <= 0:
        log.warning("SEMANTIC_CACHE_MAX_SIZE=%s must be > 0", _MAX_CACHE_SIZE)
    if _EMBED_TIMEOUT <= 0:
        log.warning("EMBED_TIMEOUT=%s must be > 0", _EMBED_TIMEOUT)

    # Phase 2 plumbing: warn if user asked for embeddings without configuring
    # the provider — they expect cosine matches that will never happen.
    embed_flag = os.environ.get("PLUGBOARD_SEMANTIC_EMBED", "").strip() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if embed_flag:
        if not _env_embed("PLUGBOARD_EMBED_PROVIDER_ID", "EMBED_PROVIDER_ID").strip():
            log.warning(
                "PLUGBOARD_SEMANTIC_EMBED=1 but EMBED_PROVIDER_ID is empty — "
                "embedding fallback disabled"
            )
        if not _env_embed("PLUGBOARD_EMBED_MODEL", "EMBED_MODEL").strip():
            log.warning(
                "PLUGBOARD_SEMANTIC_EMBED=1 but EMBED_MODEL is empty — "
                "embedding fallback disabled"
            )


# -- Schema --------------------------------------------------------------------


_CREATE_ENTRIES = """
CREATE TABLE IF NOT EXISTS semantic_cache (
    cache_key    TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL,
    signature    BLOB NOT NULL,
    band_keys    TEXT NOT NULL,
    prompt_hash  TEXT NOT NULL,
    response     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    ttl          INTEGER NOT NULL,
    hit_count    INTEGER NOT NULL DEFAULT 0,
    cost_saved   REAL NOT NULL DEFAULT 0
);
"""

_CREATE_BANDS = """
CREATE TABLE IF NOT EXISTS semantic_bands (
    band_id    INTEGER NOT NULL,
    band_hash  TEXT NOT NULL,
    cache_key  TEXT NOT NULL,
    client_id  TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL,
    PRIMARY KEY (band_id, band_hash, cache_key),
    FOREIGN KEY (cache_key) REFERENCES semantic_cache(cache_key) ON DELETE CASCADE
);
"""

_IDX_BANDS = (
    "CREATE INDEX IF NOT EXISTS idx_bands_lookup "
    "ON semantic_bands(model, client_id, band_id, band_hash);"
)

_IDX_CREATED = (
    "CREATE INDEX IF NOT EXISTS idx_semantic_created ON semantic_cache(created_at);"
)


def _get_conn() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_db = not _DB_PATH.exists()
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_ENTRIES)
    conn.execute(_CREATE_BANDS)
    conn.execute(_IDX_BANDS)
    conn.execute(_IDX_CREATED)
    _migrate_schema(conn)
    conn.commit()
    if new_db:
        # SEC-005: protect cache file (may contain sensitive cached responses).
        try:
            os.chmod(str(_DB_PATH), 0o600)
        except OSError as exc:
            log.warning("chmod 0600 on %s failed: %s", _DB_PATH, exc)
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE for phase 2 columns + multi-tenant client_id."""
    for ddl in (
        "ALTER TABLE semantic_cache ADD COLUMN embed_model TEXT",
        "ALTER TABLE semantic_cache ADD COLUMN embedding BLOB",
        # SEC-001: multi-tenant isolation. Old rows get '' (legacy global cache);
        # new writes always set the caller's client_id.
        "ALTER TABLE semantic_cache ADD COLUMN client_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE semantic_bands ADD COLUMN client_id TEXT NOT NULL DEFAULT ''",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            # column already exists — migration is idempotent
            pass


# -- Prompt normalization + shingling -----------------------------------------


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _normalize(text: str) -> str:
    return " ".join(_TOKEN_RE.findall(text.lower()))


def _extract_user_prompt(messages: List[Dict[str, Any]]) -> str:
    """Last user message wins — that's the actual query."""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                return content
            # Multi-modal: flatten text parts
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                return " ".join(parts)
    return ""


def _shingles(text: str, n: int = 3) -> Set[str]:
    tokens = _normalize(text).split()
    if len(tokens) < n:
        # Fallback to unigrams for very short prompts
        return set(tokens)
    return {" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


# -- MinHash + LSH -------------------------------------------------------------


def _minhash(shingles: Set[str]) -> Tuple[int, ...]:
    """Return a 128-dim MinHash signature of the shingle set."""
    if not shingles:
        return (0,) * _NUM_HASHES
    max_u32 = 0xFFFFFFFF
    signature = [max_u32] * _NUM_HASHES
    shingle_bytes = [s.encode("utf-8") for s in shingles]
    for i, seed in enumerate(_SEEDS):
        min_val = max_u32
        for sb in shingle_bytes:
            # md5(seed + shingle) → take first 4 bytes as uint32.
            # SEC: usedforsecurity=False — md5 here is a non-crypto hash for
            # MinHash sketching, not a security primitive. Required for FIPS.
            h = hashlib.md5(seed + sb, usedforsecurity=False).digest()[:4]
            v = struct.unpack(">I", h)[0]
            if v < min_val:
                min_val = v
        signature[i] = min_val
    return tuple(signature)


def _pack_signature(signature: Tuple[int, ...]) -> bytes:
    return struct.pack(f">{_NUM_HASHES}I", *signature)


def _unpack_signature(blob: bytes) -> Tuple[int, ...]:
    return struct.unpack(f">{_NUM_HASHES}I", blob)


def _band_keys(signature: Tuple[int, ...]) -> List[str]:
    """Split signature into bands; each band's rows → one hex hash key."""
    keys = []
    for b in range(_NUM_BANDS):
        start = b * _ROWS_PER_BAND
        band = signature[start : start + _ROWS_PER_BAND]
        packed = struct.pack(f">{_ROWS_PER_BAND}I", *band)
        # SEC: usedforsecurity=False — sha1 is a band bucketing hash, not
        # a security primitive. Collisions only over-trigger candidate match.
        keys.append(hashlib.sha1(packed, usedforsecurity=False).hexdigest()[:16])
    return keys


def _jaccard_signature(a: Tuple[int, ...], b: Tuple[int, ...]) -> float:
    """Estimate Jaccard similarity from two MinHash signatures."""
    if len(a) != len(b) or not a:
        return 0.0
    matches = sum(1 for x, y in zip(a, b) if x == y)
    return matches / len(a)


# -- Public API ----------------------------------------------------------------


def lookup_similar(
    model: str,
    messages: List[Dict[str, Any]],
    threshold: Optional[float] = None,
    client_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Return the best cached response whose prompt is semantically similar.

    Returns None if the feature is disabled, the prompt is too short, or no
    candidate clears the similarity threshold. Hits bump the hit_count.

    ``client_id`` scopes the lookup: a tenant only sees its own cached entries
    (SEC-001 — multi-tenant isolation). Empty string preserves legacy behavior
    for callers that haven't been wired up yet.
    """
    if not is_enabled():
        return None
    prompt = _extract_user_prompt(messages)
    if len(prompt) < _MIN_PROMPT_CHARS:
        return None

    threshold = threshold if threshold is not None else _DEFAULT_THRESHOLD
    shingles = _shingles(prompt)
    if not shingles:
        return None

    sig = _minhash(shingles)
    bands = _band_keys(sig)
    cid = client_id or ""

    conn = _get_conn()
    try:
        now = time.time()
        # Collect candidate cache_keys that share any band bucket with the query
        # AND belong to this client (SEC-001).
        placeholders = ",".join("?" for _ in bands)
        band_query = (
            "SELECT DISTINCT b.cache_key "  # nosec B608
            "FROM semantic_bands b "
            "WHERE b.model = ? "
            "AND b.client_id = ? "
            "AND b.band_hash IN (" + placeholders + ")"
        )
        rows = conn.execute(
            band_query,
            (model, cid, *bands),
        ).fetchall()

        if not rows:
            return None

        candidate_keys = [r["cache_key"] for r in rows]
        # Fetch full entries, filter expired. Re-check client_id defensively
        # (defense in depth — bands table could drift).
        ph = ",".join("?" for _ in candidate_keys)
        entries_query = (
            "SELECT cache_key, signature, response, created_at, ttl "  # nosec B608
            "FROM semantic_cache "
            "WHERE client_id = ? AND cache_key IN (" + ph + ")"
        )
        entries = conn.execute(
            entries_query,
            (cid, *candidate_keys),
        ).fetchall()

        best_score = 0.0
        best_entry = None
        for e in entries:
            if (now - e["created_at"]) > e["ttl"]:
                continue
            other_sig = _unpack_signature(e["signature"])
            score = _jaccard_signature(sig, other_sig)
            if score > best_score:
                best_score = score
                best_entry = e

        if best_entry is None or best_score < threshold:
            return None

        # Bump hit count
        conn.execute(
            "UPDATE semantic_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
            (best_entry["cache_key"],),
        )
        conn.commit()
        response = json.loads(best_entry["response"])
        response["_cache"] = {
            "hit": True,
            "semantic": True,
            "similarity": round(best_score, 3),
        }
        return response
    finally:
        conn.close()


def store(
    model: str,
    messages: List[Dict[str, Any]],
    response: Dict[str, Any],
    ttl: int = 0,
    cost_usd: float = 0.0,
    client_id: str = "",
) -> Optional[str]:
    """Index this prompt/response pair. Returns cache_key on success.

    The MinHash signature + LSH bands are written synchronously. To also
    populate the embedding column (phase 2), call ``store_async`` from an
    async context, or schedule ``_attach_embedding(cache_key, prompt)``
    yourself.

    ``client_id`` is folded into the cache_key hash so that two tenants asking
    the same question never collide on the PRIMARY KEY (SEC-001).
    """
    if not is_enabled():
        return None
    prompt = _extract_user_prompt(messages)
    if len(prompt) < _MIN_PROMPT_CHARS:
        return None
    shingles = _shingles(prompt)
    if not shingles:
        return None

    sig = _minhash(shingles)
    bands = _band_keys(sig)
    if ttl <= 0:
        ttl = _DEFAULT_TTL

    cid = client_id or ""
    prompt_norm = _normalize(prompt)
    # SEC-001: client_id is part of the cache_key — same prompt + model from
    # two tenants gets two distinct entries, no PRIMARY KEY collision.
    cache_key = hashlib.sha256(f"{cid}::{model}::{prompt_norm}".encode()).hexdigest()
    prompt_hash = hashlib.sha256(prompt_norm.encode()).hexdigest()
    clean = {k: v for k, v in response.items() if k != "_cache"}

    conn = _get_conn()
    try:
        # LRU eviction if over cap (bands cascade via foreign key)
        count = conn.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
        if count >= _MAX_CACHE_SIZE:
            to_evict = count - _MAX_CACHE_SIZE + 100
            conn.execute(
                "DELETE FROM semantic_cache WHERE cache_key IN "
                "(SELECT cache_key FROM semantic_cache "
                " ORDER BY created_at ASC LIMIT ?)",
                (to_evict,),
            )

        # Replace existing entry (same client + model + normalized prompt)
        conn.execute("DELETE FROM semantic_cache WHERE cache_key = ?", (cache_key,))
        conn.execute(
            """INSERT INTO semantic_cache
               (cache_key, client_id, model, signature, band_keys, prompt_hash,
                response, created_at, ttl, hit_count, cost_saved)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                cache_key,
                cid,
                model,
                _pack_signature(sig),
                json.dumps(bands),
                prompt_hash,
                json.dumps(clean),
                time.time(),
                ttl,
                cost_usd,
            ),
        )
        # Insert band entries for LSH lookup (scoped by client_id).
        conn.executemany(
            "INSERT OR IGNORE INTO semantic_bands "
            "(band_id, band_hash, cache_key, client_id, model) "
            "VALUES (?, ?, ?, ?, ?)",
            [(i, bk, cache_key, cid, model) for i, bk in enumerate(bands)],
        )
        conn.commit()
    finally:
        conn.close()
    return cache_key


def get_stats() -> Dict[str, Any]:
    conn = _get_conn()
    try:
        row = conn.execute(
            """SELECT COUNT(*) as entries,
                      COALESCE(SUM(hit_count), 0) as total_hits,
                      COALESCE(SUM(cost_saved * hit_count), 0) as total_cost_saved,
                      COALESCE(SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END), 0)
                          as entries_with_embedding
               FROM semantic_cache"""
        ).fetchone()
        return {
            "enabled": is_enabled(),
            "embed_enabled": embed_is_enabled(),
            "entries": row["entries"],
            "entries_with_embedding": row["entries_with_embedding"],
            "total_hits": row["total_hits"],
            "total_cost_saved_usd": round(row["total_cost_saved"], 4),
            "max_size": _MAX_CACHE_SIZE,
            "default_ttl": _DEFAULT_TTL,
            "threshold": _DEFAULT_THRESHOLD,
            "embed_threshold": _EMBED_THRESHOLD,
            "embed_model": _env_embed("PLUGBOARD_EMBED_MODEL", "EMBED_MODEL"),
            "embed_provider_id": _env_embed(
                "PLUGBOARD_EMBED_PROVIDER_ID", "EMBED_PROVIDER_ID"
            ),
        }
    finally:
        conn.close()


def clear() -> int:
    conn = _get_conn()
    try:
        cursor = conn.execute("DELETE FROM semantic_cache")
        conn.execute("DELETE FROM semantic_bands")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def purge_expired() -> int:
    conn = _get_conn()
    try:
        now = time.time()
        cursor = conn.execute(
            "DELETE FROM semantic_cache WHERE (? - created_at) > ttl", (now,)
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


# -- Phase 2: embedding-based semantic similarity -----------------------------


async def _embed(text: str) -> Optional[List[float]]:
    """Call the configured embedding provider. Returns None on any failure.

    Configurable via:
      EMBED_PROVIDER_ID   provider id from providers.json
      EMBED_MODEL         embedding model name
      EMBED_TIMEOUT       per-call timeout (seconds, default 10)

    The call is intentionally narrow: no router/retry/billing. Failures are
    logged at WARNING and silently fall back (the cache will still serve
    MinHash hits, and on miss the chat path proceeds to the provider call).
    """
    if not embed_is_enabled():
        return None

    provider_id = _env_embed("PLUGBOARD_EMBED_PROVIDER_ID", "EMBED_PROVIDER_ID").strip()
    embed_model = _env_embed("PLUGBOARD_EMBED_MODEL", "EMBED_MODEL").strip()

    try:
        from plugboard.config import get_provider
        from plugboard.router import _build_auth_header_async
    except Exception as exc:  # pragma: no cover — import safety net
        log.warning("embed import failed: %s", exc)
        return None

    provider = get_provider(provider_id)
    if not provider:
        log.warning("EMBED_PROVIDER_ID=%r not found in providers.json", provider_id)
        return None
    base_url = (provider.get("base_url") or "").rstrip("/")
    if not base_url:
        log.warning("provider %r has no base_url", provider_id)
        return None

    try:
        headers = await _build_auth_header_async(provider)
    except Exception as exc:
        log.warning("embed auth header failed: %s", exc)
        return None
    headers["Content-Type"] = "application/json"

    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=_EMBED_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{base_url}/embeddings",
                json={"model": embed_model, "input": text},
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("embed HTTP %s: %s", resp.status, body[:200])
                    return None
                data = await resp.json()
                vec = (data.get("data") or [{}])[0].get("embedding")
                if not isinstance(vec, list) or not vec:
                    return None
                return [float(v) for v in vec]
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("embed call failed: %s", exc)
        return None


def _pack_embedding(vec: List[float]) -> bytes:
    return struct.pack(f">{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> List[float]:
    dim = len(blob) // 4
    if dim == 0:
        return []
    return list(struct.unpack(f">{dim}f", blob))


def _cosine_lookup(
    model: str,
    query_vec: List[float],
    threshold: float,
    client_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Linear scan of stored embeddings for the same model + client. numpy required.

    Returns the highest-scoring entry above ``threshold`` or None. Falls back
    to None silently if numpy is missing. ``client_id`` enforces multi-tenant
    isolation (SEC-001) — empty string preserves the legacy global behavior.
    """
    cid = client_id or ""
    conn = _get_conn()
    try:
        now = time.time()
        rows = conn.execute(
            """SELECT cache_key, embedding, response, created_at, ttl
               FROM semantic_cache
               WHERE model = ?
                 AND client_id = ?
                 AND embedding IS NOT NULL""",
            (model, cid),
        ).fetchall()
        if not rows:
            return None

        q = [float(x) for x in query_vec]
        q_norm = sum(x * x for x in q) ** 0.5
        if q_norm == 0.0:
            return None

        best_score = 0.0
        best_row = None
        for row in rows:
            if (now - row["created_at"]) > row["ttl"]:
                continue
            blob = row["embedding"]
            if not blob:
                continue
            v = _unpack_embedding(blob)
            if len(v) != len(q):
                # Different embed model dim — skip cross-model comparison.
                continue
            v_norm = sum(x * x for x in v) ** 0.5
            if v_norm == 0.0:
                continue
            dot = sum(a * b for a, b in zip(q, v))
            score = dot / (q_norm * v_norm)
            if score > best_score:
                best_score = score
                best_row = row

        if best_row is None or best_score < threshold:
            return None

        conn.execute(
            "UPDATE semantic_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
            (best_row["cache_key"],),
        )
        conn.commit()
        response = json.loads(best_row["response"])
        response["_cache"] = {
            "hit": True,
            "semantic": True,
            "embedding": True,
            "similarity": round(best_score, 3),
        }
        return response
    finally:
        conn.close()


async def _attach_embedding(cache_key: str, prompt: str) -> None:
    """Background task: compute embedding and stamp it on the cache row.

    Designed to run via ``asyncio.create_task`` from ``store_async``. Failures
    are logged but never raised — the row remains usable through MinHash.
    """
    try:
        vec = await _embed(prompt)
        if not vec:
            return
        embed_model = _env_embed("PLUGBOARD_EMBED_MODEL", "EMBED_MODEL").strip()
        packed = _pack_embedding(vec)
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE semantic_cache SET embedding = ?, embed_model = ? "
                "WHERE cache_key = ?",
                (packed, embed_model, cache_key),
            )
            conn.commit()
        finally:
            conn.close()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("attach_embedding failed: %s", exc)


async def lookup_similar_async(
    model: str,
    messages: List[Dict[str, Any]],
    threshold: Optional[float] = None,
    embed_threshold: Optional[float] = None,
    client_id: str = "",
) -> Optional[Dict[str, Any]]:
    """MinHash sync first; on miss, fall back to embedding cosine similarity.

    Both layers are scoped by ``client_id`` (SEC-001) — a tenant never sees
    another tenant's cached responses. The embedding fallback is silent when:
      - the master flag (PLUGBOARD_SEMANTIC_CACHE) is off,
      - the embed flag (PLUGBOARD_SEMANTIC_EMBED) is off,
      - no provider/model configured,
      - numpy missing,
      - or the embedding API call fails.
    In all those cases callers see the same result as phase 1.
    """
    hit = lookup_similar(model, messages, threshold=threshold, client_id=client_id)
    if hit is not None:
        return hit
    if not embed_is_enabled():
        return None
    prompt = _extract_user_prompt(messages)
    if len(prompt) < _MIN_PROMPT_CHARS:
        return None
    vec = await _embed(prompt)
    if vec is None:
        return None
    thr = embed_threshold if embed_threshold is not None else _EMBED_THRESHOLD
    return _cosine_lookup(model, vec, thr, client_id=client_id)


# Tracks fire-and-forget embedding tasks. Tests use ``flush_pending`` to
# await them; production relies on the asyncio loop completing them.
_pending_embed_tasks: "set[asyncio.Task[Any]]" = set()


async def store_async(
    model: str,
    messages: List[Dict[str, Any]],
    response: Dict[str, Any],
    ttl: int = 0,
    cost_usd: float = 0.0,
    client_id: str = "",
) -> Optional[str]:
    """Sync MinHash store + fire-and-forget embedding attach.

    Returns the cache_key (same contract as ``store``). The embedding write
    happens in the background — callers are not blocked by the embed API.
    ``client_id`` is propagated to ``store`` for multi-tenant isolation.
    """
    cache_key = store(
        model, messages, response, ttl=ttl, cost_usd=cost_usd, client_id=client_id
    )
    if cache_key and embed_is_enabled():
        prompt = _extract_user_prompt(messages)
        try:
            task = asyncio.create_task(_attach_embedding(cache_key, prompt))
            _pending_embed_tasks.add(task)
            task.add_done_callback(_pending_embed_tasks.discard)
        except RuntimeError:
            # No running loop (synchronous test context) — schedule inline.
            try:
                await _attach_embedding(cache_key, prompt)
            except Exception as exc:
                log.warning("inline attach_embedding failed: %s", exc)
    return cache_key


async def flush_pending() -> None:
    """Await all pending embedding background tasks.

    Use in tests to ensure embeddings are attached before assertions, and at
    shutdown to avoid losing in-flight writes.
    """
    pending = list(_pending_embed_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# Run config validation once at import time (logs warnings only, never crashes).
_validate_config()
