"""
PricingScheduler — internal asyncio scheduler that keeps pricing fresh
without an external crontab (the Jobs page shows it and can start a run).

Behavior:
  * Bootstrap : once providers are loaded, call ``update_current_pricing()``
    so a fresh deployment has prices immediately (no need to wait for the
    next 03:00 UTC tick).
  * Daily loop : sleep until ``PRICING_DAILY_HOUR_UTC:00`` UTC, then call
    ``update_current_pricing()`` again. Recomputes the next deadline at
    every iteration (handles month rollover automatically).
  * Circuit breaker : 3 consecutive failures → OPEN, cooldown
    ``PRICING_CB_COOLDOWN_S`` seconds, then HALF_OPEN probe → CLOSED on
    success.

The scheduler is opt-out via ``PRICING_SCHEDULER_DISABLED=1``. The legacy
external scripts remain functional thanks to ``update_current_pricing``
idempotence; operators who keep crontab entries simply double-trigger the
update — both runs converge on the same on-disk state.

# APScheduler / Celery / Huey were considered and rejected: a single
# daily cron is 40 lines of native asyncio + zero new dependency. The
# overhead of an external scheduler is unjustified for this volume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from plugboard.storage import data_dir

log = logging.getLogger(__name__)


# ARCH-DEBT-036: bound the in-memory failure counter so a long pathological
# run cannot integer-overflow it. 1000 is far above any realistic
# consecutive-failure scenario (CB opens at 3, real bugs surface by 10).
_FAILURE_COUNTER_CAP = 1000

# ARCH-DEBT-033: persist circuit breaker state across restarts so an OOM
# crash loop doesn't repeatedly hit a known-bad upstream and burn the
# error budget. Stored next to other scheduler artefacts in data/.
_CB_STATE_FILE = data_dir() / "cb_state.json"


def _parse_bool(value: str) -> bool:
    """Match the project-wide convention used by PLUGBOARD_STRICT_TENANT
    and PLUGBOARD_SEMANTIC_CACHE — accept the same truthy strings."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or str(default))
    except (TypeError, ValueError):
        return default


# Type alias: a sleep function compatible with asyncio.sleep. Injectable
# so tests don't have to monkeypatch the global asyncio.sleep (which
# breaks asyncio.wait_for and other internals — see CPython issue 83175).
SleepFn = Callable[[float], Awaitable[None]]


