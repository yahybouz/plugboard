"""Smoke E2E: every dashboard tab renders in a real browser with no JS error.

This is the backbone of the dashboard E2E suite — it proves the harness works
(isolated server + auto-login) and that none of the 10 tabs is broken at render
time (a JS error or a missing ``#page-<tab>`` would fail here before any deeper
interaction spec runs).
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import DASHBOARD_TABS

# All E2E tests share the session-scoped event loop (see conftest).
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_autologin_reveals_dashboard(dashboard):
    """Localhost auto-login lands on the dashboard with the sidebar nav present."""
    page = dashboard.page
    assert await page.locator("#app:not(.hidden)").count() == 1
    # The Jobs nav link is one of the 10 tabs — its presence proves the
    # sidebar rendered.
    assert await page.get_by_role("link", name="Jobs", exact=True).count() == 1
    assert dashboard.js_errors == [], dashboard.js_errors


@pytest.mark.parametrize("tab", DASHBOARD_TABS)
async def test_tab_renders(dashboard, tab):
    """Each tab activates its #page-<tab> container without a JS error."""
    await dashboard.goto_tab(tab)
    page = dashboard.page
    assert await page.locator(f"#page-{tab}.active").is_visible()
    # A page heading (h2) should be present in the active page.
    assert await page.locator(f"#page-{tab} h2").count() >= 1
    # No uncaught JS exception and no app-level console.error (network 404s on
    # optional data are tolerated — they are asserted explicitly per-tab).
    assert dashboard.js_errors == [], f"{tab}: {dashboard.js_errors}"
