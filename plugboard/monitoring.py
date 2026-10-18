"""
Monitoring — real-time health check of providers and models.

Provides:
- Provider health status (ping /health endpoint)
- Model availability (quick completion test)
- Latency + error rate from metrics
- Historical snapshots in SQLite (data/monitoring.db)
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp  # noqa: F401 — still used by check_model_availability below

from plugboard.config import get_provider, load_providers
from plugboard.metrics import get_stats
from plugboard.storage import data_dir
from plugboard.endpoints import openai_url

log = logging.getLogger(__name__)

_DATA_DIR = data_dir()
_DB_PATH = _DATA_DIR / "monitoring.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS monitoring_snapshots (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    is_healthy  INTEGER NOT NULL DEFAULT 1,
    latency_ms  REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    models_up   INTEGER NOT NULL DEFAULT 0,
    models_down INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_mon_ts ON monitoring_snapshots(ts);"
_CREATE_INDEX2 = "CREATE INDEX IF NOT EXISTS idx_mon_provider ON monitoring_snapshots(provider_id, ts);"


def _get_conn() -> sqlite3.Connection:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEX)
    conn.execute(_CREATE_INDEX2)
    conn.commit()
    return conn


async def check_provider_health(provider_id: str) -> Dict[str, Any]:
    """Health check honouring auth_source (subprocess/cloud/sovereign).

    Delegates to plugboard.health_probe.probe_provider so this function,
    the dashboard endpoint, and the watchdog all agree on "healthy".
    """
    provider = get_provider(provider_id)
    if not provider:
        return {"provider_id": provider_id, "ok": False, "error": "Provider not found"}

    from plugboard.health_probe import probe_provider

    result = await probe_provider(provider)
    return {"provider_id": provider_id, **result}


async def check_model_availability(
    model_id: str,
    bearer_token: str,
    base_url: str = "http://localhost:8765",
) -> Dict[str, Any]:
    """Quick completion test on a model. Returns availability dict."""
    url = openai_url(base_url, "chat/completions")
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": "OK"}],
        "max_tokens": 1,
    }
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {bearer_token}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                latency = (time.perf_counter() - t0) * 1000
                ok = resp.status == 200
                return {
                    "model": model_id,
                    "ok": ok,
                    "status": resp.status,
                    "latency_ms": round(latency, 1),
                }
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        return {
            "model": model_id,
            "ok": False,
            "error": str(exc)[:200],
            "latency_ms": round(latency, 1),
        }


async def full_health_check(admin_key: str) -> Dict[str, Any]:
    """
    Complete monitoring snapshot:
    1. Check each provider's /health
    2. Get metrics (latency, errors, requests)
    3. Save snapshot to SQLite

    Returns full monitoring report.
    """
    providers = load_providers()
    metrics = get_stats()
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    report: Dict[str, Any] = {
        "ts": ts,
        "providers": [],
        "summary": {
            "total_providers": len(providers),
            "healthy": 0,
            "unhealthy": 0,
        },
    }

    conn = _get_conn()
    try:
        for p in providers:
            pid = p["id"]
            is_active = p.get("is_active", True)

            if not is_active:
                report["providers"].append(
                    {
                        "id": pid,
                        "name": p.get("name", pid),
                        "active": False,
                        "health": {"ok": False, "error": "inactive"},
                        "metrics": {},
                    }
                )
                report["summary"]["unhealthy"] += 1
                continue

            # Health check
            health = await check_provider_health(pid)

            # Metrics
            m = metrics.get(pid, {})
            error_rate = 0.0
            if m.get("requests", 0) > 0:
                error_rate = round(m["errors"] / m["requests"] * 100, 1)

            provider_report = {
                "id": pid,
                "name": p.get("name", pid),
                "active": True,
                "base_url": p.get("base_url", ""),
                "health": health,
                "metrics": {
                    "requests": m.get("requests", 0),
                    "errors": m.get("errors", 0),
                    "error_rate_pct": error_rate,
                    "latency_avg_ms": round(m.get("latency_avg", 0) * 1000, 1),
                },
                "models_count": len(p.get("models", [])),
            }

            if health.get("ok"):
                report["summary"]["healthy"] += 1
            else:
                report["summary"]["unhealthy"] += 1

            report["providers"].append(provider_report)

            # Save snapshot
            conn.execute(
                """INSERT INTO monitoring_snapshots
                   (id, ts, provider_id, is_healthy, latency_ms, error, models_up, models_down)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    secrets.token_hex(8),
                    ts,
                    pid,
                    1 if health.get("ok") else 0,
                    health.get("latency_ms", 0),
                    # `health["error"]` is None when healthy (see
                    # health_probe._probe_subprocess) — coerce to ""
                    # because the schema column is NOT NULL.
                    health.get("error") or "",
                    len(p.get("models", [])),
                    0,
                ),
            )

        conn.commit()
    finally:
        conn.close()

    return report


def get_monitoring_history(
    provider_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Retrieve monitoring history from SQLite."""
    conn = _get_conn()
    try:
        if provider_id:
            rows = conn.execute(
                "SELECT * FROM monitoring_snapshots WHERE provider_id = ? ORDER BY ts DESC LIMIT ?",
                (provider_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM monitoring_snapshots ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_uptime_stats(provider_id: str, hours: int = 24) -> Dict[str, Any]:
    """Calculate uptime percentage for a provider over the last N hours."""
    conn = _get_conn()
    try:
        cutoff = datetime.now(timezone.utc)
        from datetime import timedelta

        cutoff_str = (cutoff - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")

        rows = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN is_healthy = 1 THEN 1 ELSE 0 END) as healthy
               FROM monitoring_snapshots
               WHERE provider_id = ? AND ts >= ?""",
            (provider_id, cutoff_str),
        ).fetchone()

        total = rows["total"] or 0
        healthy = rows["healthy"] or 0
        uptime = round((healthy / total) * 100, 2) if total > 0 else 100.0

        return {
            "provider_id": provider_id,
            "period_hours": hours,
            "checks": total,
            "healthy": healthy,
            "unhealthy": total - healthy,
            "uptime_pct": uptime,
        }
    finally:
        conn.close()
