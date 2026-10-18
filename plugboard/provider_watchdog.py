"""
Provider Watchdog — monitors provider health and auto-restarts if down.

Provides:
- Background health check loop (configurable interval)
- Auto-restart via launcher script when provider is detected down
- Watchdog status tracking (last check, restart count, uptime)
- History of restart events in SQLite
- Integration with alerts.py for webhook notifications
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from plugboard.config import get_provider, load_providers
from plugboard.storage import data_dir
from plugboard.endpoints import openai_url

log = logging.getLogger(__name__)

_DATA_DIR = data_dir()
_DB_PATH = _DATA_DIR / "watchdog.db"
_PROJECT_DIR = Path(__file__).resolve().parent.parent

# Configuration (overridable via env)
_CHECK_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "30"))  # seconds
_FAILURE_THRESHOLD = int(os.environ.get("WATCHDOG_FAILURES", "3"))  # consecutive
_RESTART_COOLDOWN = int(os.environ.get("WATCHDOG_RESTART_COOLDOWN", "120"))  # seconds
_MAX_RESTARTS = int(os.environ.get("WATCHDOG_MAX_RESTARTS", "5"))  # per hour
_HEALTH_TIMEOUT = int(os.environ.get("WATCHDOG_HEALTH_TIMEOUT", "10"))  # seconds
_DEEP_CHECK_TIMEOUT = int(os.environ.get("WATCHDOG_DEEP_TIMEOUT", "60"))  # seconds
_DEEP_CHECK_INTERVAL = int(
    os.environ.get("WATCHDOG_DEEP_INTERVAL", "3")
)  # every N checks
_RESTART_TIMEOUT = int(os.environ.get("WATCHDOG_RESTART_TIMEOUT", "120"))  # seconds

# --- SQLite setup ---

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS watchdog_events (
    id              TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    provider_id     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    success         INTEGER NOT NULL DEFAULT 1,
    details         TEXT NOT NULL DEFAULT '',
    latency_ms      REAL NOT NULL DEFAULT 0
);
"""
_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_wd_ts ON watchdog_events(ts);"
_CREATE_INDEX2 = (
    "CREATE INDEX IF NOT EXISTS idx_wd_provider ON watchdog_events(provider_id, ts);"
)


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


def _log_event(
    provider_id: str,
    event_type: str,
    success: bool = True,
    details: str = "",
    latency_ms: float = 0,
) -> None:
    """Persist a watchdog event to SQLite."""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO watchdog_events (id, ts, provider_id, event_type, success, details, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                secrets.token_hex(8),
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                provider_id,
                event_type,
                1 if success else 0,
                details[:500],
                round(latency_ms, 1),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# --- In-memory state per provider ---


class _ProviderState:
    """Tracks health state for a single provider."""

    __slots__ = (
        "provider_id",
        "consecutive_failures",
        "last_check_ts",
        "last_healthy_ts",
        "last_restart_ts",
        "last_deep_check_ts",
        "last_deep_ok",
        "restart_count_hour",
        "restart_hour_start",
        "total_restarts",
        "is_healthy",
    )

    def __init__(self, provider_id: str):
        self.provider_id = provider_id
        self.consecutive_failures = 0
        self.last_check_ts: Optional[float] = None
        self.last_healthy_ts: Optional[float] = None
        self.last_restart_ts: Optional[float] = None
        self.last_deep_check_ts: Optional[float] = None
        self.last_deep_ok: Optional[bool] = None
        self.restart_count_hour = 0
        self.restart_hour_start = time.monotonic()
        self.total_restarts = 0
        self.is_healthy = True

    def record_healthy(self) -> None:
        self.consecutive_failures = 0
        self.last_check_ts = time.monotonic()
        self.last_healthy_ts = time.monotonic()
        self.is_healthy = True

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_check_ts = time.monotonic()
        self.is_healthy = False

    def can_restart(self) -> bool:
        """Check if restart is allowed (cooldown + max per hour)."""
        now = time.monotonic()

        # Reset hourly counter
        if now - self.restart_hour_start > 3600:
            self.restart_count_hour = 0
            self.restart_hour_start = now

        # Max restarts per hour
        if self.restart_count_hour >= _MAX_RESTARTS:
            return False

        # Cooldown since last restart
        if self.last_restart_ts and (now - self.last_restart_ts) < _RESTART_COOLDOWN:
            return False

        return True

    def record_restart(self) -> None:
        now = time.monotonic()
        self.last_restart_ts = now
        self.restart_count_hour += 1
        self.total_restarts += 1

    def record_deep_check(self, ok: bool) -> None:
        self.last_deep_check_ts = time.monotonic()
        self.last_deep_ok = ok

    def to_dict(self) -> Dict[str, Any]:
        now = time.monotonic()
        return {
            "provider_id": self.provider_id,
            "is_healthy": self.is_healthy,
            "consecutive_failures": self.consecutive_failures,
            "total_restarts": self.total_restarts,
            "restarts_this_hour": self.restart_count_hour,
            "last_check_ago_s": round(now - self.last_check_ts, 1)
            if self.last_check_ts
            else None,
            "last_healthy_ago_s": round(now - self.last_healthy_ts, 1)
            if self.last_healthy_ts
            else None,
            "last_restart_ago_s": round(now - self.last_restart_ts, 1)
            if self.last_restart_ts
            else None,
            "last_deep_check_ago_s": round(now - self.last_deep_check_ts, 1)
            if self.last_deep_check_ts
            else None,
            "last_deep_ok": self.last_deep_ok,
            "can_restart": self.can_restart(),
        }


_states: Dict[str, _ProviderState] = {}
_watchdog_task: Optional[asyncio.Task] = None
_running = False


def _get_state(provider_id: str) -> _ProviderState:
    if provider_id not in _states:
        _states[provider_id] = _ProviderState(provider_id)
    return _states[provider_id]


# --- Health check ---

_check_counter: Dict[str, int] = {}


async def _sovereign_health(base_url: str) -> Dict[str, Any]:
    """Ping a sovereign provider (e.g. Ollama) via GET / instead of /health."""
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                base_url,
                timeout=aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT),
            ) as resp:
                latency = (time.perf_counter() - t0) * 1000
                ok = resp.status < 500
                return {
                    "ok": ok,
                    "status": resp.status,
                    "latency_ms": round(latency, 1),
                }
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        return {"ok": False, "error": str(exc)[:200], "latency_ms": round(latency, 1)}


