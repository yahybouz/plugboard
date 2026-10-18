"""
Ollama integration — sovereign (local) LLM provider.

Encapsulates all Ollama-specific HTTP calls:
  - Model management: list, pull, delete, show
  - Health check: ping Ollama server
  - Bootstrap: auto-register as provider on startup

Ollama API docs: https://github.com/ollama/ollama/blob/main/docs/api.md
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
_OLLAMA_ENABLED = os.environ.get("OLLAMA_ENABLED", "auto").strip().lower()
_OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "30"))
_PROVIDER_ID = "ollama"
_PROVIDER_NAME = "Ollama"


def get_ollama_url() -> str:
    """Return the configured Ollama base URL."""
    return _OLLAMA_URL


# ── Health ───────────────────────────────────────────────────────────────────


async def check_available() -> bool:
    """Check if Ollama is running at the configured URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _OLLAMA_URL,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    return "ollama is running" in text.lower()
                return False
    except Exception:
        return False


async def health() -> Dict[str, Any]:
    """Ping Ollama and return health status with latency."""
    t0 = _time.perf_counter()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _OLLAMA_URL,
                timeout=aiohttp.ClientTimeout(total=_OLLAMA_TIMEOUT),
            ) as resp:
                elapsed_ms = (_time.perf_counter() - t0) * 1000
                ok = resp.status == 200
                return {
                    "ok": ok,
                    "latency_ms": round(elapsed_ms, 1),
                    "status": resp.status,
                }
    except Exception as exc:
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        return {"ok": False, "latency_ms": round(elapsed_ms, 1), "error": str(exc)}


# ── Model management ────────────────────────────────────────────────────────


