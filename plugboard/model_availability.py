"""Account-aware model catalog refresh.

Pricing discovery tells us which model IDs exist in the vendor catalogue.
For subscription-backed CLIs, existence is not enough: Claude/Gemini/Codex
can know about a model while the current account cannot run it. This module
probes subprocess models with a tiny prompt, quarantines hard
account-incompatible errors immediately, and persists only the usable catalog.
It also keeps provider-native catalogs such as Ollama's installed model list
fresh without touching billing history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
from plugboard.config import is_cli_provider as _is_cli_provider
from plugboard.storage import atomic_write_text, data_dir

_DATA_DIR = data_dir()
_MARKER_FILE = _DATA_DIR / "last_model_availability_run.txt"

# Model connectivity status — persisted across restarts.
# Path overridable via MODEL_STATUS_PATH env var.
_MODEL_STATUS_PATH = Path(
    os.environ.get("MODEL_STATUS_PATH", str(_DATA_DIR / "model_status.json"))
)

# Probe-on-boot flag: when true the 24h marker is ignored at bootstrap.
_PROBE_ON_BOOT = os.environ.get(
    "PLUGBOARD_PROBE_ON_BOOT", "true"
).strip().lower() not in {"0", "false", "no", "off"}

# Ignore model_status.json entirely when true (full rollback, fallback to quarantine).
_IGNORE_MODEL_STATUS = os.environ.get(
    "PLUGBOARD_IGNORE_MODEL_STATUS", "false"
).strip().lower() in {"1", "true", "yes", "on"}

# Timeout for individual Ollama HTTP probes (seconds).
_OLLAMA_PROBE_TIMEOUT_S = 10

# Global timeout for the entire run_connectivity_probes() call (seconds).
_PROBE_TOTAL_TIMEOUT_S = max(
    10, int(os.environ.get("PLUGBOARD_PROBE_TOTAL_TIMEOUT_S", "60") or "60")
)

# Max concurrent subprocess probes within a single provider (Semaphore cap).
_PROBE_SUBPROCESS_CONCURRENCY = 4

# Re-check interval for Ollama daemon when models remain offline (seconds).
_OLLAMA_RECHECK_INTERVAL_S = max(
    30,
    int(os.environ.get("PLUGBOARD_OLLAMA_RECHECK_INTERVAL_S", "300") or "300"),
)

log = logging.getLogger(__name__)

_AVAILABILITY_PROMPT = "Reply with exactly: OK"


def parse_family_and_version(model_id: str) -> tuple[str, tuple[int, ...]]:
    """
    Parses a model ID into a normalized family string and a version tuple.
    Examples:
      - "claude-opus-4-7" -> ("claude-opus", (4, 7))
      - "kimi-k2.6:cloud" -> ("kimi-k", (2, 6))
      - "qwen3.6:397b-cloud" -> ("qwen", (3, 6))
    """
    base = model_id
    # 1. Strip provider prefix if present
    parts = base.split(":", 1)
    if len(parts) > 1 and parts[0] in {
        "ollama",
        "claude-via-claude-cli",
        "chatgpt-via-codex-cli",
        "google-gemini-via-gemini-cli",
        "antigravity-via-agy-cli",
        "anthropic",
        "openai",
        "google",
        "gemini",
        "claude",
        "chatgpt",
    }:
        base = parts[1]

    # 2. Strip tags (anything after : in the remaining model name)
    if ":" in base:
        base = base.split(":", 1)[0]

    base = (
        base.replace(":cloud", "").replace("-cloud", "").replace(":latest", "").lower()
    )

    # Extract any version numbers (e.g. 5.5, 4-7, 3.6, 2.5)
    numbers_str = re.findall(r"\d+(?:\.\d+)*", base.replace("-", "."))
    versions = []
    for num_s in numbers_str:
        for part in num_s.split("."):
            if part.isdigit():
                versions.append(int(part))

    # Reconstruct normalized family (strip all version numbers and variant suffixes)
    family = base
    for num_s in re.findall(r"\d+(?:\.\d+)*", base):
        family = family.replace(num_s, "")
    for marker in [
        "-mini",
        "-pro",
        "-preview",
        "-thinking",
        "-vl",
        "-coder",
        "-next",
        "-lite",
        "-spark",
        "-v",
    ]:
        family = family.replace(marker, "")
    family = re.sub(r"[-:._]+", "-", family).strip("-")

    if not family:
        family = "unknown"

    return family, tuple(versions)


def _parse_bool(value: str) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or str(default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or str(default))
    except (TypeError, ValueError):
        return default


def _probe_kinds_from_env() -> Set[str]:
    raw = os.environ.get(
        "PLUGBOARD_MODEL_REFRESH_PROBE_KINDS", "codex,claude,gemini,agy,ollama"
    )
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


# ── Model status persistence ──────────────────────────────────────────────────


def load_model_status(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load persisted model connectivity status from disk.

    Returns an empty dict if the file does not exist or is malformed.
    """
    target = path or _MODEL_STATUS_PATH
    try:
        if target.exists():
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "model_status: failed to load %s (%s) — starting fresh", target, exc
        )
    return {}