async def ping_provider(provider_id: str, force_deep: bool = False) -> Dict[str, Any]:
    """
    Check provider health. Two levels:
    1. Shallow: ping /health (every check)
    2. Deep: send a real completion request (every N checks, or on force)

    Deep check catches cases where /health returns OK but LLM backends
    are unreachable.

    Sovereign providers (e.g. Ollama) use GET / instead of /health,
    and skip deep checks (local inference, no gateway layer).
    """
    provider = get_provider(provider_id)
    if not provider:
        return {
            "ok": False,
            "error": "Provider not found",
            "latency_ms": 0,
            "check": "none",
        }

    base_url = provider["base_url"].rstrip("/")
    provider_type = provider.get("provider_type", "cloud")
    auth_source = provider.get("auth_source", "") or ""

    # Subprocess providers: no HTTP endpoint — delegate to shared probe.
    if auth_source.startswith("subprocess:"):
        from plugboard.health_probe import probe_provider

        result = await probe_provider(provider)
        return {**result, "check": "subprocess"}

    # Sovereign providers: simple GET / health, no deep check
    if provider_type == "sovereign":
        result = await _sovereign_health(base_url)
        return {**result, "check": "sovereign"}

    # Step 1: Shallow health check
    shallow = await _shallow_health(base_url)
    if not shallow["ok"]:
        return {**shallow, "check": "shallow"}

    # Step 2: Deep check (every N cycles, or forced)
    count = _check_counter.get(provider_id, 0) + 1
    _check_counter[provider_id] = count

    do_deep = force_deep or (count % _DEEP_CHECK_INTERVAL == 0)
    if do_deep:
        bearer = provider.get("bearer_token", "")
        model = provider.get("default_model", "")
        deep = await _deep_health(base_url, bearer, model)
        if not deep["ok"]:
            return {**deep, "check": "deep", "shallow_ok": True}
        return {
            "ok": True,
            "check": "deep",
            "latency_ms": deep["latency_ms"],
            "shallow_latency_ms": shallow["latency_ms"],
        }

    return {**shallow, "check": "shallow"}


async def _shallow_health(base_url: str) -> Dict[str, Any]:
    """Ping /health endpoint."""
    url = base_url + "/health"
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=_HEALTH_TIMEOUT),
            ) as resp:
                latency = (time.perf_counter() - t0) * 1000
                ok = resp.status < 500
                return {
                    "ok": ok,
                    "status": resp.status,
                    "latency_ms": round(latency, 1),
                }
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        return {"ok": False, "error": str(exc)[:200], "latency_ms": round(latency, 1)}


