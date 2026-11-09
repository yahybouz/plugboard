"""Phone layout: every tab can open the navigation.

The sidebar is off-screen below 768 px and the menu button used to sit in the
Overview header only: on any other tab a phone user had no way to navigate.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import DASHBOARD_TABS

pytestmark = pytest.mark.asyncio(loop_scope="session")

PHONE = {"width": 390, "height": 844}


@pytest.mark.parametrize("tab", DASHBOARD_TABS)
async def test_menu_button_on_every_tab(dashboard, tab):
    page = dashboard.page
    await page.set_viewport_size(PHONE)
    await dashboard.goto_tab(tab)
    btn = page.locator("#mobile-menu-btn")
    assert await btn.is_visible(), f"{tab}: no menu button on a phone"
    box = await btn.bounding_box()
    assert box["width"] >= 44 and box["height"] >= 44, box
    assert await btn.get_attribute("aria-label")


async def test_menu_opens_navigates_and_closes(dashboard):
    page = dashboard.page
    await page.set_viewport_size(PHONE)
    await dashboard.goto_tab("providers")
    btn = page.locator("#mobile-menu-btn")
    assert await btn.get_attribute("aria-expanded") == "false"
    await btn.click()
    await page.wait_for_selector("#sidebar.open")
    assert await btn.get_attribute("aria-expanded") == "true"
    await page.locator('#sidebar [data-tab="jobs"]').click()
    await page.wait_for_selector("#page-jobs.active")
    await page.wait_for_selector("#sidebar:not(.open)")
    assert await btn.get_attribute("aria-expanded") == "false"
    # Still reachable from the new tab.
    assert await btn.is_visible()
    assert dashboard.js_errors == [], dashboard.js_errors


async def test_menu_bar_hidden_on_desktop(dashboard):
    page = dashboard.page
    await page.set_viewport_size({"width": 1280, "height": 800})
    await dashboard.goto_tab("overview")
    assert not await page.locator("#mobile-menu-btn").is_visible()
