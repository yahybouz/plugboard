"""The price update and the catalog parse do their file work off the event loop.

Reading, merging and writing a month's grid under its lock, or validating a
20 MB catalog, used to run on the event loop and stall every request for as
long as it took. They now run in a worker thread (``asyncio.to_thread``).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from aioresponses import aioresponses

from plugboard import billing, models_dev, pricing_scraper

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
