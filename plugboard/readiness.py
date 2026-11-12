"""Readiness of the instance, for ``GET /health/ready`` (PLU-31).

``/health`` says the process answers (liveness); readiness says it can serve
requests. Blocking checks:

* ``usage_db``: the usage database takes a write lock (``BEGIN IMMEDIATE``);
* ``cli``: every active CLI provider has its binary on PATH (never started);
* ``auth``: client auth is on, or the instance runs in dev mode.

Reported, never blocking: ``ollama`` (from the watchdog's last checks, no
network call) and ``background`` (watchdog and periodic jobs running).

The route is public: states are generic words, never a provider id, a path
or an exception text. A check that raises gives its failure state and a
server-side warning naming the exception class only.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from plugboard import cli_subprocess, config, provider_watchdog, router, usage
from plugboard.legacy_connectors import is_removed_provider

log = logging.getLogger(__name__)

CACHE_TTL_S = 5.0

_cache: Optional[Tuple[float, Dict[str, Any]]] = None
_lock: Optional[asyncio.Lock] = None


def reset_cache() -> None:
    """Forget the last result (tests, or a state change that must show now)."""
    global _cache, _lock
    _cache = None
    _lock = None


def _is_active(provider: Dict[str, Any]) -> bool:
    return bool(provider.get("is_active", True))


def usage_db_state() -> str:
    """``ok`` when usage.db takes a write lock, ``fail`` otherwise. Blocking:
    run it in a thread."""
    try:
        conn = usage._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        log.warning("readiness: usage database not writable (%s)", type(exc).__name__)
        return "fail"
    return "ok"


def cli_state(providers: List[Dict[str, Any]]) -> str:
    """``none`` without an active CLI provider, ``ok`` when every binary is
    on PATH, ``fail`` otherwise. Never starts the CLI."""
    kinds = {
        str(p.get("auth_source")).removeprefix("subprocess:")
        for p in providers
        if _is_active(p)
        and not is_removed_provider(p)
        and str(p.get("auth_source") or "").startswith("subprocess:")
    }
    if not kinds:
        return "none"
    if all(cli_subprocess.check_cli_installed(kind).get("ok") for kind in kinds):
        return "ok"
    return "fail"


def auth_state(api_key: str, dev_mode: bool) -> str:
    if api_key:
        return "ok"
    return "dev" if dev_mode else "fail"


def ollama_state(providers: List[Dict[str, Any]], watchdog: Dict[str, Any]) -> str:
    """From the watchdog's last checks: ``not_configured``, ``unknown`` (watchdog
    stopped, no check, or a check older than three intervals), ``ok`` or
    ``unreachable``."""
    ollama = [p for p in providers if _is_active(p) and router._is_ollama_provider(p)]
    if not ollama:
        return "not_configured"
    if not watchdog.get("running"):
        return "unknown"
    max_age = 3 * float(watchdog.get("check_interval_s") or 0)
    states = watchdog.get("providers") or {}
    healthy = True
    for provider in ollama:
        state = states.get(provider.get("id"))
        if not state:
            return "unknown"
        ago = state.get("last_check_ago_s")
        if ago is None or ago > max_age:
            return "unknown"
        healthy = healthy and bool(state.get("is_healthy"))
    return "ok" if healthy else "unreachable"


def background_state(app_state: Any, watchdog: Dict[str, Any]) -> str:
    """``ok`` when the watchdog runs and each periodic job runs or is turned
    off; ``degraded`` otherwise."""
    if not watchdog.get("running"):
        return "degraded"
    for name in ("availability_job", "retention_job"):
        job = getattr(app_state, name, None)
        if job is None:
            return "degraded"
        status = job.status_dict
        if not (status.get("running") or status.get("disabled")):
            return "degraded"
    return "ok"


def _guard(name: str, failed: str, check, *args) -> Any:
    try:
        return check(*args)
    except Exception as exc:  # a broken check reports, it never raises
        log.warning("readiness: %s check raised %s", name, type(exc).__name__)
        return failed


async def _evaluate(app_state: Any, api_key: str, dev_mode: bool) -> Dict[str, Any]:
    try:
        # Only the SQLite lock leaves the loop: the watchdog state is
        # modified by the loop and read here.
        usage_db = await asyncio.to_thread(usage_db_state)
    except Exception as exc:
        log.warning("readiness: usage_db check raised %s", type(exc).__name__)
        usage_db = "fail"
    providers = _guard("providers", None, config.load_providers)
    watchdog = _guard("watchdog", None, provider_watchdog.get_watchdog_status)
    checks = {
        "usage_db": usage_db,
        "cli": "fail"
        if providers is None
        else _guard("cli", "fail", cli_state, providers),
        "auth": _guard("auth", "fail", auth_state, api_key, dev_mode),
        "ollama": "unknown"
        if providers is None or watchdog is None
        else _guard("ollama", "unknown", ollama_state, providers, watchdog),
        "background": "degraded"
        if watchdog is None
        else _guard("background", "degraded", background_state, app_state, watchdog),
    }
    ready = (
        checks["usage_db"] == "ok"
        and checks["cli"] != "fail"
        and (checks["auth"] != "fail")
    )
    return {"ready": ready, "checks": checks}


async def evaluate(app_state: Any, *, api_key: str, dev_mode: bool) -> Dict[str, Any]:
    """``{"ready": bool, "checks": {...}}``, evaluated at most once every
    ``CACHE_TTL_S`` seconds; concurrent callers share one evaluation."""
    global _cache, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_TTL_S:
            result = _cache[1]
        else:
            result = await _evaluate(app_state, api_key, dev_mode)
            _cache = (time.monotonic(), result)
    return {"ready": result["ready"], "checks": dict(result["checks"])}