def save_model_status(status: Dict[str, Any], path: Optional[Path] = None) -> None:
    """Persist model connectivity status to disk atomically."""
    target = path or _MODEL_STATUS_PATH
    try:
        atomic_write_text(target, json.dumps(status, indent=2))
    except Exception as exc:  # noqa: BLE001
        log.warning("model_status: failed to save %s (%s)", target, exc)


def _model_entries(model_ids: List[str]) -> List[Dict[str, str]]:
    return [{"id": mid, "name": mid} for mid in model_ids]


async def probe_subprocess_model(
    kind: str,
    model_id: str,
    *,
    timeout_s: float,
) -> Dict[str, Any]:
    """Probe one subprocess model and classify the result."""
    from plugboard import cli_subprocess
    from plugboard.model_quarantine import (
        is_account_unsupported_error,
        is_terminal_model_unavailable_error,
    )

    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": _AVAILABILITY_PROMPT}],
        "max_tokens": 4,
        "temperature": 0,
    }
    try:
        result = await cli_subprocess.complete(kind, body, timeout_s=timeout_s)
        content = (
            ((result.get("choices") or [{}])[0].get("message") or {}).get("content")
            or ""
        ).strip()
        if not content:
            return {"ok": False, "terminal": False, "reason": "empty completion"}
        return {"ok": True, "reason": ""}
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
        return {
            "ok": False,
            "terminal": is_terminal_model_unavailable_error(reason),
            "terminal_reason": (
                "account_unsupported"
                if is_account_unsupported_error(reason)
                else "model_unavailable"
            ),
            "reason": reason[:400],
        }


async def probe_ollama_model(
    model_id: str,
    *,
    base_url: str = "",
    timeout_s: float = _OLLAMA_PROBE_TIMEOUT_S,
) -> Dict[str, Any]:
    """Probe a single Ollama model via POST /api/generate with a minimal prompt.

    Returns a dict with ``ok`` bool and ``reason`` string.
    """
    import aiohttp

    url = (base_url or os.environ.get("OLLAMA_URL", "http://localhost:11434")).rstrip(
        "/"
    )
    payload = {
        "model": model_id,
        "prompt": "Reply: OK",
        "stream": False,
        "options": {"num_predict": 4},
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{url}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return {
                        "ok": False,
                        "reason": f"http_{resp.status}",
                        "detail": text[:200],
                    }
                data = await resp.json()
                response_text = (data.get("response") or "").strip()
                if not response_text:
                    return {"ok": False, "reason": "empty_response"}
                return {"ok": True, "reason": ""}
    except asyncio.TimeoutError:
        return {"ok": False, "reason": f"timeout_{int(timeout_s)}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}


async def probe_ollama_provider(
    *,
    base_url: str = "",
    timeout_s: float = _OLLAMA_PROBE_TIMEOUT_S,
) -> Dict[str, Dict[str, Any]]:
    """Probe all installed Ollama models.

    If the Ollama daemon is unreachable, every model is marked offline.
    Returns a dict keyed by model_id.
    """
    from plugboard import ollama as ollama_mod

    effective_url = base_url or os.environ.get("OLLAMA_URL", "http://localhost:11434")
    now_str = datetime.now(timezone.utc).isoformat()
    results: Dict[str, Dict[str, Any]] = {}

    # First check if daemon is reachable
    try:
        models_raw = await ollama_mod.list_models()
    except Exception as exc:  # noqa: BLE001
        log.warning("ollama probe: daemon unreachable (%s) — marking all offline", exc)
        return {}  # caller will mark all known Ollama models offline

    for item in models_raw:
        mid = item.get("name") or item.get("model") or ""
        if not mid:
            continue
        probe = await probe_ollama_model(
            mid, base_url=effective_url, timeout_s=timeout_s
        )
        results[mid] = {
            "connected": probe["ok"],
            "last_probed_at": now_str,
            "provider": "ollama",
            "reason": probe.get("reason") if not probe["ok"] else None,
        }

    return results


async def _probe_subprocess_provider(
    provider: Dict[str, Any],
    *,
    kinds: Set[str],
    now_str: str,
    semaphore: asyncio.Semaphore,
    timeout_s: float,
) -> Dict[str, Any]:
    """Probe one subprocess provider; returns partial {compound_key: entry} dict."""
    provider_id: str = provider.get("id", "")
    auth_source: str = provider.get("auth_source", "") or ""
    kind = auth_source.removeprefix("subprocess:")
    results: Dict[str, Any] = {}

    if not (kind and ("all" in kinds or kind in kinds)):
        return results

    model_entries = [
        m
        for m in (provider.get("models", []) or [])
        if isinstance(m, dict) and m.get("id")
    ]

    async def _probe_one(model_entry: Dict[str, Any]) -> None:
        raw_mid = model_entry.get("id", "")
        compound_key = f"{provider_id}:{raw_mid}"
        async with semaphore:
            try:
                probe = await probe_subprocess_model(
                    kind, raw_mid, timeout_s=max(timeout_s, 15.0)
                )
                results[compound_key] = {
                    "connected": probe["ok"],
                    "last_probed_at": now_str,
                    "provider": provider_id,
                    "reason": probe.get("reason") if not probe["ok"] else None,
                }
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "connectivity_probes: subprocess probe failed for %s (%s)",
                    compound_key,
                    exc,
                )

    await asyncio.gather(*[_probe_one(m) for m in model_entries])
    return results


async def _probe_ollama_provider(
    provider: Dict[str, Any],
    *,
    kinds: Set[str],
    now_str: str,
    timeout_s: float,
) -> Dict[str, Any]:
    """Probe one Ollama provider; returns partial {compound_key: entry} dict."""
    results: Dict[str, Any] = {}
    if "ollama" not in kinds or not _is_ollama_provider_dict(provider):
        return results

    provider_id: str = provider.get("id", "")
    base_url = (provider.get("base_url") or "").rstrip("/")
    try:
        ollama_results = await probe_ollama_provider(
            base_url=base_url, timeout_s=timeout_s
        )
        if ollama_results:
            for mid, entry in ollama_results.items():
                results[f"{provider_id}:{mid}"] = entry
        else:
            # Daemon unreachable — mark all known Ollama models offline
            for model_entry in provider.get("models", []) or []:
                if not isinstance(model_entry, dict):
                    continue
                raw_mid = model_entry.get("id", "")
                if not raw_mid:
                    continue
                results[f"{provider_id}:{raw_mid}"] = {
                    "connected": False,
                    "last_probed_at": now_str,
                    "provider": provider_id,
                    "reason": "ollama_daemon_unreachable",
                }
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "connectivity_probes: ollama probe failed for %s (%s)", provider_id, exc
        )
    return results


