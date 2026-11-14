"""PeriodicJob — a small in-process asyncio job runner.

Runs one async ``work`` function either at a fixed interval or once a day
at a given UTC hour. The Jobs page reads it like the pricing schedulers:
``status_dict``, ``running_since`` and ``run_now("manual")``, which raises
``RuntimeError`` while a run is already in flight.

A failing run is recorded in ``last_error`` and the loop goes on; a
cancellation (``stop()``) propagates and is never recorded as an error.

Two jobs are built here and started with the server:

- ``availability_job()``: availability snapshots every
  ``PLUGBOARD_MONITORING_INTERVAL_S`` seconds;
- ``retention_job()``: the daily data retention pass; a pass with a store
  in error is a failed run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from plugboard import envcompat, monitoring, retention, storage

log = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]
WorkFn = Callable[[], Awaitable[Dict[str, Any]]]

_ERROR_MAX_CHARS = 300

DEFAULT_MONITORING_INTERVAL_S = 300
MIN_MONITORING_INTERVAL_S = 60


class PeriodicJob:
    """Run ``work`` every ``interval_s`` seconds or daily at ``daily_hour_utc``."""

    def __init__(
        self,
        name: str,
        work: WorkFn,
        *,
        interval_s: int = 0,
        daily_hour_utc: Optional[int] = None,
        first_delay_s: float = 60.0,
        disabled: bool = False,
        sleep_fn: Optional[SleepFn] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        scheduled_guard: Optional[Callable[[], Optional[str]]] = None,
        catch_up: Optional[Callable[[], bool]] = None,
    ) -> None:
        if (interval_s > 0) == (daily_hour_utc is not None):
            raise ValueError(
                f"{name}: give exactly one schedule, interval_s > 0 or daily_hour_utc"
            )
        if daily_hour_utc is not None and not 0 <= daily_hour_utc <= 23:
            raise ValueError(f"{name}: daily_hour_utc must be between 0 and 23")

        self.name = name
        self._work = work
        self.interval_s = interval_s
        self.daily_hour_utc = daily_hour_utc
        self.first_delay_s = first_delay_s
        self.disabled = disabled
        self._sleep = sleep_fn or asyncio.sleep
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        # Asked before each scheduled run, never before a manual one: a
        # message holds the run and is recorded as its error.
        self._scheduled_guard = scheduled_guard
        # Asked once when the loop starts: True runs a daily job after
        # first_delay_s instead of waiting for the next daily hour (BUG-72).
        self._catch_up = catch_up

        self._task: Optional[asyncio.Task] = None
        self._run_lock = asyncio.Lock()
        self.running_since: Optional[str] = None
        self.started_at: Optional[str] = None
        self.last_run_at: Optional[str] = None
        self.last_success_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_result: Optional[Dict[str, Any]] = None
        self.runs = 0

    # -- public API ----------------------------------------------------------

    async def start(self) -> None:
        """Start the loop in the background; no-op if disabled or running."""
        if self.disabled:
            log.info("PeriodicJob %s disabled", self.name)
            return
        if self._task is not None and not self._task.done():
            return
        self.started_at = self._now().isoformat()
        self._task = asyncio.create_task(self._loop(), name=f"periodic-{self.name}")
        self._task.add_done_callback(self._on_task_done)

    async def stop(self) -> None:
        """Cancel the loop (and any run in flight) and wait for it."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        finally:
            self._task = None

    async def run_now(self, kind: str = "manual") -> None:
        """Run once now; raises RuntimeError while another run is in flight."""
        if self._run_lock.locked():
            raise RuntimeError("already running")
        await self._run_once(kind)

    @property
    def task(self) -> Optional[asyncio.Task]:
        """The background loop task, for tests and observability."""
        return self._task

    @property
    def status_dict(self) -> Dict[str, Any]:
        """Machine-readable status, in the shape the Jobs page reads."""
        return {
            "name": self.name,
            "disabled": self.disabled,
            "running": self._task is not None and not self._task.done(),
            "interval_s": self.interval_s,
            "daily_hour_utc": self.daily_hour_utc,
            "refresh_interval_h": 0,
            "running_since": self.running_since,
            "started_at": self.started_at,
            "last_run_at": self.last_run_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "runs": self.runs,
        }

    # -- internals -----------------------------------------------------------

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("PeriodicJob %s crashed: %s", self.name, exc, exc_info=exc)
            # The Jobs page must show a loop that ended, not its last success.
            self.last_error = f"loop stopped: {type(exc).__name__}: {exc}"[
                :_ERROR_MAX_CHARS
            ]

    def _seconds_until_daily_hour(self) -> float:
        now = self._now().astimezone(timezone.utc)
        target = now.replace(
            hour=self.daily_hour_utc or 0, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    def catches_up(self) -> bool:
        """Whether a daily job's first run is now, a missed one (BUG-72)."""
        if self.daily_hour_utc is None or self._catch_up is None:
            return False
        try:
            return bool(self._catch_up())
        except Exception as exc:  # noqa: BLE001 - never stops the loop
            log.warning("PeriodicJob %s catch-up check failed: %s", self.name, exc)
            return False

    def _guard_reason(self) -> Optional[str]:
        """The scheduled guard's reason to hold the run; a guard that fails
        holds it too, rather than ending the loop (M-1)."""
        if self._scheduled_guard is None:
            return None
        try:
            return self._scheduled_guard()
        except Exception as exc:  # noqa: BLE001 - recorded as the held reason
            return (
                f"held: the check before the run failed ({type(exc).__name__}: {exc})"
            )

    async def _loop(self) -> None:
        first = True
        while True:
            if first and self.catches_up():
                delay = self.first_delay_s
            elif self.daily_hour_utc is not None:
                delay = self._seconds_until_daily_hour()
            else:
                delay = self.first_delay_s if first else self.interval_s
            first = False
            await self._sleep(delay)
            await self._run_once("scheduled")

    async def _run_once(self, kind: str) -> None:
        # One run at a time: a scheduled tick that finds a manual run in
        # progress (or the reverse) is skipped rather than run alongside it.
        if self._run_lock.locked():
            log.info(
                "PeriodicJob %s %s run skipped: another run started at %s",
                self.name,
                kind,
                self.running_since,
            )
            return
        async with self._run_lock:
            held = self._guard_reason() if kind != "manual" else None
            if held:
                log.warning("PeriodicJob %s %s run %s", self.name, kind, held)
                self.last_error = held[:_ERROR_MAX_CHARS]
                self.runs += 1
                self.last_run_at = self._now().isoformat()
                return
            self.running_since = self._now().isoformat()
            try:
                result = await self._work()
            except Exception as exc:  # noqa: BLE001 - recorded, the loop goes on
                self.last_error = f"{type(exc).__name__}: {exc}"[:_ERROR_MAX_CHARS]
                log.warning("PeriodicJob %s %s run failed: %s", self.name, kind, exc)
            else:
                self.last_result = result
                self.last_success_at = self._now().isoformat()
                self.last_error = None
            finally:
                self.running_since = None
            self.runs += 1
            self.last_run_at = self._now().isoformat()


def availability_job() -> PeriodicJob:
    """Availability snapshots every ``PLUGBOARD_MONITORING_INTERVAL_S`` seconds.

    ``0`` turns the snapshots off; 1 to 59 gives 60, so that probing every
    provider cannot run back to back.
    """
    interval = envcompat.get_int(
        "PLUGBOARD_MONITORING_INTERVAL_S",
        default=DEFAULT_MONITORING_INTERVAL_S,
        minimum=0,
    )
    disabled = interval == 0
    if disabled:
        interval = DEFAULT_MONITORING_INTERVAL_S
    return PeriodicJob(
        "availability_snapshots",
        monitoring.record_snapshot,
        interval_s=max(interval, MIN_MONITORING_INTERVAL_S),
        disabled=disabled,
    )


async def _retention_pass() -> Dict[str, Any]:
    # The pass blocks on SQLite: it runs in a worker thread, which a stop()
    # cannot interrupt, so the job waits for it before letting the
    # cancellation go on.
    report, cancelled = await storage.to_thread_to_the_end(retention.run_retention)
    if cancelled is not None:
        raise cancelled
    # A store in error fails the run, so that the Jobs page shows it; the
    # report is already written and GET /admin/retention gives the details.
    if report.get("errors"):
        failed = sorted(
            name for name, store in report["stores"].items() if "error" in store
        )
        raise RuntimeError(
            f"{len(failed)} store(s) in error: {', '.join(failed)}; "
            "see GET /admin/retention"
        )
    return report


def retention_job() -> PeriodicJob:
    """The data retention pass, daily at ``PLUGBOARD_RETENTION_HOUR_UTC``."""
    settings = retention.settings()
    return PeriodicJob(
        "data_retention",
        _retention_pass,
        daily_hour_utc=settings["hour_utc"],
        disabled=settings["disabled"],
        scheduled_guard=retention.clock_check,
        catch_up=retention.pass_overdue,
    )
