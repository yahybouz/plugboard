"""E2E — API Keys tab: full create → appears → revoke CRUD flow.

Exercises the real ``app.js`` form logic + ``/admin/keys`` endpoints in a browser,
against the isolated server (no real keys touched).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_create_then_revoke_key(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("keys")

    # Empty state first (fresh isolated data dir → no keys yet).
    await page.wait_for_selector("#keys-table-container")

    # Open the create form and submit a new key.
    await page.click("#btn-create-key")
    await page.wait_for_selector("#create-key-form:not(.hidden)")
    client_id = "e2e-acme-corp"
    await page.fill("#key-client-id", client_id)
    await page.fill("#key-label", "E2E Production")
    await page.fill("#key-limit", "50")
    await page.fill("#key-rpm", "120")
    await page.click("#btn-submit-key")

    # The generated lle_ key is revealed.
    await page.wait_for_selector("#new-key-alert:not(.hidden)", timeout=10_000)
    key_value = (await page.text_content("#new-key-value")) or ""
    assert key_value.startswith("pb_"), key_value

    # The key now appears in the table with an "active" badge.
    row = page.locator("#keys-table-container table tbody tr", has_text=client_id)
    await row.wait_for(timeout=10_000)
    assert await row.locator(".badge-green", has_text="active").count() == 1

    # Revoke it → badge flips to "revoked", Revoke button disappears.
    await row.get_by_role("button", name="Revoke").click()
    # The in-page confirmation dialog (window.confirm is not used).
    await page.locator(".confirm-modal").get_by_role("button", name="Confirm").click()
    await page.wait_for_selector(
        f"#keys-table-container table tbody tr:has-text('{client_id}') .badge-red",
        timeout=10_000,
    )
    revoked_row = page.locator(
        "#keys-table-container table tbody tr", has_text=client_id
    )
    assert await revoked_row.locator(".badge-red", has_text="revoked").count() == 1

    assert dashboard.js_errors == [], dashboard.js_errors
