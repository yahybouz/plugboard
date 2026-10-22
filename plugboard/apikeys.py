"""
Multi-tenant API key registry.
Reads/writes data/api_keys.json.

Schema: {"keys": [{
  "key_id": str,        # UUID hex, used in admin URLs
  "key_hash": str,      # SHA-256 hex digest of the bearer token (never store plaintext)
  "key_prefix": str,    # first 8 chars of the key, for display
  "client_id": str,     # slug identifying the client (may share across keys for rotation)
  "label": str,         # free text
  "created_at": str,    # ISO8601 UTC
  "is_active": bool,
  "monthly_limit_usd": float  # 0 = unlimited
}]}

v3.0.0 BREAKING: the legacy fallback that synthesised a record from the
``API_KEY`` env var has been removed (ADR-034). Empty ``api_keys.json``
now means zero keys — provision via ``POST /admin/keys``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from plugboard.storage import atomic_write_json, data_dir

_DATA_DIR = data_dir()
KEYS_FILE = _DATA_DIR / "api_keys.json"
KEYS_LOCK = _DATA_DIR / "api_keys.json.lock"

# ARCH-DEBT-029 closed: single source of truth for the lle_ key prefix.
# Importable from plugboard.app and any tooling that needs to detect a
# Bearer token shape. Changing the format = update this constant + add
# a back-compat detect path in get_key_record() if the change is breaking.
KEY_PREFIX = "pb_"
# Prefixes recognised as client keys: new keys use "pb_"; keys issued before
# the Plugboard rename keep their "lle_" prefix and stay valid.
KEY_PREFIXES = (KEY_PREFIX, "lle_")

log = logging.getLogger(__name__)

# In-process cache for key records. Invalidated via file mtime check so
# external writers (admin_portal, migrations) are picked up automatically.
# Guard with a threading.RLock — uvicorn workers are async but filelock
# + concurrent tasks can race on the index rebuild.
_cache_lock = threading.RLock()
_cache_keys: Optional[List[Dict[str, Any]]] = None
_cache_hash_index: Dict[str, Dict[str, Any]] = {}
_cache_mtime_ns: int = 0

try:
    from filelock import FileLock as _FileLock

    def _lock():
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        return _FileLock(str(KEYS_LOCK))
except ImportError:
    from contextlib import contextmanager

    @contextmanager
    def _lock():
        yield


def _hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of a raw API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _read() -> Dict[str, Any]:
    if not KEYS_FILE.exists():
        return {"keys": []}
    try:
        return json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Failed to read api_keys.json: %s", exc)
        return {"keys": []}


def _write(data: Dict[str, Any]) -> None:
    # Atomic and owner-only: API key hashes live here.
    atomic_write_json(KEYS_FILE, data, mode=0o600)
    # File mtime changed — cache will refresh on next access.
    _invalidate_cache()


def _invalidate_cache() -> None:
    """Force cache refresh on next read. Called after every write."""
    global _cache_keys, _cache_hash_index, _cache_mtime_ns
    with _cache_lock:
        _cache_keys = None
        _cache_hash_index = {}
        _cache_mtime_ns = 0


def _cache_valid() -> bool:
    """Return True if the in-memory cache matches the on-disk file."""
    if _cache_keys is None:
        return False
    try:
        current_mtime = KEYS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        # No file → legacy env-var path. Always re-read so env changes
        # (test monkeypatching of API_KEY) are reflected.
        return False
    return current_mtime == _cache_mtime_ns


def _refresh_cache() -> None:
    """Rebuild cache from disk. Caller must hold _cache_lock."""
    global _cache_keys, _cache_hash_index, _cache_mtime_ns
    with _lock():
        data = _read()
    keys = data.get("keys", [])
    # v3.0.0 BREAKING (ADR-034): the synthetic record built from the
    # legacy API_KEY env var has been removed. Empty file = empty key
    # list. Use POST /admin/keys to provision lle_* keys.
    _cache_keys = keys
    _cache_hash_index = {k["key_hash"]: k for k in keys if k.get("key_hash")}
    try:
        _cache_mtime_ns = KEYS_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        _cache_mtime_ns = 0


def load_keys() -> List[Dict[str, Any]]:
    """Return all key records. Falls back to legacy synthetic record if empty."""
    with _cache_lock:
        if not _cache_valid():
            _refresh_cache()
        return list(_cache_keys or [])


def get_key_record(raw_key: str) -> Optional[Dict[str, Any]]:
    """Return the active key record for raw_key, or None.

    SHA-256 hashes the incoming key and uses an O(1) dict lookup on the
    cached index. The 256-bit output space of SHA-256 makes timing attacks
    on dict lookup infeasible in practice (any leak would still require a
    SHA-256 preimage). Defense in depth: we also verify the matched record's
    hash via ``hmac.compare_digest`` (constant-time) before returning, so
    even a hypothetical dict-lookup side channel cannot be exploited.
    """
    incoming_hash = _hash_key(raw_key)
    with _cache_lock:
        if not _cache_valid():
            _refresh_cache()
        rec = _cache_hash_index.get(incoming_hash)
    if rec is None:
        return None
    # Constant-time confirmation that the cached hash matches the incoming
    # hash. SEC-003: never short-circuit string comparison on secrets.
    stored_hash = rec.get("key_hash", "")
    if not hmac.compare_digest(stored_hash, incoming_hash):
        return None
    if rec.get("is_active", True):
        return rec
    return None


def create_key(
    client_id: str,
    label: str = "",
    monthly_limit_usd: float = 0.0,
    allowed_models: Optional[List[str]] = None,
    rpm_limit: int = 0,
    org_id: str = "",
) -> Dict[str, Any]:
    """Generate a new random key, persist its hash, return record with plaintext key.

    The plaintext key is included in the returned dict under ``"key"`` so the
    caller can show it once.  Only the hash is persisted to disk.
    """
    key_id = uuid.uuid4().hex
    raw_key = KEY_PREFIX + secrets.token_hex(32)
    key_hash = _hash_key(raw_key)
    key_prefix = raw_key[:8]

    record: Dict[str, Any] = {
        "key_id": key_id,
        "key_hash": key_hash,
        "key_prefix": key_prefix,
        "client_id": client_id,
        "org_id": org_id,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "is_active": True,
        "monthly_limit_usd": monthly_limit_usd,
        "allowed_models": allowed_models or [],
        "rpm_limit": rpm_limit,
    }
    with _lock():
        data = _read()
        keys = list(data.get("keys", []))
        keys.append(record)
        data["keys"] = keys
        _write(data)

    # Return a copy that includes the plaintext key for one-time display
    return {**record, "key": raw_key}


def revoke_key(key_id: str) -> bool:
    """Set is_active=False for key with given key_id. Returns False if not found."""
    with _lock():
        data = _read()
        for k in data.get("keys", []):
            if k["key_id"] == key_id:
                k["is_active"] = False
                _write(data)
                return True
    return False


def delete_key(key_id: str) -> bool:
    """Hard-delete key by key_id. Returns False if not found."""
    with _lock():
        data = _read()
        before = len(data.get("keys", []))
        data["keys"] = [k for k in data.get("keys", []) if k["key_id"] != key_id]
        if len(data["keys"]) == before:
            return False
        _write(data)
    return True


def get_keys_by_client(client_id: str) -> List[Dict[str, Any]]:
    """Return all key records for a given client_id."""
    return [k for k in load_keys() if k.get("client_id") == client_id]


def get_keys_by_org(org_id: str) -> List[Dict[str, Any]]:
    """Return all key records for a given org_id."""
    return [k for k in load_keys() if k.get("org_id") == org_id]


def get_orgs() -> List[Dict[str, Any]]:
    """Return list of unique organisations with key counts and usage."""
    keys = load_keys()
    orgs: Dict[str, Dict[str, Any]] = {}
    for k in keys:
        oid = k.get("org_id", "")
        if not oid:
            continue
        if oid not in orgs:
            orgs[oid] = {"org_id": oid, "clients": set(), "keys": 0, "active_keys": 0}
        orgs[oid]["clients"].add(k.get("client_id", ""))
        orgs[oid]["keys"] += 1
        if k.get("is_active", True):
            orgs[oid]["active_keys"] += 1
    # Convert sets to lists for JSON serialization
    return [
        {**v, "clients": sorted(v["clients"]), "client_count": len(v["clients"])}
        for v in orgs.values()
    ]


def rotate_key(key_id: str) -> Optional[Dict[str, Any]]:
    """Rotate a key: revoke the old one and create a new one with same settings.

    Returns the new key record (with plaintext key) or None if key_id not found.
    """
    with _lock():
        data = _read()
        old = None
        for k in data.get("keys", []):
            if k["key_id"] == key_id:
                old = k
                break
        if not old:
            return None
        # Revoke old
        old["is_active"] = False
        _write(data)

    # Create new with same settings
    return create_key(
        client_id=old["client_id"],
        label=old.get("label", ""),
        monthly_limit_usd=old.get("monthly_limit_usd", 0.0),
        allowed_models=old.get("allowed_models"),
        rpm_limit=old.get("rpm_limit", 0),
        org_id=old.get("org_id", ""),
    )


def mask_key(key_prefix: str) -> str:
    """Return a masked representation using the stored key_prefix.

    `key_prefix` is already the only part of a key kept in storage, so the
    whole of it is shown whatever its length.
    """
    return key_prefix + "***"