async def run_connectivity_probes(
    *,
    probe_kinds: Optional[Set[str]] = None,
    status_path: Optional[Path] = None,
    timeout_s: float = _OLLAMA_PROBE_TIMEOUT_S,
) -> Dict[str, Any]:
    """Probe all active providers for connectivity and persist results.

    Providers are probed **in parallel** (one asyncio task per provider).
    Within each subprocess provider, individual model probes share a
    ``asyncio.Semaphore(_PROBE_SUBPROCESS_CONCURRENCY)`` to avoid saturating
    the CLI subprocess pool.

    A global wall-clock cap (``PLUGBOARD_PROBE_TOTAL_TIMEOUT_S``, default 60s)
    wraps the entire fan-out.  Partial results from already-completed providers
    are persisted even when the cap fires.

    This is the boot-time probe that populates ``model_status.json``.
    It is best-effort: any exception preserves the previous state on disk.

    Returns the updated status dict.
    """
    from plugboard.config import load_providers

    kinds = probe_kinds if probe_kinds is not None else _probe_kinds_from_env()
    status_path = status_path or _MODEL_STATUS_PATH
    now_str = datetime.now(timezone.utc).isoformat()

    # Load previous state — used as fallback if probe fails
    previous = load_model_status(status_path)
    new_models: Dict[str, Any] = dict(previous.get("models", {}))

    try:
        providers = load_providers()
    except Exception as exc:  # noqa: BLE001
        log.warning("connectivity_probes: cannot load providers (%s)", exc)
        return previous

    active_providers = [p for p in providers if p.get("is_active", True)]

    # Shared semaphore limits concurrent subprocess probes across all providers.
    semaphore = asyncio.Semaphore(_PROBE_SUBPROCESS_CONCURRENCY)

    async def _probe_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
        """Run the appropriate probe for one provider, returning partial results."""
        auth_source: str = provider.get("auth_source", "") or ""
        if auth_source.startswith("subprocess:"):
            return await _probe_subprocess_provider(
                provider,
                kinds=kinds,
                now_str=now_str,
                semaphore=semaphore,
                timeout_s=timeout_s,
            )
        return await _probe_ollama_provider(
            provider, kinds=kinds, now_str=now_str, timeout_s=timeout_s
        )

    tasks = [asyncio.create_task(_probe_provider(p)) for p in active_providers]

    # Fan-out with global wall-clock timeout.  Persist partial results when
    # some tasks complete before the timeout fires.
    try:
        done, pending = await asyncio.wait(tasks, timeout=_PROBE_TOTAL_TIMEOUT_S)
        if pending:
            log.warning(
                "connectivity_probes: %d provider task(s) timed out after %ss "
                "— partial results persisted",
                len(pending),
                _PROBE_TOTAL_TIMEOUT_S,
            )
            for t in pending:
                t.cancel()
    except Exception as exc:  # noqa: BLE001
        log.warning("connectivity_probes: fan-out error (%s)", exc)
        done = set()

    for task in done:
        try:
            partial = task.result()
            new_models.update(partial)
        except Exception as exc:  # noqa: BLE001
            log.warning("connectivity_probes: provider task raised (%s)", exc)

    updated_status = {"last_run_at": now_str, "models": new_models}
    save_model_status(updated_status, status_path)
    log.info(
        "connectivity_probes: probed %d model entries, saved to %s",
        len(new_models),
        status_path,
    )
    return updated_status


def _is_ollama_provider_dict(provider: Dict[str, Any]) -> bool:
    """Check if a provider dict is an Ollama provider."""
    base = (provider.get("base_url") or "").lower()
    name = (provider.get("name") or provider.get("id") or "").lower()
    return provider.get("provider_type") == "sovereign" and (
        "ollama" in name or "localhost:11434" in base or "127.0.0.1:11434" in base
    )


