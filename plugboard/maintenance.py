"""PeriodicJob — a small in-process asyncio job runner.

Runs one async ``work`` function either at a fixed interval or once a day
at a given UTC hour. The Jobs page reads it like the pricing schedulers:
``status_dict``, ``running_since`` and ``run_now("manual")``, which raises
``RuntimeError`` while a run is already in flight.

A failing run is recorded in ``last_error`` and the loop goes on; a
cancellation (``stop()``) propagates and is never recorded as an error.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

log = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]
WorkFn = Callable[[], Awaitable[Dict[str, Any]]]

_ERROR_MAX_CHARS = 300


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

    def _seconds_until_daily_hour(self) -> float:
        now = self._now().astimezone(timezone.utc)
        target = now.replace(
            hour=self.daily_hour_utc or 0, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _loop(self) -> None:
        first = True
        while True:
            if self.daily_hour_utc is not None:
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
