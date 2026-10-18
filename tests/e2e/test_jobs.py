"""E2E — Jobs tab: the in-process jobs render and the watchdog is not runnable.

Locks in:
- the 3 jobs render (Price refresh, Model catalog refresh, Provider watchdog),
- the continuous Provider watchdog's "Run now" button is DISABLED while the
  scheduler jobs' buttons are enabled,
- the Details viewer opens without error.

It deliberately does NOT click "Run now" on a scheduler job (that would start a
real price refresh). The disabled-button + details assertions are hermetic.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_three_jobs_render(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("jobs")
    await page.wait_for_selector("#jobs-list table tbody tr")
    for name in ("Price refresh", "Model catalog refresh", "Provider watchdog"):
        assert (
            await page.locator("#jobs-list table tbody tr", has_text=name).count() == 1
        ), name
    assert dashboard.js_errors == [], dashboard.js_errors


async def test_watchdog_run_now_disabled_but_schedulers_enabled(dashboard):
    """The continuous watchdog cannot be triggered; the schedulers can."""
    page = dashboard.page
    await dashboard.goto_tab("jobs")
    await page.wait_for_selector("#jobs-list table tbody tr")

    watchdog = page.locator("#jobs-list table tbody tr", has_text="Provider watchdog")
    assert await watchdog.get_by_role("button", name="Run now").is_disabled()

    pricing = page.locator("#jobs-list table tbody tr", has_text="Price refresh")
    assert await pricing.get_by_role("button", name="Run now").is_enabled()


async def test_job_details_open(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("jobs")
    await page.wait_for_selector("#jobs-list table tbody tr")
    pricing = page.locator("#jobs-list table tbody tr", has_text="Price refresh")
    await pricing.get_by_role("button", name="Details").click()
    await page.wait_for_selector("#job-log-container .card", timeout=10_000)
    assert dashboard.js_errors == [], dashboard.js_errors