async def refresh_subprocess_provider_models(
    provider: Dict[str, Any],
    *,
    timeout_s: Optional[float] = None,
    max_probes: Optional[int] = None,
    probe_kinds: Optional[Set[str]] = None,
    quarantine_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Discover and account-filter one subprocess provider catalog.

    Returns a report with the new model entries. The caller decides whether
    to persist it with ``config.update_provider``.
    """
    auth_source = provider.get("auth_source", "") or ""
    if not auth_source.startswith("subprocess:"):
        return {
            "provider": provider.get("id"),
            "updated": False,
            "reason": "not a subprocess provider",
            "models": provider.get("models", []) or [],
        }

    kind = auth_source.removeprefix("subprocess:")
    probe_kinds = probe_kinds if probe_kinds is not None else _probe_kinds_from_env()
    should_probe = "all" in probe_kinds or kind in probe_kinds
    timeout_s = timeout_s or _env_float("PLUGBOARD_MODEL_REFRESH_TIMEOUT_S", 45.0)
    max_probes = (
        max_probes
        if max_probes is not None
        else max(0, _env_int("PLUGBOARD_MODEL_REFRESH_MAX_PROBES", 40))
    )

    from plugboard.model_quarantine import (
        QUARANTINE_THRESHOLD,
        get_quarantined_suffixes,
        record_account_unsupported,
        record_failure,
        record_success,
        record_terminal_unavailable,
    )

    previous_ids = [
        item.get("id", "")
        for item in provider.get("models", []) or []
        if isinstance(item, dict) and item.get("id")
    ]
    previous_set = set(previous_ids)
    candidate_ids: List[str] = []
    seen: Set[str] = set()
    if should_probe:
        from plugboard import router as router_mod

        discovered = await router_mod.discover_models(provider)
        source_items = discovered
    else:
        # Do not expand a subscription-backed CLI from pricing unless this run
        # is also going to account-probe it. Otherwise a Claude/Gemini refresh
        # can persist models the current subscription may not actually allow.
        source_items = provider.get("models", []) or []
    for item in source_items:
        mid = item.get("id", "") if isinstance(item, dict) else ""
        if mid and mid not in seen:
            seen.add(mid)
            candidate_ids.append(mid)

    quarantined = (
        get_quarantined_suffixes(quarantine_path)
        if quarantine_path
        else get_quarantined_suffixes()
    )

    kept: List[str] = []
    dropped: List[Dict[str, Any]] = []
    verified = 0
    skipped = 0

    for mid in candidate_ids:
        if not should_probe:
            if mid not in quarantined:
                kept.append(mid)
            else:
                dropped.append({"id": mid, "reason": "quarantined"})
            continue

        if verified >= max_probes:
            skipped += 1
            if mid in previous_set and mid not in quarantined:
                kept.append(mid)
            continue

        verified += 1
        probe = await probe_subprocess_model(kind, mid, timeout_s=timeout_s)
        if probe.get("ok"):
            if quarantine_path:
                record_success(mid, path=quarantine_path)
            else:
                record_success(mid)
            kept.append(mid)
            continue

        reason = str(probe.get("reason") or "")
        if probe.get("terminal"):
            if probe.get("terminal_reason"):
                terminal_reason = str(probe["terminal_reason"])
            else:
                from plugboard.model_quarantine import is_account_unsupported_error

                terminal_reason = (
                    "account_unsupported"
                    if is_account_unsupported_error(reason)
                    else "model_unavailable"
                )
            if terminal_reason == "account_unsupported":
                if quarantine_path:
                    record_account_unsupported(mid, reason=reason, path=quarantine_path)
                else:
                    record_account_unsupported(mid, reason=reason)
            elif quarantine_path:
                record_terminal_unavailable(
                    mid,
                    reason=reason,
                    terminal_reason=terminal_reason,
                    path=quarantine_path,
                )
            else:
                record_terminal_unavailable(
                    mid,
                    reason=reason,
                    terminal_reason=terminal_reason,
                )
            dropped.append(
                {"id": mid, "reason": terminal_reason, "detail": reason[:200]}
            )
            continue

        entry = (
            record_failure(mid, reason=reason, path=quarantine_path)
            if quarantine_path
            else record_failure(mid, reason=reason)
        )
        fails = int(entry.get("consecutive_fails", 0) or 0)
        if fails >= QUARANTINE_THRESHOLD:
            dropped.append({"id": mid, "reason": "quarantined", "failures": fails})
        elif mid in previous_set:
            # Keep an already-published model through transient failures until
            # the quarantine threshold is reached.
            kept.append(mid)
        else:
            dropped.append({"id": mid, "reason": "probe_failed", "failures": fails})

    current_default = provider.get("default_model", "") or ""
    new_default = (
        current_default if current_default in kept else (kept[0] if kept else "")
    )
    return {
        "provider": provider.get("id"),
        "kind": kind,
        "updated": True,
        "candidate_count": len(candidate_ids),
        "kept_count": len(kept),
        "dropped_count": len(dropped),
        "verified_count": verified,
        "skipped_count": skipped,
        "dropped": dropped,
        "models": _model_entries(kept),
        "default_model": new_default,
    }


async def refresh_all_subprocess_providers(
    *,
    timeout_s: Optional[float] = None,
    max_probes: Optional[int] = None,
    probe_kinds: Optional[Set[str]] = None,
    quarantine_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Refresh, verify, and persist every active subprocess provider."""
    from plugboard.config import load_providers, update_provider
    from plugboard.router import invalidate_models_cache

    reports: List[Dict[str, Any]] = []
    persisted = 0
    for provider in load_providers():
        if not provider.get("is_active", True):
            continue
        if not (provider.get("auth_source", "") or "").startswith("subprocess:"):
            continue
        report = await refresh_subprocess_provider_models(
            provider,
            timeout_s=timeout_s,
            max_probes=max_probes,
            probe_kinds=probe_kinds,
            quarantine_path=quarantine_path,
        )
        reports.append(report)
        models = report.get("models") or []
        if models:
            updates: Dict[str, Any] = {"models": models}
            if report.get("default_model"):
                updates["default_model"] = report["default_model"]
            update_provider(str(provider["id"]), updates)
            persisted += 1
    if persisted:
        invalidate_models_cache()
    return {"providers": reports, "persisted": persisted}


def _is_ollama_provider(provider: Dict[str, Any]) -> bool:
    base = (provider.get("base_url") or "").lower()
    name = (provider.get("name") or provider.get("id") or "").lower()
    return provider.get("provider_type") == "sovereign" and (
        "ollama" in name or "localhost:11434" in base or "127.0.0.1:11434" in base
    )


def _is_native_catalog_provider(provider: Dict[str, Any]) -> bool:
    base = (provider.get("base_url") or "").lower()
    return (
        _is_ollama_provider(provider)
        or "api.anthropic.com" in base
        or "generativelanguage.googleapis.com" in base
    )


async def refresh_native_provider_models(provider: Dict[str, Any]) -> Dict[str, Any]:
    """Refresh providers with authoritative native model-list APIs.

    Anthropic and Gemini expose account-scoped model-list endpoints. Ollama's
    ``/api/tags`` is authoritative for locally installed models. Persisting
    these lists hides missing/deleted models from future `/v1/models` calls
    while leaving historical usage and billing rows untouched.
    """
    if not _is_native_catalog_provider(provider):
        return {
            "provider": provider.get("id"),
            "updated": False,
            "reason": "no native model catalog",
            "models": provider.get("models", []) or [],
        }

    from plugboard import router as router_mod

    models = await router_mod.discover_models(provider)
    current_default = provider.get("default_model", "") or ""
    model_ids = [
        item.get("id", "")
        for item in models
        if isinstance(item, dict) and item.get("id")
    ]
    # C2 (2026-06-01): for Ollama, merge the PUBLIC cloud catalogue so a freshly
    # released cloud version (e.g. minimax-m3) auto-surfaces without a manual
    # `ollama pull` — Ollama serves <name>:cloud on demand. Union, order kept.
    # A model that turns out to have no cloud variant self-heals via the
    # connectivity probe (it gets quarantined on the first failed use).
    if provider.get("id") == "ollama":
        from plugboard import ollama as ollama_mod

        if ollama_mod.cloud_autosync_enabled():
            try:
                cloud_ids = await ollama_mod.discover_cloud_whitelisted_ids()
                if cloud_ids:
                    # P3.2: never re-surface a quarantined or denylisted model. A
                    # phantom cloud id (no real variant) gets quarantined on its
                    # first failed probe; filtering here stops it being re-added
                    # every refresh, and honours admin removals (denylist).
                    from plugboard import model_quarantine as _mq
                    from plugboard.router import _model_denylist

                    excluded = _mq.get_quarantined_suffixes() | _model_denylist()
                    cloud_ids = [c for c in cloud_ids if c not in excluded]
                    if cloud_ids:
                        model_ids = list(dict.fromkeys(list(model_ids) + cloud_ids))
            except Exception as exc:  # noqa: BLE001
                log.warning("ollama cloud autosync merge failed: %s", exc)
    new_default = (
        current_default
        if current_default in set(model_ids)
        else (model_ids[0] if model_ids else "")
    )
    return {
        "provider": provider.get("id"),
        "kind": "native",
        "updated": True,
        "candidate_count": len(model_ids),
        "kept_count": len(model_ids),
        "dropped_count": 0,
        "verified_count": 0,
        "skipped_count": 0,
        "dropped": [],
        "models": _model_entries(model_ids),
        "default_model": new_default,
    }


def _refresh_state_path() -> Path:
    return data_dir() / "catalog" / "refresh_state.json"


def _read_refresh_state() -> Dict[str, str]:
    path = _refresh_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _cli_due(
    provider_id: str, state: Dict[str, str], now: datetime, hours: int
) -> bool:
    last = state.get(provider_id)
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(hours=hours)
    except ValueError:
        return True


async def discover_openai_models(
    provider: Dict[str, Any],
) -> tuple[Optional[List[Dict[str, str]]], str]:
    """``GET <base>/models`` of an OpenAI-compatible provider.

    Returns (entries, "") only for HTTP 200 with a non-empty OpenAI-format
    list; anything else is (None, reason), which model_diff treats as "no
    information" rather than "every model disappeared".
    """
    import aiohttp

    from plugboard import router as router_mod
    from plugboard.endpoints import openai_url

    url = openai_url(provider.get("base_url", ""), "models")
    try:
        headers = await router_mod._build_auth_header_async(provider)
        from plugboard.http_pool import get_session

        session = await get_session()
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status != 200:
                return None, f"GET /models returned HTTP {resp.status}"
            body = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        return None, f"GET /models failed: {exc}"[:300]
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None, "GET /models did not answer in the standard chat format"
    ids = [
        m["id"]
        for m in data
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]
    ]
    preset = str(provider.get("preset_key") or "")
    if preset.startswith("catalog-"):
        from plugboard import models_dev

        cat = models_dev.load_catalog().get(preset[len("catalog-") :])
        if cat is not None:
            excluded = set(models_dev.excluded_models(cat))
            ids = [m for m in ids if m not in excluded]
    if not ids:
        return None, "GET /models returned an empty list"
    return _model_entries(list(dict.fromkeys(ids))), ""


