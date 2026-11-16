"""E2E — the dashboard is usable at a mobile viewport (responsive smoke).

Not a pixel-perfect layout assertion: it verifies that at a phone width the app
still authenticates, renders, and lets you navigate tabs (the sidebar goes
off-canvas via the CSS media query, but content and routing keep working).
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import login_url

pytestmark = pytest.mark.asyncio(loop_scope="session")

_MOBILE = {"width": 390, "height": 844}  # iPhone 12/13/14 logical size


async def test_dashboard_usable_at_mobile_width(browser, e2e_server):
    context = await browser.new_context(viewport=_MOBILE)
    page = await context.new_page()
    page_errors = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    try:
        await page.goto(
            login_url(e2e_server), wait_until="domcontentloaded", timeout=20_000
        )
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


async def test_tables_read_as_cards_at_mobile_width(browser, e2e_server):
    """BUG-125: at 390 px a table row is a card that fits the screen.

    Every cell carries its column title, no cell or button runs off the right
    edge, the action buttons and the page's main button are 44 px targets.
    """
    context = await browser.new_context(viewport=_MOBILE)
    page = await context.new_page()
    try:
        await page.goto(
            login_url(e2e_server), wait_until="domcontentloaded", timeout=20_000
        )
        await page.wait_for_selector("#app:not(.hidden)", timeout=15_000)
        await page.evaluate("location.hash = 'providers'")
        await page.wait_for_selector(
            "#providers-table-container table.table-cards tbody tr", timeout=10_000
        )
        report = await page.evaluate(
            """() => {
              const t = document.querySelector('#providers-table-container table');
              const cells = Array.from(t.querySelectorAll('tbody td'));
              const buttons = Array.from(t.querySelectorAll('tbody button'));
              return {
                pageWidth: document.documentElement.scrollWidth,
                unlabelled: cells.filter(td => !td.hasAttribute('data-label')).length,
                offscreen: cells.concat(buttons)
                  .filter(e => e.getBoundingClientRect().right > innerWidth + 1).length,
                smallButtons: buttons
                  .filter(b => b.getBoundingClientRect().height < 44).length,
                roles: [t.getAttribute('role'), cells[0].getAttribute('role')],
              };
            }"""
        )
        assert report["pageWidth"] <= _MOBILE["width"], report
        assert report["unlabelled"] == 0, report
        assert report["offscreen"] == 0, report
        assert report["smallButtons"] == 0, report
        assert report["roles"] == ["table", "cell"], report
        await page.evaluate("location.hash = 'keys'")
        await page.wait_for_selector("#page-keys.active", timeout=10_000)
        box = await page.locator("#btn-create-key").bounding_box()
        assert box and box["height"] >= 44, box
    finally:
        await context.close()
