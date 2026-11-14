"""
Jobs — the background tasks Plugboard runs in-process, for the Jobs page.

- ``pricing_refresh``: the PricingScheduler (published prices into the monthly
  grid, every ``PLUGBOARD_PRICING_REFRESH_INTERVAL_H`` hours);
- ``model_refresh``: the ModelAvailabilityScheduler (model lists and
  connectivity probes);
- ``provider_watchdog``: the health monitor, continuous;
- ``availability_snapshots``: a PeriodicJob probing every active provider
  every ``PLUGBOARD_MONITORING_INTERVAL_S`` seconds for the uptime figures;
- ``data_retention``: a PeriodicJob purging old rows from the SQLite
  stores, daily at ``PLUGBOARD_RETENTION_HOUR_UTC``.

Nothing is scheduled outside the server: no crontab, no script. "Run now"
starts the scheduler's own update in the background, also for a job whose
scheduled runs are disabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_JOBS: Dict[str, Dict[str, Any]] = {
    "pricing_refresh": {
        "name": "Price refresh",
        "description": "Reads published prices (models.dev, LiteLLM, OpenRouter, Ollama) "
        "into this month's grid; large changes wait in Pricing → Review.",
        "state_attr": "pricing_scheduler",
    },
    "model_refresh": {
        "name": "Model catalog refresh",
        "description": "Refreshes every provider's model list and probes which models "
        "the connected accounts can really use.",
        "state_attr": "model_availability_scheduler",
    },
    "provider_watchdog": {
        "name": "Provider watchdog",
        "description": "Checks every active provider every 30 s and marks unhealthy ones inactive.",
        "continuous": True,
    },
    "availability_snapshots": {
        "name": "Availability snapshots",
        "description": "Probes every active provider and stores the result; the "
        "Monitoring page computes uptime from these snapshots.",
        "state_attr": "availability_job",
    },
    "data_retention": {
        "name": "Data retention",
        "description": "Deletes usage, audit, watchdog, monitoring and session rows "
        "older than their retention, then compacts the audit, watchdog and "
        "monitoring stores (never usage or sessions, which requests write).",
        "state_attr": "retention_job",
    },
}

# Strong references to manual runs, so a started task is not garbage collected.
_manual_runs: set = set()


def _scheduler(job: Dict[str, Any], app_state: Any) -> Optional[Any]:
    attr = job.get("state_attr")
    return getattr(app_state, attr, None) if attr and app_state is not None else None


def _status(sched: Any) -> Dict[str, Any]:
    """A scheduler's status: ``status_dict`` is a method on one, a property on the other."""
    value = sched.status_dict
    return dict(value() if callable(value) else value)


def _schedule_label(status: Dict[str, Any]) -> str:
    seconds = int(status.get("interval_s") or 0)
    if seconds > 0:
        return f"every {seconds // 60} min (in-process)"
    hours = int(status.get("refresh_interval_h") or 0)
    if hours > 0:
        return f"every {hours} h (in-process)"
    return f"daily at {int(status.get('daily_hour_utc') or 0):02d}:00 UTC (in-process)"


def _next_run(status: Dict[str, Any]) -> str:
    if status.get("disabled"):
        return "disabled"
    if status.get("running_since"):
        return "running now"
    seconds = int(status.get("interval_s") or 0)
    if seconds <= 0:
        seconds = int(status.get("refresh_interval_h") or 0) * 3600
    last = status.get("last_run_at") or status.get("last_success_at")
    if seconds > 0 and last:
        try:
            when = datetime.fromisoformat(str(last)) + timedelta(seconds=seconds)
            return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            pass
    return "—"


def _short_time(value: Any) -> Optional[str]:
    if not value:
        return None
    try:
        return (
            datetime.fromisoformat(str(value))
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC")
        )
    except ValueError:
        return str(value)


def _scheduler_state(status: Dict[str, Any]) -> str:
    """One word for the Jobs page. A job that is not working never reads as
    healthy: a loop that is no longer alive is ``stopped``, an open or degraded
    circuit breaker is ``error`` whatever its last success was."""
    if status.get("disabled"):
        return "disabled"
    if status.get("running_since"):
        return "running"
    if status.get("last_error") or status.get("cb_state") in ("OPEN", "DEGRADED"):
        return "error"
    if status.get("running") is False:
        return "stopped"
    if status.get("stale"):
        return "stale"
    if status.get("last_success_at"):
        return "success"
    return "never_run"