async def _deep_health(
    base_url: str, bearer_token: str, model: str = ""
) -> Dict[str, Any]:
    """Send a real LLM completion to verify the full pipeline works. Uses max_tokens=1."""
    url = openai_url(base_url, "chat/completions")
    body = {
        "model": model or "default",
        "messages": [{"role": "user", "content": "ping"}],
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
                timeout=aiohttp.ClientTimeout(total=_DEEP_CHECK_TIMEOUT),
            ) as resp:
                latency = (time.perf_counter() - t0) * 1000
                if resp.status == 200:
                    data = await resp.json()
                    if "choices" in data:
                        return {"ok": True, "latency_ms": round(latency, 1)}
                    return {
                        "ok": False,
                        "error": "No choices in response",
                        "latency_ms": round(latency, 1),
                    }
                text = await resp.text()
                return {
                    "ok": False,
                    "error": f"HTTP {resp.status}: {text[:200]}",
                    "latency_ms": round(latency, 1),
                }
    except asyncio.TimeoutError:
        latency = (time.perf_counter() - t0) * 1000
        return {
            "ok": False,
            "error": f"LLM completion timeout (>{_DEEP_CHECK_TIMEOUT}s)",
            "latency_ms": round(latency, 1),
        }
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        return {"ok": False, "error": str(exc)[:200], "latency_ms": round(latency, 1)}


# --- Restart logic ---


def _find_launcher_script() -> Optional[Path]:
    """Return the launcher script path when present and executable-ish."""
    candidates = (
        _PROJECT_DIR / "scripts" / "openclaw_launcher.sh",
        _PROJECT_DIR / "openclaw_launcher.sh",
    )
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def _load_dotenv_env() -> Dict[str, str]:
    """Best-effort .env parser from project root for launcher subprocess."""
    env_file = _PROJECT_DIR / ".env"
    loaded: Dict[str, str] = {}
    if not env_file.exists():
        return loaded
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            k = key.strip()
            if not k:
                continue
            v = value.strip()
            if len(v) >= 2 and (
                (v[0] == "'" and v[-1] == "'") or (v[0] == '"' and v[-1] == '"')
            ):
                v = v[1:-1]
            loaded[k] = v
    except OSError as exc:
        log.warning("Watchdog: failed reading .env (%s)", exc)
    return loaded


