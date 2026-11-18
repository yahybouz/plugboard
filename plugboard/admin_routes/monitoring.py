"""Cache, Claude quota, monitoring, watchdog, model status and audit (admin).

Routes ``/admin/cache*``, ``/admin/claude-quota``, ``/admin/monitoring*``,
``/admin/watchdog*``, ``/admin/models/status``, ``/admin/models/refresh``,
``/admin/audit`` and ``/metrics/prometheus``, moved verbatim from
``plugboard/app.py`` (PLU-36). ``core`` is the ``plugboard.app`` module that
registers them: the names the tests patch there, and the refresh state that
stays there (``_MODEL_REFRESH_LOCK``, ``_MODEL_REFRESH_COOLDOWN_S``,
``_LAST_REFRESH_AT``), are read as ``core.<name>`` when a handler runs; the
refresh stamp is written back as ``core._LAST_REFRESH_AT``.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from plugboard.audit import count_audit_log, get_audit_log

log = logging.getLogger("plugboard.app")

_MODEL_REFRESH_TIMEOUT_S = 120


_REASON_NORMALISATION = {
    # Map verbose/variable reason strings to stable short codes.
    # Keeps the public API surface stable regardless of internal wording changes.
    "empty completion": "empty_response",
    "subprocess timeout": "timeout",
    "ollama_daemon_unreachable": "daemon_unreachable",
}


def _normalise_reason(raw: Optional[str]) -> Optional[str]:
    """Return a normalised short reason code, or None."""
    if not raw:
        return None
    lower = raw.lower()
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if "404" in raw or "not found" in lower:
        return "http_404"
    if "empty" in lower:
        return "empty_response"
    if "ollama_daemon_unreachable" in lower or "daemon_unreachable" in lower:
        return "daemon_unreachable"
    return _REASON_NORMALISATION.get(raw, raw)


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Declare this block's routes on ``app``, in their original order."""

    # -- /admin/cache ------------------------------------------------------------

    @app.get("/admin/cache/stats")
    async def cache_stats(_: None = Depends(core._require_admin)):
        """Return cache statistics (entries, hits, cost saved).

        The semantic cache is opt-in and may not be initialised (missing dep,
        chmod failure, schema migration crash). We isolate its failure so the
        exact-cache stats remain available.
        """
        from plugboard.cache import get_cache_stats

        semantic_stats: Dict[str, Any]
        try:
            from plugboard import semantic_cache as _sem

            semantic_stats = _sem.get_stats()
        except Exception as exc:
            log.warning("semantic_cache.get_stats failed: %s", exc)
            semantic_stats = {"enabled": False, "error": str(exc)}

        return {
            "exact": get_cache_stats(),
            "semantic": semantic_stats,
        }

    @app.delete("/admin/cache", status_code=200)
    async def clear_cache_endpoint(_: None = Depends(core._require_admin)):
        """Clear all cached responses."""
        from plugboard.cache import clear_cache

        deleted = clear_cache()
        core._audit("cache.clear", details=f"deleted={deleted}")
        return {"deleted": deleted}

    # -- /admin/monitoring -------------------------------------------------------

    @app.get("/admin/claude-quota")
    async def admin_claude_quota(_: None = Depends(core._require_admin)):
        """The last Claude quota limit real calls met, and the count per provider
        (PLU-67). Read from data/claude_quota.json: no provider is called."""
        from plugboard import claude_quota

        return await _asyncio.to_thread(claude_quota.snapshot)

    @app.get("/admin/monitoring")
    async def admin_monitoring(_: None = Depends(core._require_admin)):
        """Full health check of all providers + metrics + uptime."""
        from plugboard.monitoring import full_health_check, get_uptime_stats

        report = await full_health_check()

        # Add uptime stats for each provider
        for p in report["providers"]:
            if p.get("active"):
                p["uptime_24h"] = get_uptime_stats(p["id"], hours=24)
                p["uptime_7d"] = get_uptime_stats(p["id"], hours=168)

        return report

    @app.get("/admin/monitoring/history")
    async def admin_monitoring_history(
        provider_id: Optional[str] = None,
        limit: int = 100,
        _: None = Depends(core._require_admin),
    ):
        """Retrieve monitoring history snapshots."""
        from plugboard.monitoring import get_monitoring_history

        return {"entries": get_monitoring_history(provider_id=provider_id, limit=limit)}

    # -- /admin/watchdog ---------------------------------------------------------

    @app.get("/admin/watchdog")
    async def admin_watchdog_status(_: None = Depends(core._require_admin)):
        """Get watchdog status and all provider states."""
        from plugboard.provider_watchdog import get_watchdog_status

        return get_watchdog_status()

    @app.post("/admin/watchdog/start")
    async def admin_watchdog_start(_: None = Depends(core._require_admin)):
        """Start the provider watchdog background task."""
        from plugboard.provider_watchdog import start_watchdog, get_watchdog_status

        start_watchdog()
        core._audit("watchdog.start")
        return get_watchdog_status()

    @app.post("/admin/watchdog/stop")
    async def admin_watchdog_stop(_: None = Depends(core._require_admin)):
        """Stop the provider watchdog background task."""
        from plugboard.provider_watchdog import stop_watchdog, get_watchdog_status

        stop_watchdog()
        core._audit("watchdog.stop")
        return get_watchdog_status()

    @app.post("/admin/watchdog/{provider_id}/restart")
    async def admin_watchdog_restart(
        provider_id: str, _: None = Depends(core._require_admin)
    ):
        """Manually trigger a provider restart via the launcher script."""
        from plugboard.provider_watchdog import restart_provider

        result = restart_provider(provider_id)
        core._audit(
            "watchdog.restart",
            target_type="provider",
            target_id=provider_id,
            details="success"
            if result.get("success")
            else result.get("error", "failed"),
        )
        if not result.get("success"):
            raise HTTPException(500, result.get("error", "Restart failed"))
        return result

    @app.get("/admin/watchdog/events")
    async def admin_watchdog_events(
        provider_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
        _: None = Depends(core._require_admin),
    ):
        """Retrieve watchdog event history."""
        from plugboard.provider_watchdog import get_watchdog_events

        return {
            "entries": get_watchdog_events(
                provider_id=provider_id, event_type=event_type, limit=limit
            )
        }

    @app.get("/admin/watchdog/restarts")
    async def admin_watchdog_restart_stats(
        hours: int = 24,
        _: None = Depends(core._require_admin),
    ):
        """Get restart statistics over the last N hours."""
        from plugboard.provider_watchdog import get_restart_stats

        return get_restart_stats(hours=hours)

    @app.get("/admin/models/status")
    async def admin_models_status(_: None = Depends(core._require_admin)):
        """Return persisted model connectivity status and scheduler metadata.

        Note: the ``status_file`` path is intentionally omitted from the response
        to avoid leaking absolute filesystem paths (MN-2). The ``reason`` field
        is normalised to short codes (``timeout``, ``http_404``, ``empty_response``,
        ``daemon_unreachable``) for API stability; detailed messages are in logs.
        """
        from plugboard.model_availability import load_model_status

        status = load_model_status()
        scheduler_meta: dict = {}
        try:
            sched = getattr(app.state, "model_availability_scheduler", None)
            if sched is not None:
                scheduler_meta = sched.status_dict
        except Exception:
            pass

        # Normalise reason codes — strip verbose/internal details from public API.
        raw_models: Dict[str, Any] = status.get("models", {})
        normalised_models = {
            k: {**v, "reason": _normalise_reason(v.get("reason"))}
            for k, v in raw_models.items()
        }

        return {
            "last_run_at": status.get("last_run_at"),
            "model_count": len(raw_models),
            "models": normalised_models,
            "scheduler": scheduler_meta,
        }

    @app.post("/admin/models/refresh")
    async def admin_models_refresh(_: None = Depends(core._require_admin)):
        """Trigger a synchronous connectivity re-probe for all active providers.

        Blocks until complete (timeout: 120s). Returns the updated status.
        A 60-second cooldown (configurable via PLUGBOARD_REFRESH_COOLDOWN_S)
        prevents sequential spam: repeated calls within the cooldown window
        receive HTTP 429 with a Retry-After header.
        """
        from fastapi.responses import JSONResponse

        from plugboard.model_availability import run_connectivity_probes

        if core._MODEL_REFRESH_LOCK.locked():
            raise HTTPException(
                status_code=409, detail="A refresh is already in progress"
            )

        elapsed = time.monotonic() - core._LAST_REFRESH_AT
        remaining = core._MODEL_REFRESH_COOLDOWN_S - elapsed
        if remaining > 0:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(int(remaining) + 1)},
                content={
                    "detail": f"Refresh cooldown active, retry in {int(remaining) + 1}s"
                },
            )

        async with core._MODEL_REFRESH_LOCK:
            try:
                result = await _asyncio.wait_for(
                    run_connectivity_probes(), timeout=_MODEL_REFRESH_TIMEOUT_S
                )
            except _asyncio.TimeoutError:
                raise HTTPException(
                    status_code=504,
                    detail=f"Connectivity probe timed out after {_MODEL_REFRESH_TIMEOUT_S}s",
                )
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc

        core._LAST_REFRESH_AT = time.monotonic()
        core._audit("models.refresh")
        core.invalidate_models_cache()
        return {
            "last_run_at": result.get("last_run_at"),
            "model_count": len(result.get("models", {})),
            "models": result.get("models", {}),
        }

    # -- /admin/audit ------------------------------------------------------------

    @app.get("/admin/audit")
    async def admin_audit_log(
        limit: int = 50,
        offset: int = 0,
        action: Optional[str] = None,
        _: None = Depends(core._require_admin),
    ):
        """Retrieve admin audit trail (newest first)."""
        entries = get_audit_log(limit=limit, offset=offset, action=action)
        total = count_audit_log(action=action)
        return {"entries": entries, "total": total, "limit": limit, "offset": offset}

    @app.get("/metrics/prometheus", include_in_schema=False)
    async def prometheus_metrics(_: None = Depends(core._require_admin)):
        from plugboard.metrics import format_prometheus

        return PlainTextResponse(
            content=format_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return SimpleNamespace(admin_models_refresh=admin_models_refresh)
