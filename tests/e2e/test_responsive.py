"""E2E — the dashboard is usable at a mobile viewport (responsive smoke).

Not a pixel-perfect layout assertion: it verifies that at a phone width the app
still authenticates, renders, and lets you navigate tabs (the sidebar goes
off-canvas via the CSS media query, but content and routing keep working).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

_MOBILE = {"width": 390, "height": 844}  # iPhone 12/13/14 logical size


async def test_dashboard_usable_at_mobile_width(browser, e2e_server):
    context = await browser.new_context(viewport=_MOBILE)
    page = await context.new_page()
    page_errors = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    try:
        await page.goto(e2e_server, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_selector("#app:not(.hidden)", timeout=15_000)
        # Content area renders at phone width.
        assert await page.locator("#page-overview").count() == 1
        # Hash navigation still works at mobile width.
        await page.evaluate("location.hash = 'jobs'")
        await page.wait_for_selector("#page-jobs.active", timeout=10_000)
        assert await page.locator("#jobs-list").count() == 1
        assert page_errors == [], page_errors
    finally:
        await context.close()