def _watchdog_status() -> Dict[str, Any]:
    from plugboard.provider_watchdog import get_watchdog_status

    return get_watchdog_status()


def _watchdog_state() -> str:
    try:
        return "running" if _watchdog_status().get("running") else "stopped"
    except Exception:  # noqa: BLE001 - a status that cannot be read is no proof
        log.warning("watchdog status unreadable", exc_info=True)
        return "error"


def _watchdog_last_run() -> Optional[str]:
    """Time of the most recent provider check, from the watchdog's own state."""
    try:
        ages = [
            p["last_check_ago_s"]
            for p in (_watchdog_status().get("providers") or {}).values()
            if p.get("last_check_ago_s") is not None
        ]
    except Exception:  # noqa: BLE001
        return None
    if not ages:
        return None
    when = datetime.now(timezone.utc) - timedelta(seconds=min(ages))
    return when.strftime("%Y-%m-%d %H:%M UTC")


def get_jobs_status(app_state: Any = None) -> List[Dict[str, Any]]:
    """Status of every background job, for ``GET /admin/jobs``."""
    result = []
    for job_id, job in _JOBS.items():
        if job.get("continuous"):
            result.append(
                {
                    "id": job_id,
                    "name": job["name"],
                    "description": job["description"],
                    "schedule": "continuous (every 30 s)",
                    "next_run": "continuous",
                    "last_run": _watchdog_last_run(),
                    "last_status": _watchdog_state(),
                }
            )
            continue
        sched = _scheduler(job, app_state)
        status = _status(sched) if sched is not None else {}
        result.append(
            {
                "id": job_id,
                "name": job["name"],
                "description": job["description"],
                "schedule": _schedule_label(status)
                if sched is not None
                else "not started",
                "next_run": _next_run(status),
                "last_run": _short_time(
                    status.get("last_run_at") or status.get("last_success_at")
                ),
                "last_status": _scheduler_state(status)
                if sched is not None
                else "not_started",
            }
        )
    return result


def get_job_log(job_id: str, lines: int = 50, app_state: Any = None) -> Dict[str, Any]:
    """Last result of a job (``GET /admin/jobs/{id}/log``)."""
    job = _JOBS.get(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}
    if job.get("continuous"):
        try:
            detail: Dict[str, Any] = _watchdog_status()
        except Exception as exc:  # noqa: BLE001
            detail = {"error": str(exc)}
        status = _watchdog_state()
    else:
        sched = _scheduler(job, app_state)
        detail = _status(sched) if sched is not None else {"started": False}
        status = _scheduler_state(detail) if sched is not None else "not_started"
    text = json.dumps(detail, indent=2, default=str)
    output_lines = text.splitlines()
    return {
        "id": job_id,
        "name": job["name"],
        "status": status,
        "output": "\n".join(output_lines[-max(1, lines) :]),
        "lines_total": len(output_lines),
    }


async def run_job(job_id: str, app_state: Any = None) -> Dict[str, Any]:
    """Start a job now (``POST /admin/jobs/{id}/run``); returns at once."""
    job = _JOBS.get(job_id)
    if job is None:
        return {"error": f"Job '{job_id}' not found"}
    if job.get("continuous"):
        return {
            "id": job_id,
            "name": job["name"],
            "status": "error",
            "error": f"'{job['name']}' runs continuously; it cannot be started by hand.",
        }
    sched = _scheduler(job, app_state)
    if sched is None:
        return {
            "id": job_id,
            "name": job["name"],
            "status": "error",
            "error": "scheduler not started",
        }
    if getattr(sched, "running_since", None):
        return {
            "id": job_id,
            "name": job["name"],
            "status": "running",
            "output": f"Already running since {sched.running_since}.",
        }

    async def _run() -> None:
        try:
            await sched.run_now("manual")
        except RuntimeError:
            log.info("job %s: already running, skipped", job_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("job %s failed: %s", job_id, exc)

    task = asyncio.create_task(_run())
    _manual_runs.add(task)
    task.add_done_callback(_manual_runs.discard)
    log.info("Job %s started by hand", job_id)
    return {
        "id": job_id,
        "name": job["name"],
        "status": "started",
        "output": "Started in the background. Refresh this page to see the result.",
    }
