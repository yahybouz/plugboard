"""The lifespan's fire-and-forget boot tasks are kept alive, then cancelled.

The event loop only holds a weak reference to a task, so one that nothing else
references can be garbage-collected while it still runs; and a task nothing
cancels keeps running after the application has shut down. Both are regressions
this suite guards against for the two boot tasks of `_lifespan`.
"""

from __future__ import annotations

import asyncio
import gc
import importlib

import pytest

from plugboard import app as app_module

BOOT_TASK_NAMES = {"subprocess-warm-up", "internet-boot-probe"}


# `plugboard.app` and `plugboard.tokenizer` are patched on the module objects
# `_lifespan` really uses, not through a dotted path: other tests of the suite
# re-import `plugboard.app` (test_audit_e2e) or swap `plugboard.tokenizer` in
# sys.modules (test_coverage_batch2), and a dotted path then reaches another
# copy than the one `app_module._lifespan` runs with.
def _tokenizer_in_use():
    """The tokenizer `_lifespan` imports (sys.modules), not the package attribute."""
    return importlib.import_module("plugboard.tokenizer")


class _StubScheduler:
    """Stands in for the pricing and model-availability schedulers."""

    disabled = True
    daily_hour_utc = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _FakeJob:
    """Stands in for a maintenance PeriodicJob: counts start() and stop()."""

    disabled = False
    interval_s = 300
    daily_hour_utc = None

    def __init__(self, name: str) -> None:
        self.name = name
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1


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
    monkeypatch.setattr(app_module, "check_all_models_internet_access", _never_probe)
    monkeypatch.setattr("plugboard.change_events.drain", _noop)
    # Loading the tiktoken encoding may download it: never from the suite.
    monkeypatch.setattr(_tokenizer_in_use(), "_get_encoding", lambda: None)
    monkeypatch.setattr("plugboard.http_pool.close_session", _noop)
    monkeypatch.setenv("PLUGBOARD_INTERNET_CHECK_ON_BOOT", "1")

    # No real snapshot or retention pass; the fakes leave app.state afterwards.
    jobs: dict[str, _FakeJob] = {}

    def _factory(name: str):
        def _make() -> _FakeJob:
            jobs[name] = _FakeJob(name)
            return jobs[name]

        return _make

    monkeypatch.setattr(
        "plugboard.maintenance.availability_job", _factory("availability_snapshots")
    )
    monkeypatch.setattr(
        "plugboard.maintenance.retention_job", _factory("data_retention")
    )
    for attr in ("availability_job", "retention_job"):
        monkeypatch.setattr(app_module.app.state, attr, None, raising=False)
    return jobs


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
async def test_lifespan_starts_and_stops_the_maintenance_jobs(quiet_lifespan):
    async with app_module._lifespan(app_module.app):
        assert set(quiet_lifespan) == {"availability_snapshots", "data_retention"}
        for job in quiet_lifespan.values():
            assert (job.starts, job.stops) == (1, 0), job.name
        assert (
            app_module.app.state.availability_job
            is quiet_lifespan["availability_snapshots"]
        )
        assert app_module.app.state.retention_job is quiet_lifespan["data_retention"]

    for job in quiet_lifespan.values():
        assert (job.starts, job.stops) == (1, 1), job.name


@pytest.mark.asyncio
async def test_internet_boot_probe_can_be_disabled(quiet_lifespan, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_INTERNET_CHECK_ON_BOOT", "0")
    async with app_module._lifespan(app_module.app):
        await asyncio.sleep(0)
        assert set(_boot_tasks()) == {"subprocess-warm-up"}


@pytest.mark.asyncio
async def test_internet_boot_probe_skips_local_models(quiet_lifespan, monkeypatch):
    # Probing a local Ollama model loads and runs it on the gateway host, for
    # a model that has no web access anyway: the boot probe leaves them out.
    seen: list[dict] = []
    probed = asyncio.Event()

    async def _record(options):
        seen.append(options)
        probed.set()
        return {}

    real_sleep = asyncio.sleep

    async def _no_delay(_delay, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(app_module, "check_all_models_internet_access", _record)
    # The probe waits 5 s after boot; the test does not.
    monkeypatch.setattr(asyncio, "sleep", _no_delay)
    async with app_module._lifespan(app_module.app):
        await asyncio.wait_for(probed.wait(), timeout=5)

    assert seen == [{"persist": True, "skip_local": True}]


@pytest.mark.asyncio
async def test_boot_warms_the_tiktoken_encoding(quiet_lifespan, monkeypatch):
    # The first token count loads (and may download) the encoding: done at
    # boot, off the event loop, so the first request does not pay for it.
    import threading

    loaded = threading.Event()

    def _load():
        loaded.set()
        return None

    monkeypatch.setattr(_tokenizer_in_use(), "_get_encoding", _load)
    async with app_module._lifespan(app_module.app):
        assert await asyncio.to_thread(loaded.wait, 5)