async def list_models() -> List[Dict[str, Any]]:
    """List installed Ollama models via GET /api/tags."""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{_OLLAMA_URL}/api/tags",
            timeout=aiohttp.ClientTimeout(total=_OLLAMA_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(
                    f"Ollama /api/tags returned {resp.status}: {text[:200]}"
                )
            data = await resp.json()
            return data.get("models", [])


# ── Cloud catalog discovery (C2, 2026-06-01) ──────────────────────────────────
#
# /api/tags on the LOCAL daemon only lists PULLED models, so a freshly-released
# cloud version (e.g. minimax-m3) never auto-appears. The public catalogue at
# https://ollama.com/api/tags lists every AVAILABLE model. Ollama serves a
# ``<name>:cloud`` model on-demand even without a local pull, so merging the
# whitelisted-family cloud ids into the provider list is enough to surface them.

_CLOUD_CATALOG_URL = os.environ.get(
    "OLLAMA_CLOUD_CATALOG_URL", "https://ollama.com/api/tags"
).rstrip("/")

# Mirrors the Ollama keyword whitelist in router._is_whitelisted_model — keep in
# sync ("gemini" was missing here, present in the router).
_DEFAULT_CLOUD_FAMILIES = (
    "deepseek", "kimi", "qwen", "glm", "gemma", "nemotron", "minimax", "gpt-oss",
    "gemini", "beam",
)


def _cloud_families() -> tuple:
    """Cloud families listed for Ollama: PLUGBOARD_OLLAMA_CLOUD_FAMILIES
    (comma-separated, ``all`` for the whole catalogue) or the default set."""
    raw = os.environ.get("PLUGBOARD_OLLAMA_CLOUD_FAMILIES", "").strip().lower()
    if not raw:
        return _DEFAULT_CLOUD_FAMILIES
    if raw == "all":
        return ("",)  # every name contains ""
    return tuple(f.strip() for f in raw.split(",") if f.strip())


# Kept for callers that read the default set directly.
_CLOUD_WHITELIST_FAMILIES = _DEFAULT_CLOUD_FAMILIES


def cloud_autosync_enabled() -> bool:
    """C2 gate. Default ON; set PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC=0 to disable."""
    return os.environ.get(
        "PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC", "1"
    ).strip().lower() not in ("0", "false", "no", "")


def _to_cloud_id(catalog_name: str) -> str:
    """Derive the Ollama cloud id from a catalogue base name. Verified against the
    pulled set: a tagged name appends ``-cloud`` to the tag (``qwen3.5:397b`` ->
    ``qwen3.5:397b-cloud``, ``gpt-oss:120b`` -> ``gpt-oss:120b-cloud``), an
    untagged name appends ``:cloud`` (``minimax-m3`` -> ``minimax-m3:cloud``)."""
    if catalog_name.endswith(":cloud") or catalog_name.endswith("-cloud"):
        return catalog_name
    return f"{catalog_name}-cloud" if ":" in catalog_name else f"{catalog_name}:cloud"


def cloud_ids_for_whitelist(catalog_names: List[str]) -> List[str]:
    """Pure: from catalogue base names, the ``<name>:cloud`` ids for whitelisted
    families (deepseek/kimi/qwen/glm/gemma/nemotron/minimax/gpt-oss)."""
    out: List[str] = []
    seen = set()
    for name in catalog_names:
        if not name:
            continue
        low = name.lower()
        if any(fam in low for fam in _cloud_families()):
            cid = _to_cloud_id(name)
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out


async def fetch_cloud_catalog(timeout: int = 10) -> List[str]:
    """Fetch the public Ollama Cloud catalogue (base model names). Graceful:
    returns [] on any failure so a transient network issue never breaks listing."""
    t0 = _time.perf_counter()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _CLOUD_CATALOG_URL,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    log.warning(
                        "ollama cloud catalog %s -> %s", _CLOUD_CATALOG_URL, resp.status
                    )
                    return []
                data = await resp.json(content_type=None)
                names = [
                    m.get("name", "")
                    for m in data.get("models", [])
                    if isinstance(m, dict) and m.get("name")
                ]
                elapsed_ms = (_time.perf_counter() - t0) * 1000
                # Observability: log count AND latency — a
                # silent drop to 0 would otherwise vanish the cloud catalog with no
                # trace if the upstream (unofficial) schema changes, and the fetch
                # sits in the refresh path so its latency must be visible.
                if names:
                    log.info(
                        "ollama cloud catalog: %d models in %.0fms", len(names), elapsed_ms
                    )
                else:
                    log.warning(
                        "ollama cloud catalog: 0 models in %.0fms (schema change or empty?)",
                        elapsed_ms,
                    )
                return names
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = (_time.perf_counter() - t0) * 1000
        log.warning(
            "ollama cloud catalog fetch failed after %.0fms: %s", elapsed_ms, exc
        )
        return []


async def discover_cloud_whitelisted_ids(timeout: int = 10) -> List[str]:
    """Convenience: fetch the cloud catalogue + return whitelisted ``:cloud`` ids."""
    return cloud_ids_for_whitelist(await fetch_cloud_catalog(timeout=timeout))


async def pull_model(name: str) -> AsyncIterator[Dict[str, Any]]:
    """
    Pull (download) a model via POST /api/pull.
    Yields NDJSON progress dicts: {"status": "...", "completed": N, "total": N}
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{_OLLAMA_URL}/api/pull",
            json={"name": name},
            timeout=aiohttp.ClientTimeout(total=3600),  # pulls can be very slow
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Ollama pull failed ({resp.status}): {text[:200]}")
            buffer = b""
            async for chunk in resp.content.iter_any():
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            log.warning(
                                "Ollama pull: invalid JSON line: %s", line[:100]
                            )


async def delete_model(name: str) -> bool:
    """Delete a model via DELETE /api/delete."""
    async with aiohttp.ClientSession() as session:
        async with session.delete(
            f"{_OLLAMA_URL}/api/delete",
            json={"name": name},
            timeout=aiohttp.ClientTimeout(total=_OLLAMA_TIMEOUT),
        ) as resp:
            if resp.status == 200:
                return True
            if resp.status == 404:
                return False
            text = await resp.text()
            raise RuntimeError(f"Ollama delete failed ({resp.status}): {text[:200]}")


async def show_model(name: str) -> Dict[str, Any]:
    """Get model details via POST /api/show."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{_OLLAMA_URL}/api/show",
            json={"name": name},
            timeout=aiohttp.ClientTimeout(total=_OLLAMA_TIMEOUT),
        ) as resp:
            if resp.status == 404:
                raise RuntimeError(f"Model '{name}' not found in Ollama")
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Ollama show failed ({resp.status}): {text[:200]}")
            return await resp.json()