def _family_pruned(
    models: List[Dict[str, Any]], used_models: Set[str], default_models: Set[str]
) -> List[str]:
    """Models the exposure policy hides (see ``model_exposure``).

    In ``latest`` mode, an unused, non-default model whose version is not among
    the newest of its family is dropped from the list; in ``all`` mode nothing
    is dropped.
    """
    from plugboard import model_exposure

    ids = [m.get("id", "") for m in models if m.get("id")]
    protected: Set[str] = set()
    for mid in ids:
        suffix = mid.split(":", 1)[-1] if ":" in mid else mid
        if {mid, suffix} & (used_models | default_models):
            protected.add(mid)
    pruned = model_exposure.hidden_ids(ids, protected)
    for mid in pruned:
        log.info(
            "Hiding %s: older than the %d newest versions of its family",
            mid,
            model_exposure.versions(),
        )
    return pruned


async def refresh_all_model_catalogs(
    *,
    timeout_s: Optional[float] = None,
    max_probes: Optional[int] = None,
    probe_kinds: Optional[Set[str]] = None,
    quarantine_path: Optional[Path] = None,
    pass_kind: str = "scheduled",
) -> Dict[str, Any]:
    """Refresh every active model catalog that can be read automatically.

    - ``subprocess:`` CLI providers: probed (at most every
      ``PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H`` hours, except for a
      manual pass);
    - Anthropic, Gemini and Ollama: their native list APIs;
    - any other provider with ``auto_discover: true``: ``GET /models``.

    Lists are written through model_diff (two-absence removal, anomaly
    guard); real changes are journaled and sent as ``models.changed``.
    """
    from plugboard import change_events, model_diff
    from plugboard.config import load_providers
    from plugboard.router import invalidate_models_cache
    from plugboard.usage import get_all_used_models

    used_models = get_all_used_models()
    all_providers = load_providers()
    default_models = {
        str(p["default_model"]) for p in all_providers if p.get("default_model")
    }
    now = datetime.now(timezone.utc)
    from plugboard.intervals import cli_refresh_interval_h

    cli_hours = cli_refresh_interval_h()
    refresh_state = _read_refresh_state()
    state_changed = False

    reports: List[Dict[str, Any]] = []
    changes: Dict[str, Any] = {}
    errors: List[Dict[str, str]] = []
    batch = change_events.ChangeBatch()
    persisted = 0
    for provider in all_providers:
        if not provider.get("is_active", True):
            continue
        pid = str(provider.get("id"))
        auth_source = provider.get("auth_source", "") or ""
        is_cli = _is_cli_provider(provider)
        if (
            is_cli
            and pass_kind != "manual"
            and not _cli_due(pid, refresh_state, now, cli_hours)
        ):
            continue
        if auth_source.startswith("subprocess:"):
            report = await refresh_subprocess_provider_models(
                provider,
                timeout_s=timeout_s,
                max_probes=max_probes,
                probe_kinds=probe_kinds,
                quarantine_path=quarantine_path,
            )
        elif _is_native_catalog_provider(provider):
            report = await refresh_native_provider_models(provider)
        elif provider.get("auto_discover") is True:
            entries, reason = await discover_openai_models(provider)
            report = {"provider": pid, "kind": "openai", "models": entries or []}
            if reason:
                report["error"] = reason
        else:
            continue
        models = report.get("models") or []
        if is_cli and models and not report.get("error"):
            # A failed probe (CLI logged out, timeout) is retried next pass.
            refresh_state[pid] = now.isoformat()
            state_changed = True

        reports.append(report)
        # Family pruning keeps its original scope: CLI and native catalogs.
        # Reseller lists (auto_discover) are only trimmed by the absence rule.
        prunable = report.get("kind") != "openai"
        pruned = (
            _family_pruned(models, used_models, default_models)
            if models and prunable
            else []
        )
        diff = model_diff.apply(
            pid,
            models or None,
            "scheduler",
            pruned=pruned,
            default_model=report.get("default_model") or None,
        )
        if report.get("error"):
            diff.error = report["error"]  # the discovery's own reason is clearer
        if diff.error:
            errors.append({"provider": pid, "error": diff.error})
        if diff.applied:
            persisted += 1
        if diff.changed or diff.pending_removal or diff.error:
            changes[pid] = diff.as_dict()
        if diff.changed and not diff.first_population:
            batch.add(
                "models.changed",
                {"provider": pid, "added": diff.added, "removed": diff.removed},
            )
    if state_changed:
        try:
            _refresh_state_path().parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                _refresh_state_path(), json.dumps(refresh_state, indent=2)
            )
        except OSError as exc:
            log.warning("Cannot write %s: %s", _refresh_state_path(), exc)
    if persisted:
        invalidate_models_cache()
    sent = batch.flush_in_background()
    return {
        "providers": reports,
        "persisted": persisted,
        "last_changes": {
            "at": now.isoformat(),
            "pass": pass_kind,
            "providers": changes,
            "errors": errors,
            "webhooks": sent,
        },
    }