def restart_provider(provider_id: str) -> Dict[str, Any]:
    """Restart provider via launcher script when available."""
    script = _find_launcher_script()
    if script is None:
        msg = "Launcher script not found"
        _log_event(provider_id, "restart", success=False, details=msg)
        return {"success": False, "error": msg}

    env = os.environ.copy()
    env.update(_load_dotenv_env())
    try:
        proc = subprocess.run(
            ["bash", str(script)],
            cwd=str(_PROJECT_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=_RESTART_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"restart timeout after {exc.timeout}s"
        _log_event(provider_id, "restart", success=False, details=msg)
        return {"success": False, "error": msg}
    except Exception as exc:
        msg = f"restart failed: {exc}"
        _log_event(provider_id, "restart", success=False, details=msg)
        return {"success": False, "error": msg}

    output = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    success = proc.returncode == 0
    details = output if success else (err or output or f"exit {proc.returncode}")
    _log_event(provider_id, "restart", success=success, details=details)
    if success:
        return {"success": True, "exit_code": proc.returncode, "output": output}
    return {
        "success": False,
        "exit_code": proc.returncode,
        "error": err or output or f"exit {proc.returncode}",
        "output": output,
    }


# --- Watchdog loop ---


async def _watchdog_loop() -> None:
    """Background loop: check providers, restart if down."""
    global _running
    _running = True
    log.info(
        "Watchdog started (interval=%ds, threshold=%d, cooldown=%ds)",
        _CHECK_INTERVAL,
        _FAILURE_THRESHOLD,
        _RESTART_COOLDOWN,
    )

    while _running:
        try:
            providers = load_providers()
            for p in providers:
                if not p.get("is_active", True):
                    continue

                pid = p["id"]
                state = _get_state(pid)
                result = await ping_provider(pid)
                check_type = result.get("check", "shallow")

                if result.get("ok"):
                    state.record_healthy()
                    if check_type == "deep":
                        state.record_deep_check(True)
                    _log_event(
                        pid,
                        f"health_{check_type}",
                        success=True,
                        latency_ms=result.get("latency_ms", 0),
                    )
                else:
                    state.record_failure()
                    if check_type == "deep":
                        state.record_deep_check(False)
                    error_msg = result.get("error", f"HTTP {result.get('status', '?')}")
                    # Note if shallow was OK but deep failed
                    if result.get("shallow_ok"):
                        error_msg = f"[deep fail, shallow OK] {error_msg}"
                    _log_event(
                        pid,
                        f"health_{check_type}",
                        success=False,
                        details=error_msg,
                        latency_ms=result.get("latency_ms", 0),
                    )
                    log.warning(
                        "Watchdog: provider '%s' unhealthy (%d/%d) — %s",
                        pid,
                        state.consecutive_failures,
                        _FAILURE_THRESHOLD,
                        error_msg,
                    )

                    # Auto-restart if threshold reached
                    # Skip restart for sovereign providers (lifecycle managed externally)
                    is_sovereign = p.get("provider_type", "cloud") == "sovereign"
                    if (
                        not is_sovereign
                        and state.consecutive_failures >= _FAILURE_THRESHOLD
                    ):
                        if state.can_restart():
                            log.warning("Watchdog: triggering restart for '%s'", pid)
                            restart_result = restart_provider(pid)
                            state.record_restart()

                            if restart_result.get("success"):
                                log.info("Watchdog: restart of '%s' succeeded", pid)
                                # Send alert about recovery
                                try:
                                    from plugboard.alerts import (
                                        record_provider_success,
                                    )

                                    record_provider_success(pid)
                                except Exception:
                                    pass
                            else:
                                log.error(
                                    "Watchdog: restart of '%s' failed: %s",
                                    pid,
                                    restart_result.get("error", ""),
                                )
                                try:
                                    from plugboard.alerts import (
                                        record_provider_failure,
                                    )

                                    await record_provider_failure(pid)
                                except Exception:
                                    pass
                        else:
                            log.warning(
                                "Watchdog: cannot restart '%s' (cooldown or max restarts reached)",
                                pid,
                            )

        except Exception as exc:
            log.error("Watchdog loop error: %s", exc)

        await asyncio.sleep(_CHECK_INTERVAL)


def start_watchdog() -> Optional[asyncio.Task]:
    """Start the background watchdog task. Returns the asyncio.Task."""
    global _watchdog_task, _running
    if _watchdog_task and not _watchdog_task.done():
        log.info("Watchdog already running")
        return _watchdog_task
    _running = True
    _watchdog_task = asyncio.create_task(_watchdog_loop())
    return _watchdog_task


def stop_watchdog() -> None:
    """Stop the background watchdog task."""
    global _running, _watchdog_task
    _running = False
    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()
    _watchdog_task = None
    log.info("Watchdog stopped")


# --- Query functions ---


def get_watchdog_status() -> Dict[str, Any]:
    """Get current watchdog status and all provider states."""
    return {
        "running": _running
        and _watchdog_task is not None
        and not _watchdog_task.done(),
        "check_interval_s": _CHECK_INTERVAL,
        "failure_threshold": _FAILURE_THRESHOLD,
        "restart_cooldown_s": _RESTART_COOLDOWN,
        "max_restarts_per_hour": _MAX_RESTARTS,
        "providers": {pid: st.to_dict() for pid, st in _states.items()},
    }


def get_watchdog_events(
    provider_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Retrieve watchdog event history from SQLite."""
    conn = _get_conn()
    try:
        query = "SELECT * FROM watchdog_events WHERE 1=1"
        params: list = []
        if provider_id:
            query += " AND provider_id = ?"
            params.append(provider_id)
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_restart_stats(hours: int = 24) -> Dict[str, Any]:
    """Get restart statistics over the last N hours."""
    conn = _get_conn()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )
        rows = conn.execute(
            """SELECT provider_id,
                      COUNT(*) as total_restarts,
                      SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successful,
                      SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failed
               FROM watchdog_events
               WHERE event_type = 'restart' AND ts >= ?
               GROUP BY provider_id""",
            (cutoff,),
        ).fetchall()

        return {
            "period_hours": hours,
            "providers": {
                r["provider_id"]: {
                    "total_restarts": r["total_restarts"],
                    "successful": r["successful"],
                    "failed": r["failed"],
                }
                for r in rows
            },
        }
    finally:
        conn.close()