# ── Bootstrap ────────────────────────────────────────────────────────────────


async def bootstrap_ollama() -> Optional[Dict[str, Any]]:
    """
    Auto-register Ollama as a sovereign provider if detected.

    Behavior depends on OLLAMA_ENABLED env var:
      - "auto" (default): try to detect, register if found
      - "true": always try to register
      - "false": never register
    """
    if _OLLAMA_ENABLED == "false":
        log.debug("Ollama bootstrap disabled (OLLAMA_ENABLED=false)")
        return None

    available = await check_available()

    if not available:
        if _OLLAMA_ENABLED == "true":
            log.warning("Ollama not available at %s (OLLAMA_ENABLED=true)", _OLLAMA_URL)
        else:
            log.debug("Ollama not detected at %s", _OLLAMA_URL)
        return None

    from plugboard.config import create_provider, load_providers

    # Check if already registered
    for p in load_providers():
        if p.get("id") == _PROVIDER_ID:
            log.info("Ollama provider already registered (id=%s)", _PROVIDER_ID)
            # Sync models
            await _sync_models(p)
            return p

    # Register new provider
    log.info("Registering Ollama as sovereign provider: %s", _OLLAMA_URL)
    record = create_provider(
        name=_PROVIDER_NAME,
        base_url=_OLLAMA_URL,
        bearer_token="",
        default_model="",
        is_active=True,
        provider_type="sovereign",
    )

    # Discover and sync models
    await _sync_models(record)

    return record


async def _sync_models(provider: Dict[str, Any]) -> None:
    """Discover installed Ollama models and save to provider record.

    update_provider() does a full replacement of the ``models`` key, so any
    sticky flags (notably ``internet_access`` written by the boot probe or
    by the admin UI) would be wiped on each sync. We therefore read the
    current provider config first, build a snapshot of preserved per-model
    flags, and re-inject them into the new list before writing.

    Preserved flags: ``internet_access``, ``internet_access_confidence``,
    ``last_probed_at``, ``connected``, ``reason``.
    """
    try:
        from plugboard.config import get_provider, update_provider
        from plugboard.router import invalidate_models_cache

        # Snapshot existing sticky flags before the sync replaces the list.
        # Use a fresh read of the config (not the cached `provider` arg) in
        # case the boot probe wrote internet_access after this provider
        # record was constructed.
        preserved: Dict[str, Dict[str, Any]] = {}
        try:
            current = get_provider(provider["id"]) or provider
            for m in (current.get("models") or []):
                if not isinstance(m, dict):
                    continue
                mid = m.get("id") or m.get("name")
                if not mid:
                    continue
                snap: Dict[str, Any] = {}
                for k in (
                    "internet_access",
                    "internet_access_confidence",
                    "last_probed_at",
                    "connected",
                    "reason",
                ):
                    if k in m:
                        snap[k] = m[k]
                if snap:
                    preserved[mid] = snap
        except Exception as exc:  # noqa: BLE001
            log.debug("sticky-flags snapshot failed: %s", exc)

        models = await list_models()
        model_list: List[Dict[str, Any]] = []
        for m in models:
            mid = m.get("name") or m.get("model") or ""
            if not mid:
                continue
            entry: Dict[str, Any] = {"id": mid, "name": mid}
            # Re-inject preserved sticky flags so the boot-probe verdict
            # survives subsequent catalog syncs (Gemini audit finding).
            if mid in preserved:
                entry.update(preserved[mid])
            model_list.append(entry)

        if model_list:
            update_provider(provider["id"], {"models": model_list})
            invalidate_models_cache()
            log.info(
                "Synced %d Ollama models (preserved sticky flags for %d)",
                len(model_list),
                sum(1 for m in model_list if "internet_access" in m),
            )
    except Exception as exc:
        log.warning("Failed to sync Ollama models: %s", exc)
