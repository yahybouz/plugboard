"""E2E — People tab: create a personal key, reveal it once, sign in with it, revoke it."""

from __future__ import annotations

import pytest

from plugboard import admin_mfa

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_create_reveal_once_sign_in_and_revoke(dashboard, browser, e2e_server):
    page = dashboard.page
    await dashboard.goto_tab("people")
    await page.click("#btn-create-person")
    await page.fill("#person-name", "e2e-person-one")
    await page.click("#btn-submit-person")
    await page.wait_for_selector("#new-person-alert:not(.hidden)", timeout=10_000)
    key = (await page.text_content("#new-person-key")) or ""
    secret = (await page.text_content("#new-person-secret")) or ""
    assert key.startswith("pba_") and secret
    assert (await page.text_content("#new-person-uri") or "").startswith(
        "otpauth://totp/"
    )
    await page.click("#btn-person-copied")
    await page.wait_for_selector("#new-person-alert", state="hidden")
    assert (await page.text_content("#new-person-key")) == ""
    row = page.locator(
        "#people-table-container table tbody tr", has_text="e2e-person-one"
    )
    await row.locator(".badge-orange", has_text="pending").wait_for(timeout=10_000)

    context = await browser.new_context()
    login = await context.new_page()
    await login.goto(f"{e2e_server}/login?logged_out=1")
    await login.fill("#k", key)
    await login.click("#b")
    await login.wait_for_selector("#m:visible")
    await login.fill("#m", admin_mfa._generate_code(secret))
    await login.click("#b")
    await login.wait_for_selector("#app:not(.hidden)", timeout=15_000)
    await context.close()

    await page.reload()
    await dashboard.goto_tab("people")
    row = page.locator(
        "#people-table-container table tbody tr", has_text="e2e-person-one"
    )
    await row.locator(".badge-green", has_text="active").wait_for(timeout=10_000)
    await row.get_by_role("button", name="Revoke").click()
    await page.locator(".confirm-modal").get_by_role("button", name="Revoke").click()
    await row.locator(".badge-red", has_text="revoked").wait_for(timeout=10_000)
    assert dashboard.js_errors == [], dashboard.js_errors
