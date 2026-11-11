"""The price update and the catalog parse do their file work off the event loop.

Reading, merging and writing a month's grid under its lock, or validating a
20 MB catalog, used to run on the event loop and stall every request for as
long as it took. They now run in a worker thread (``asyncio.to_thread``).
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest
from aioresponses import aioresponses

from plugboard import billing, change_events, models_dev, pricing_scraper, storage

FIXTURE = Path(__file__).parent / "fixtures" / "models_dev_small.json"
URL = "https://catalog.test/api.json"


def _record_threads(monkeypatch, module, name):
    """Wrap module.name so each call records the thread it ran in."""
    real = getattr(module, name)
    threads = []

    def wrapper(*args, **kwargs):
        threads.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return threads


@pytest.mark.asyncio
async def test_price_update_reads_merges_and_saves_off_the_event_loop(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(billing, "_PRICING_DIR", tmp_path)

    async def no_sources(order):
        return {}, {}

    monkeypatch.setattr(pricing_scraper, "_fetch_sources", no_sources)
    monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", "")
    # The update keeps the sources it used for revert_model(): those stay
    # with this test, not with the tests that run after it.
    monkeypatch.setattr(pricing_scraper, "_last_sources", {})
    monkeypatch.setattr(pricing_scraper, "_last_space_sources", {})
    loaded = _record_threads(monkeypatch, pricing_scraper, "load_pricing")
    saved = _record_threads(monkeypatch, pricing_scraper, "save_pricing")
    month = billing.current_pricing_month()
    (tmp_path / f"{month}.json").write_text(
        json.dumps(
            {"month": month, "models": {"m": {"input_price": 1, "output_price": 2}}}
        )
    )

    loop_thread = threading.get_ident()
    result = await pricing_scraper.update_current_pricing()

    assert result["month"] == month
    assert loaded, "the grid was never read"
    assert loop_thread not in loaded + saved


@pytest.mark.asyncio
async def test_price_update_reads_the_stored_catalog_off_the_event_loop(monkeypatch):
    monkeypatch.setattr(pricing_scraper, "_models_dev_pricing", lambda: ({}, {}))
    threads = _record_threads(monkeypatch, pricing_scraper, "_models_dev_pricing")

    loop_thread = threading.get_ident()
    await pricing_scraper._fetch_sources(["models_dev"])

    assert threads and loop_thread not in threads


@pytest.mark.asyncio
async def test_catalog_refresh_parses_off_the_event_loop(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("PLUGBOARD_DATA", str(data))
    monkeypatch.setenv("PLUGBOARD_CATALOG_URL", URL)
    monkeypatch.delenv("PLUGBOARD_CATALOG", raising=False)
    monkeypatch.setitem(models_dev._cache, "mtime", None)
    monkeypatch.setitem(models_dev._cache, "providers", {})
    parsed = _record_threads(monkeypatch, models_dev, "parse_catalog")

    loop_thread = threading.get_ident()
    with aioresponses() as m:
        m.get(URL, status=200, body=FIXTURE.read_text())
        result = await models_dev.refresh_catalog()
    assert result["state"] == "updated"
    # The next read on the event loop reuses the copy parsed by the refresh.
    assert "acme" in models_dev.load_catalog()

    assert parsed and loop_thread not in parsed


# -- A cancellation does not cut the write half-way ------------------------------
#
# The scheduler runs each update under asyncio.wait_for(): its timeout cancels
# the update. While the write ran on the event loop, a timeout could not fall
# in it. A worker thread cannot be stopped: cancelled meanwhile, the update
# waits for the write to end and finishes its follow-up (journal, webhooks,
# catalog meta) before it gives way to the cancellation.


def _hold_in_thread(monkeypatch, module, name):
    """Make module.name wait, in its thread, until the test releases it."""
    real = getattr(module, name)
    started, release = threading.Event(), threading.Event()

    def held(*args, **kwargs):
        started.set()
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, held)
    return started, release


async def _cancel_while_held(coro, started, release):
    """Cancel coro while its file work runs, as the scheduler's timeout does."""
    task = asyncio.ensure_future(coro)
    assert await asyncio.to_thread(started.wait, 5), "the file work never started"
    task.cancel()
    await asyncio.sleep(0)  # the cancellation reaches the update first
    release.set()
    # The cancellation is not lost, and is raised once the follow-up is done.
    with pytest.raises(asyncio.CancelledError):
        await task
    await change_events.drain()  # webhooks are sent in the background


@pytest.mark.asyncio
async def test_price_update_cancelled_during_the_write_still_journals_and_announces(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setattr(billing, "_PRICING_DIR", tmp_path)

    async def no_sources(order):
        return {}, {}

    monkeypatch.setattr(pricing_scraper, "_fetch_sources", no_sources)
    monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", "")
    monkeypatch.setattr(pricing_scraper, "_last_sources", {})
    monkeypatch.setattr(pricing_scraper, "_last_space_sources", {})
    sent = []

    async def fake_emit(event, data):
        sent.append((event, data))
        return 1

    monkeypatch.setattr("plugboard.webhooks.emit", fake_emit)

    def one_change(month, updated, drifts, drift_pct):
        batch = change_events.ChangeBatch()
        batch.add("pricing.changed", {"month": month, "model": "m"})
        return batch

    monkeypatch.setattr(pricing_scraper, "price_change_events", one_change)
    month = billing.current_pricing_month()
    (tmp_path / f"{month}.json").write_text(
        json.dumps(
            {"month": month, "models": {"m": {"input_price": 1, "output_price": 2}}}
        )
    )
    held = _hold_in_thread(monkeypatch, pricing_scraper, "_merge_and_save")

    await _cancel_while_held(pricing_scraper.update_current_pricing(), *held)

    assert [e["event"] for e in change_events.read_journal()] == ["pricing.changed"]
    assert [event for event, _ in sent] == ["pricing.changed"]


@pytest.mark.asyncio
async def test_catalog_refresh_cancelled_while_storing_still_records_its_meta(
    tmp_path, monkeypatch
):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("PLUGBOARD_DATA", str(data))
    monkeypatch.setenv("PLUGBOARD_CATALOG_URL", URL)
    monkeypatch.delenv("PLUGBOARD_CATALOG", raising=False)
    monkeypatch.setitem(models_dev._cache, "mtime", None)
    monkeypatch.setitem(models_dev._cache, "providers", {})
    held = _hold_in_thread(monkeypatch, models_dev, "_store_catalog")

    with aioresponses() as m:
        m.get(URL, status=200, body=FIXTURE.read_text(), headers={"ETag": '"v2"'})
        await _cancel_while_held(models_dev.refresh_catalog(), *held)

    status = models_dev.status()
    assert status["providers"] > 0, "the catalog was not written"
    assert status["fetched_at"], "the catalog was written but not recorded"
    assert models_dev._read_meta()["etag"] == '"v2"'


@pytest.mark.asyncio
async def test_a_cancellation_is_not_lost_when_the_file_work_fails():
    started, release = threading.Event(), threading.Event()

    def failing():
        started.set()
        release.wait(5)
        raise OSError("disk full")

    task = asyncio.ensure_future(storage.to_thread_to_the_end(failing))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    # The caller was cancelled: it stops, whatever the thread then raised.
    with pytest.raises(asyncio.CancelledError):
        await task