SleepFn = Callable[[float], Awaitable[None]]


class ModelAvailabilityScheduler:
    """Daily account-aware catalog refresh for runnable model providers."""

    def __init__(
        self,
        *,
        update_fn: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
        sleep_fn: Optional[SleepFn] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self._update_fn = update_fn or refresh_all_model_catalogs
        # The default refresh is told which kind of pass it runs (CLI cadence).
        self._pass_kind_aware = update_fn is None
        self._sleep = sleep_fn or asyncio.sleep
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self.disabled = _parse_bool(
            os.environ.get("MODEL_AVAILABILITY_SCHEDULER_DISABLED", "")
        )
        pricing_hour = _env_int("PRICING_DAILY_HOUR_UTC", 3)
        self.daily_hour_utc = max(
            0,
            min(23, _env_int("MODEL_AVAILABILITY_DAILY_HOUR_UTC", pricing_hour + 1)),
        )
        # Hours between refreshes (default 6); 0 = daily at daily_hour_utc.
        from plugboard.intervals import models_refresh_interval_h

        self.refresh_interval_h = models_refresh_interval_h()
        self.bootstrap_delay_s = max(
            0.0, _env_float("MODEL_AVAILABILITY_BOOTSTRAP_DELAY_S", 8.0)
        )
        self.update_timeout_s = max(
            1, _env_int("MODEL_AVAILABILITY_UPDATE_TIMEOUT_S", 900)
        )
        self.last_run_at: Optional[str] = None
        self.last_result: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None
        self.last_success_at: Optional[str] = None
        self.last_changes: Optional[Dict[str, Any]] = None
        self.running_since: Optional[str] = None
        self.started_at: Optional[str] = None
        self._run_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self.disabled:
            log.info(
                "ModelAvailabilityScheduler disabled via "
                "MODEL_AVAILABILITY_SCHEDULER_DISABLED"
            )
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self.started_at = self._now().isoformat()
        self._task = asyncio.create_task(
            self._run(), name="model-availability-scheduler"
        )
        self._task.add_done_callback(self._on_task_done)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        finally:
            self._task = None

    @property
    def status_dict(self) -> Dict[str, Any]:
        return {
            "disabled": self.disabled,
            "running": self._task is not None and not self._task.done(),
            "daily_hour_utc": self.daily_hour_utc,
            "refresh_interval_h": self.refresh_interval_h,  # C3: 0 = daily-at-hour
            "bootstrap_delay_s": self.bootstrap_delay_s,
            "update_timeout_s": self.update_timeout_s,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "last_success_at": self.last_success_at,
            "last_changes": self.last_changes,
            "running_since": self.running_since,
            "stale": self.is_stale(),
        }

    def is_stale(self) -> bool:
        """No successful refresh for 2 intervals (48 h in daily mode)."""
        from plugboard.intervals import stale_after_s

        if self.disabled:
            return False
        if self.last_success_at is None:
            # Never succeeded since start: stale once it has had time to.
            if self.started_at is None:
                return False
            last = datetime.fromisoformat(self.started_at)
        else:
            last = datetime.fromisoformat(self.last_success_at)
        return (self._now() - last).total_seconds() > stale_after_s(
            self.refresh_interval_h
        )

    async def run_now(self, kind: str = "manual") -> None:
        """Run one refresh now; raises RuntimeError while another one runs."""
        if self._run_lock.locked():
            raise RuntimeError(self.running_since or "")
        await self._invoke(kind)

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("ModelAvailabilityScheduler crashed: %s", exc, exc_info=exc)

    async def _run(self) -> None:
        if self.bootstrap_delay_s:
            await self._sleep(self.bootstrap_delay_s)

        # PLUGBOARD_PROBE_ON_BOOT=true (default): always probe at bootstrap,
        # ignoring the 24h marker.  Ensures every redeploy gets fresh status.
        # Set to false to restore the previous skip-if-recent behaviour.
        should_run_bootstrap = True
        if not _PROBE_ON_BOOT:
            try:
                if _MARKER_FILE.exists():
                    last_run_str = _MARKER_FILE.read_text(encoding="utf-8").strip()
                    if last_run_str:
                        last_run = datetime.fromisoformat(last_run_str)
                        if self._now() - last_run < timedelta(hours=24):
                            should_run_bootstrap = False
                            log.info(
                                "ModelAvailabilityScheduler: last successful run was at %s "
                                "(within 24h, PROBE_ON_BOOT=false), skipping bootstrap run",
                                last_run_str,
                            )
            except Exception as exc:
                log.warning(
                    "ModelAvailabilityScheduler: failed to read last run marker (%s), "
                    "proceeding with bootstrap",
                    exc,
                )

        if should_run_bootstrap:
            # Also run connectivity probes so model_status.json is fresh.
            await self._run_connectivity_probes()
            await self._invoke("bootstrap")
        else:
            try:
                self.last_run_at = _MARKER_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                pass

        # Spawn a background task that re-probes Ollama models every
        # PLUGBOARD_OLLAMA_RECHECK_INTERVAL_S (default 300s).
        # This covers the common case where the Ollama daemon starts
        # after Plugboard (e.g. docker-compose depends_on race).
        ollama_watcher = asyncio.create_task(
            self._watch_ollama_late_start(), name="ollama-late-start-watcher"
        )

        try:
            while not self._stopping.is_set():
                try:
                    await self._sleep(self._seconds_until_next_run())
                except asyncio.CancelledError:
                    raise
                if self._stopping.is_set():
                    return
                await self._invoke("daily")
        finally:
            ollama_watcher.cancel()
            try:
                await ollama_watcher
            except (asyncio.CancelledError, Exception):
                pass

    def _seconds_until_next_run(self) -> float:
        if self.refresh_interval_h > 0:  # C3: fixed frequent interval
            return max(1.0, self.refresh_interval_h * 3600 + random.uniform(0, 300))
        now = self._now()
        target = now.replace(
            hour=self.daily_hour_utc, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target = target + timedelta(days=1)
        return max(1.0, (target - now).total_seconds() + random.uniform(0, 300))

    async def _invoke(self, kind: str) -> None:
        # One run at a time: a scheduled tick that finds a manual run in
        # progress (or the reverse) is skipped rather than run alongside it.
        if self._run_lock.locked():
            log.info(
                "%s %s skipped: another run started at %s",
                type(self).__name__,
                kind,
                self.running_since,
            )
            return
        async with self._run_lock:
            self.running_since = self._now().isoformat()
            try:
                await self._invoke_inner(kind)
            finally:
                self.running_since = None

    async def _invoke_inner(self, kind: str) -> None:
        try:
            call = (
                self._update_fn(pass_kind=kind)  # type: ignore[call-arg]  # default refresh only
                if self._pass_kind_aware
                else self._update_fn()
            )
            result = await asyncio.wait_for(call, timeout=self.update_timeout_s)
            now_str = self._now().isoformat()
            self.last_run_at = now_str
            self.last_success_at = now_str
            self.last_result = {k: v for k, v in result.items() if k != "last_changes"}
            if result.get("last_changes") is not None:
                self.last_changes = result["last_changes"]
            self.last_error = None
            log.info(
                "ModelAvailabilityScheduler %s OK - persisted=%s",
                kind,
                result.get("persisted"),
            )
            # Write marker file
            try:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                _MARKER_FILE.write_text(now_str, encoding="utf-8")
            except Exception as exc:
                log.warning(
                    "ModelAvailabilityScheduler: failed to write last run marker (%s)",
                    exc,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_run_at = self._now().isoformat()
            self.last_error = str(exc)[:400]
            log.warning("ModelAvailabilityScheduler %s failed: %s", kind, exc)

    async def _run_connectivity_probes(self) -> None:
        """Run connectivity probes best-effort; never raises."""
        try:
            await asyncio.wait_for(
                run_connectivity_probes(),
                timeout=self.update_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ModelAvailabilityScheduler: connectivity probes failed (%s) — "
                "previous model_status.json preserved",
                exc,
            )

    async def _watch_ollama_late_start(self) -> None:
        """Periodically re-probe Ollama models that are offline.

        Runs every ``_OLLAMA_RECHECK_INTERVAL_S`` seconds in the background.
        Transitions offline→online and online→offline are logged at INFO level.
        Exits cleanly on CancelledError (triggered by scheduler stop).
        """
        while True:
            try:
                await self._sleep(_OLLAMA_RECHECK_INTERVAL_S)
            except asyncio.CancelledError:
                return

            status_before = load_model_status()
            models_before: Dict[str, Any] = status_before.get("models", {})

            # Only bother if any Ollama model is currently offline.
            has_offline_ollama = any(
                not entry.get("connected", True)
                and entry.get("reason") == "ollama_daemon_unreachable"
                for entry in models_before.values()
            )
            if not has_offline_ollama:
                continue

            try:
                updated = await asyncio.wait_for(
                    run_connectivity_probes(probe_kinds={"ollama"}),
                    timeout=60.0,
                )
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("ollama_watcher: probe failed (%s)", exc)
                continue

            models_after: Dict[str, Any] = updated.get("models", {})
            for key, after_entry in models_after.items():
                before_entry = models_before.get(key, {})
                was_connected = bool(before_entry.get("connected", True))
                now_connected = bool(after_entry.get("connected", True))
                if was_connected != now_connected:
                    direction = "online" if now_connected else "offline"
                    log.info("ollama_watcher: %s transitioned to %s", key, direction)