class PricingScheduler:
    """Internal cron-replacement that bootstraps + refreshes pricing daily.

    The class encapsulates the loop so tests can inject a custom sleep,
    a custom update function, and a custom clock without monkeypatching
    asyncio internals.
    """

    def __init__(
        self,
        *,
        update_fn: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
        providers_fn: Optional[Callable[[], list]] = None,
        sleep_fn: Optional[SleepFn] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        cb_state_file: Optional[Path] = None,
        catalog_fn: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
        llm_fn: Optional[Callable[[], Awaitable[Any]]] = None,
    ):
        # Lazy imports keep this module cheap to import (used at boot time
        # before app modules finish wiring up).
        # The models.dev catalog refreshes with the default pricing update.
        # Tests that inject their own update_fn get no catalog fetch.
        if catalog_fn is None and update_fn is None:
            from plugboard.models_dev import refresh_catalog

            catalog_fn = refresh_catalog
        if llm_fn is None and update_fn is None:
            from plugboard.llm_pricing import scheduled_pass

            llm_fn = scheduled_pass
        if update_fn is None:
            from plugboard.pricing_scraper import update_current_pricing

            update_fn = update_current_pricing
        if providers_fn is None:
            from plugboard.config import load_providers

            providers_fn = load_providers

        self._update_fn = update_fn
        self._catalog_fn = catalog_fn
        self._llm_fn = llm_fn
        self._providers_fn = providers_fn
        self._sleep = sleep_fn or asyncio.sleep
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

        # Tunables (read at construction so tests can override env vars
        # via monkeypatch.setenv before instantiation).
        self.disabled = _parse_bool(os.environ.get("PRICING_SCHEDULER_DISABLED", ""))
        self.daily_hour_utc = max(0, min(23, _env_int("PRICING_DAILY_HOUR_UTC", 3)))
        # C3 (2026-06-01): optional FREQUENT refresh. When > 0, the loop sleeps
        # this many hours between runs instead of waking once/day at daily_hour_utc.
        # Default 0 = legacy daily-at-hour behaviour. Clamped to 1..24 when enabled.
        # Hours between updates (default 6); 0 = daily at daily_hour_utc.
        from plugboard.intervals import pricing_refresh_interval_h

        self.refresh_interval_h = pricing_refresh_interval_h()
        self.update_timeout_s = max(1, _env_int("PRICING_UPDATE_TIMEOUT_S", 120))
        self.cb_cooldown_s = max(1, _env_int("PRICING_CB_COOLDOWN_S", 3600))
        self.providers_ready_max_retries = _env_int(
            "PRICING_PROVIDERS_READY_MAX_RETRIES", 3
        )
        self.providers_ready_backoff_s = max(
            1, _env_int("PRICING_PROVIDERS_READY_BACKOFF_S", 2)
        )

        # ARCH-DEBT-033 closed: CB state is persisted to data/cb_state.json
        # so an OOM crash loop doesn't reset the breaker on every boot and
        # repeatedly slam a known-bad upstream. Loaded here, written in
        # _record_failure / _record_success. Tests inject a tmp path.
        self._cb_state_file = cb_state_file or _CB_STATE_FILE
        self._consecutive_failures, self._cb_open_until = self._load_cb_state()

        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self.running_since: Optional[str] = None
        self.started_at: Optional[str] = None
        self._run_lock = asyncio.Lock()
        self.last_success_at: Optional[str] = None
        self.last_result: Optional[Dict[str, Any]] = None

    def _load_cb_state(self) -> tuple[int, Optional[datetime]]:
        """Restore CB state from disk. Tolerant of missing/corrupt file."""
        try:
            data = json.loads(self._cb_state_file.read_text())
        except FileNotFoundError:
            return 0, None
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "PricingScheduler: cb_state.json unreadable (%s) — starting CLOSED",
                exc,
            )
            return 0, None
        try:
            failures = max(0, int(data.get("consecutive_failures", 0)))
            open_until_iso = data.get("cb_open_until")
            open_until = (
                datetime.fromisoformat(open_until_iso) if open_until_iso else None
            )
            # Discard a stale OPEN window (cooldown already elapsed).
            if open_until is not None and open_until <= self._now():
                open_until = None
            return failures, open_until
        except (TypeError, ValueError) as exc:
            log.warning(
                "PricingScheduler: cb_state.json malformed (%s) — starting CLOSED",
                exc,
            )
            return 0, None

    def _persist_cb_state(self) -> None:
        """Atomic write of CB state. Best-effort: a write failure must not
        crash the scheduler — degrade to in-memory only and log."""
        payload = {
            "consecutive_failures": self._consecutive_failures,
            "cb_open_until": (
                self._cb_open_until.isoformat() if self._cb_open_until else None
            ),
            "saved_at": self._now().isoformat(),
        }
        try:
            self._cb_state_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self._cb_state_file.parent,
                prefix=".cb_state.",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                json.dump(payload, tmp)
                tmp_path = tmp.name
            os.replace(tmp_path, self._cb_state_file)
        except OSError as exc:
            log.warning("PricingScheduler: failed to persist cb_state (%s)", exc)

    # -- public API ----------------------------------------------------------

    async def start(self) -> None:
        """Kick off the scheduler in the current event loop.

        Returns immediately — the actual work happens in a background task.
        Stash the returned reference (or call ``stop()``) to avoid the
        task being garbage-collected (see CPython asyncio docs).
        """
        if self.disabled:
            log.info("PricingScheduler disabled via PRICING_SCHEDULER_DISABLED")
            return
        if self._task is not None and not self._task.done():
            return  # idempotent
        self._stopping.clear()
        self.started_at = self._now().isoformat()
        self._task = asyncio.create_task(self._run(), name="pricing-scheduler")
        self._task.add_done_callback(self._on_task_done)

    async def stop(self) -> None:
        """Signal the loop to exit and wait for the in-flight update."""
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
    def task(self) -> Optional[asyncio.Task]:
        """Exposed for tests + observability."""
        return self._task

    @property
    def status_dict(self) -> Dict[str, Any]:
        """Machine-readable scheduler status (manifesto §29.7 monitoring).

        Surfaced via /admin/pricing/scheduler/status so SREs don't have
        to grep logs to know whether the circuit breaker is open or how
        many failures preceded it.
        """
        if self._cb_open_until is not None:
            cb_state = "OPEN"
        elif self._consecutive_failures > 0:
            cb_state = "DEGRADED"
        else:
            cb_state = "CLOSED"
        return {
            "disabled": self.disabled,
            "running": self._task is not None and not self._task.done(),
            "daily_hour_utc": self.daily_hour_utc,
            "refresh_interval_h": self.refresh_interval_h,  # C3: 0 = daily-at-hour
            "mode": "interval" if self.refresh_interval_h > 0 else "daily",  # C3
            "update_timeout_s": self.update_timeout_s,
            "cb_state": cb_state,
            "cb_consecutive_failures": self._consecutive_failures,
            "cb_open_until": self._cb_open_until.isoformat()
            if self._cb_open_until
            else None,
            "cb_cooldown_s": self.cb_cooldown_s,
            "running_since": self.running_since,
            "last_success_at": self.last_success_at,
            "last_result": self.last_result,
            "stale": self.is_stale(),
        }

    def is_stale(self) -> bool:
        """No successful update for 2 intervals (48 h in daily mode)."""
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
        """Run one update now; raises RuntimeError while another one runs."""
        if self._run_lock.locked():
            raise RuntimeError(self.running_since or "")
        await self._invoke_update(kind)

    # -- internals -----------------------------------------------------------

    def _on_task_done(self, task: asyncio.Task) -> None:
        """Surface unexpected exceptions instead of swallowing them."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("PricingScheduler task crashed: %s", exc, exc_info=exc)

    async def _wait_providers_ready(self) -> bool:
        """Poll for providers being loaded before the bootstrap update.

        Avoids the race condition where update_current_pricing runs before
        bootstrap_ollama / providers.json finish syncing (architect-arbiter
        MUST-FIX #2). Returns True if at least one provider is loaded.
        """
        for attempt in range(self.providers_ready_max_retries):
            # Early-exit on shutdown so stop() doesn't wait the full
            # max_retries * backoff_s window during boot interruption.
            if self._stopping.is_set():
                return True
            try:
                providers = self._providers_fn() or []
            except Exception as exc:  # noqa: BLE001
                log.warning("providers_fn raised on attempt %d: %s", attempt + 1, exc)
                providers = []
            if providers:
                return True
            await self._sleep(self.providers_ready_backoff_s)
        log.warning(
            "PricingScheduler bootstrap proceeding without providers loaded "
            "after %d retries — pricing may be incomplete on first run",
            self.providers_ready_max_retries,
        )
        return False

    async def _run(self) -> None:
        """Top-level loop: bootstrap once, then daily refresh forever."""
        # Step 1: bootstrap as soon as providers are loaded.
        await self._wait_providers_ready()
        await self._invoke_update("bootstrap")

        # Step 2: daily loop. Recompute the deadline every iteration so a
        # mid-loop month rollover doesn't desync.
        while not self._stopping.is_set():
            sleep_for = self._seconds_until_next_run()
            await self._sleep(sleep_for)
            if self._stopping.is_set():
                return
            if self._cb_should_skip():
                log.warning(
                    "PricingScheduler skipping run — circuit breaker OPEN until %s",
                    self._cb_open_until,
                )
                continue
            await self._invoke_update("daily")

    def _seconds_until_next_run(self) -> float:
        """Sleep duration until the next run: a fixed interval when
        PRICING_REFRESH_INTERVAL_H is set (C3), else the next daily_hour_utc:00 UTC."""
        if self.refresh_interval_h > 0:
            return max(1.0, self.refresh_interval_h * 3600 + random.uniform(0, 300))
        now = self._now()
        target = now.replace(
            hour=self.daily_hour_utc, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target = target + timedelta(days=1)
        delta = (target - now).total_seconds()
        # ARCH-DEBT-035 closed: jitter widened from 0-30s to 0-300s (5 min)
        # so a multi-instance rollout cannot thundering-herd the upstream
        # pricing API at exactly 03:00 UTC. ARCH-DEBT-037 (closed by
        # comment): a reboot at 02:59:31 would compute target=tomorrow03:00
        # and miss today's window, but the next iteration's window absorbs
        # the gap (pricing is idempotent and bootstrap already runs once).
        jitter = random.uniform(0, 300)
        return max(1.0, delta + jitter)

    def _cb_should_skip(self) -> bool:
        if self._cb_open_until is None:
            return False
        if self._now() >= self._cb_open_until:
            # HALF_OPEN: let the next call probe.
            self._cb_open_until = None
            log.info("PricingScheduler circuit breaker → HALF_OPEN (probing)")
            return False
        return True

    async def _invoke_update(self, kind: str) -> None:
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
                await self._invoke_update_inner(kind)
            finally:
                self.running_since = None

    async def _invoke_update_inner(self, kind: str) -> None:
        if self._catalog_fn is not None:
            # A catalog failure keeps the last good copy and never counts
            # against the pricing circuit breaker.
            try:
                await asyncio.wait_for(
                    self._catalog_fn(), timeout=self.update_timeout_s
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("PricingScheduler %s catalog refresh failed: %s", kind, exc)
        try:
            result = await asyncio.wait_for(
                self._update_fn(), timeout=self.update_timeout_s
            )
            self._record_success(kind, result)
            if self._llm_fn is not None:
                # Prices read by a model are suggestions only; a failure here
                # never counts against the pricing circuit breaker.
                try:
                    await asyncio.wait_for(
                        self._llm_fn(), timeout=self.update_timeout_s
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "PricingScheduler %s LLM pricing pass failed: %s", kind, exc
                    )
        except asyncio.TimeoutError:
            log.error(
                "PricingScheduler %s update timed out after %ss",
                kind,
                self.update_timeout_s,
            )
            self._record_failure()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("PricingScheduler %s update failed: %s", kind, exc, exc_info=exc)
            self._record_failure()

    def _record_success(self, kind: str, result: Dict[str, Any]) -> None:
        had_failures = self._consecutive_failures or self._cb_open_until is not None
        if had_failures:
            log.info("PricingScheduler circuit breaker → CLOSED (recovery)")
        self._consecutive_failures = 0
        self._cb_open_until = None
        self.last_result = result if isinstance(result, dict) else None
        # A pass that wrote nothing because the shrink guard refused it, or in
        # which no source answered, ran but did not refresh prices: it does
        # not count towards freshness.
        if (
            isinstance(result, dict)
            and not result.get("sentinel_refused")
            and result.get("sources", ["?"])
        ):
            self.last_success_at = self._now().isoformat()
        if had_failures:
            self._persist_cb_state()  # ARCH-DEBT-033
        log.info(
            "PricingScheduler %s OK — added=%s updated=%s inherited=%s total=%s",
            kind,
            result.get("added"),
            result.get("updated"),
            result.get("inherited"),
            result.get("total"),
        )

    def _record_failure(self) -> None:
        # ARCH-DEBT-036 closed: cap to prevent integer overflow on a
        # pathological long failure run. CB opens at 3 anyway.
        if self._consecutive_failures < _FAILURE_COUNTER_CAP:
            self._consecutive_failures += 1
        if self._consecutive_failures >= 3 and self._cb_open_until is None:
            self._cb_open_until = self._now() + timedelta(seconds=self.cb_cooldown_s)
            log.critical(
                "PricingScheduler circuit breaker → OPEN until %s "
                "(after %d consecutive failures)",
                self._cb_open_until,
                self._consecutive_failures,
            )
        self._persist_cb_state()  # ARCH-DEBT-033
