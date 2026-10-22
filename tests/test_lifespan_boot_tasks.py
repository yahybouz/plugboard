"""The lifespan's fire-and-forget boot tasks are kept alive, then cancelled.

The event loop only holds a weak reference to a task, so one that nothing else
references can be garbage-collected while it still runs; and a task nothing
cancels keeps running after the application has shut down. Both are regressions
this suite guards against for the two boot tasks of `_lifespan`.
"""

from __future__ import annotations

import asyncio
import gc

import pytest

from plugboard import app as app_module

BOOT_TASK_NAMES = {"subprocess-warm-up", "internet-boot-probe"}


class _StubScheduler:
    """Stands in for the pricing and model-availability schedulers."""

    disabled = True
    daily_hour_utc = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _boot_tasks() -> dict[str, asyncio.Task]:
    return {
        task.get_name(): task
        for task in asyncio.all_tasks()
        if task.get_name() in BOOT_TASK_NAMES
    }


@pytest.fixture
def quiet_lifespan(monkeypatch):
    """Neutralise everything `_lifespan` starts except the two boot tasks.

    The coroutines the boot tasks run are replaced by ones that never finish,
    so the test observes them while they are still pending.
    """
    monkeypatch.setattr("plugboard.provider_watchdog.start_watchdog", lambda: None)
    monkeypatch.setattr("plugboard.provider_watchdog.stop_watchdog", lambda: None)
    monkeypatch.setattr("plugboard.pricing_scheduler.PricingScheduler", _StubScheduler)
    monkeypatch.setattr(
        "plugboard.model_availability.ModelAvailabilityScheduler", _StubScheduler
    )

    async def _no_ollama():
        return None

    async def _never_warm_up(timeout_s: float = 0.0):
        await asyncio.sleep(3600)
        return {}

    async def _never_probe(_options):
        await asyncio.sleep(3600)
        return {}

    async def _noop():
        return None

    monkeypatch.setattr("plugboard.ollama.bootstrap_ollama", _no_ollama)
    monkeypatch.setattr(
        "plugboard.cli_subprocess.warm_up_all_installed", _never_warm_up
    )
    monkeypatch.setattr("plugboard.app.check_all_models_internet_access", _never_probe)
    monkeypatch.setattr("plugboard.change_events.drain", _noop)
    monkeypatch.setattr("plugboard.http_pool.close_session", _noop)
    monkeypatch.setenv("PLUGBOARD_INTERNET_CHECK_ON_BOOT", "1")


@pytest.mark.asyncio
async def test_boot_tasks_survive_gc_then_are_cancelled(quiet_lifespan):
    async with app_module._lifespan(app_module.app):
        # Let both tasks start, then collect: an unreferenced task would be
        # dropped here and never run to completion.
        await asyncio.sleep(0)
        gc.collect()
        running = _boot_tasks()
        assert set(running) == BOOT_TASK_NAMES
        assert not any(task.done() for task in running.values())

    # Shutdown cancels whatever is still pending.
    await asyncio.sleep(0)
    for name, task in running.items():
        assert task.done(), f"{name} outlived the application"
        assert task.cancelled(), f"{name} was not cancelled"
    assert not _boot_tasks()


@pytest.mark.asyncio
async def test_internet_boot_probe_can_be_disabled(quiet_lifespan, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_INTERNET_CHECK_ON_BOOT", "0")
    async with app_module._lifespan(app_module.app):
        await asyncio.sleep(0)
        assert set(_boot_tasks()) == {"subprocess-warm-up"}
