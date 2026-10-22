"""E2E — Providers tab: the seeded provider renders, and the add-form toggles."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_seeded_provider_row_renders(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("providers")
    await page.wait_for_selector("#providers-table-container table")
    row = page.locator(
        "#providers-table-container table tbody tr", has_text="E2E Provider"
    )
    await row.wait_for(timeout=10_000)
    # Active badge + the two seeded models surface in the row.
    assert await row.locator(".badge-green", has_text="active").count() == 1
    assert await row.get_by_text("2 models").count() == 1
    assert dashboard.js_errors == [], dashboard.js_errors


async def test_edit_provider_toggles_active_state(dashboard):
    """Edit the seeded provider: active → inactive → active (self-restoring).

    The e2e server is session-scoped, so this test restores the provider to its
    original ``active`` state at the end to stay order-independent.
    """
    page = dashboard.page
    await dashboard.goto_tab("providers")
    row_sel = "#providers-table-container table tbody tr"
    row = page.locator(row_sel, has_text="E2E Provider")
    await row.wait_for(timeout=10_000)
    assert await row.locator(".badge-green", has_text="active").count() == 1

    # Edit → uncheck active → save → the row flips to "inactive".
    await row.get_by_role("button", name="Edit").click()
    await page.wait_for_selector("#provider-form-container:not(.hidden)")
    await page.uncheck("#pf-active")
    await page.click("#pf-submit-btn")
    await page.wait_for_selector(
        f"{row_sel}:has-text('E2E Provider') .badge-red", timeout=10_000
    )

    # Restore → check active → save → back to "active".
    row = page.locator(row_sel, has_text="E2E Provider")
    await row.get_by_role("button", name="Edit").click()
    await page.wait_for_selector("#provider-form-container:not(.hidden)")
    await page.check("#pf-active")
    await page.click("#pf-submit-btn")
    await page.wait_for_selector(
        f"{row_sel}:has-text('E2E Provider') .badge-green", timeout=10_000
    )
    assert dashboard.js_errors == [], dashboard.js_errors


async def test_add_direct_provider_form_toggles(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("providers")
    form = page.locator("#direct-provider-form")
    assert not await form.is_visible()
    await page.click("#btn-add-direct-provider")
    # The handler sets display:block before any async preset load, so the form
    # becomes visible immediately (preset fetch is a separate, optional concern).
    await form.wait_for(state="visible", timeout=10_000)
    assert await form.is_visible()


async def test_oauth_preset_offers_disconnect_from_the_provider_form(dashboard):
    """The removed Connections tab's Disconnect now lives in the provider form."""
    page = dashboard.page
    state = {"connected": True, "deleted": []}

    async def connections(route):
        rows = (
            [{"source": "github", "account": "e2e-user"}] if state["connected"] else []
        )
        await route.fulfill(json={"connections": rows})

    async def disconnect(route):
        if route.request.method != "DELETE":
            await route.fallback()
            return
        state["deleted"].append(route.request.url.rsplit("/", 1)[-1])
        state["connected"] = False
        await route.fulfill(status=204, body="")

    await page.route("**/admin/oauth/connections", connections)
    await page.route("**/admin/oauth/github", disconnect)
    await dashboard.goto_tab("providers")
    await page.click("#btn-add-direct-provider")
    await page.locator('#dp-preset option[value="github-broker"]').wait_for(
        state="attached", timeout=10_000
    )
    await page.select_option("#dp-preset", "github-broker")
    status = page.locator("#dp-status")
    await status.get_by_text("Already connected as").wait_for(timeout=10_000)
    assert await page.locator("#dp-reconnect-btn").is_visible()
    await page.click("#dp-disconnect-btn")
    await page.locator(".confirm-modal .btn-danger").click()
    await status.get_by_text("Not connected yet").wait_for(timeout=10_000)
    assert state["deleted"] == ["github"]
    assert dashboard.js_errors == [], dashboard.js_errors
