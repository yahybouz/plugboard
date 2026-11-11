"""Sidebar "Log out everywhere": every dashboard session ends, or none does.

Each test opens two logged-in browsers, A and B, on the shared E2E server.
A uses the button; B stands for another device signed in to the dashboard.
The confirmation is the in-page dialog (``confirmAction``), not
``window.confirm``, like every other destructive action of the dashboard.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import _open_logged_in

pytestmark = pytest.mark.asyncio(loop_scope="session")

_OK = "Log out everywhere"


async def _status(dash, path="/admin/keys"):
    return await dash.page.evaluate(
        "p => fetch(p, {credentials: 'same-origin'}).then(r => r.status)", path
    )


async def _two_browsers(browser, base_url):
    ctx_a, a = await _open_logged_in(browser, base_url)
    ctx_b, b = await _open_logged_in(browser, base_url)
    return ctx_a, a, ctx_b, b


async def test_button_fits_the_sidebar_footer(dashboard):
    # Three buttons share a 256 px footer: the new one must not spill over the
    # padding nor break its label over two lines.
    page = dashboard.page
    await page.set_viewport_size({"width": 1280, "height": 800})
    edge = await page.evaluate(
        """() => {
          const f = document.querySelector('.sidebar-footer');
          const r = f.getBoundingClientRect();
          return r.right - parseFloat(getComputedStyle(f).paddingRight);
        }"""
    )
    btn = await page.locator("#logout-all-btn").bounding_box()
    logout = await page.locator("#logout-btn").bounding_box()
    assert btn["x"] + btn["width"] <= edge + 0.5, (btn, edge)
    assert abs(btn["height"] - logout["height"]) < 2, (btn, logout)


async def test_log_out_everywhere_signs_out_other_browsers(browser, e2e_server):
    ctx_a, a, ctx_b, b = await _two_browsers(browser, e2e_server)
    try:
        assert await _status(b) == 200
        await a.page.locator("#logout-all-btn").click()
        modal = a.page.locator(".confirm-modal")
        await modal.get_by_role("button", name=_OK).click()
        await a.page.wait_for_url("**/login**", timeout=10_000)
        assert "logged_out=1" in a.page.url
        assert await _status(b) == 401
        assert a.js_errors == [], a.js_errors
    finally:
        await ctx_a.close()
        await ctx_b.close()


async def test_signed_out_browser_is_not_signed_back_in(browser, e2e_server):
    # The E2E server is a local install with auto-login: B is a remembered
    # browser. Reloading must show the login form, not open a new session.
    ctx_a, a, ctx_b, b = await _two_browsers(browser, e2e_server)
    try:
        await a.page.locator("#logout-all-btn").click()
        await a.page.locator(".confirm-modal").get_by_role("button", name=_OK).click()
        await a.page.wait_for_url("**/login**", timeout=10_000)
        await b.page.reload(wait_until="domcontentloaded")
        assert "/login" in b.page.url, b.page.url
        assert await _status(b) == 401
        await a.page.goto(f"{e2e_server}/dashboard", wait_until="domcontentloaded")
        assert "/login" in a.page.url, a.page.url
        assert await _status(a) == 401
    finally:
        await ctx_a.close()
        await ctx_b.close()


async def test_cancel_keeps_every_session(browser, e2e_server):
    ctx_a, a, ctx_b, b = await _two_browsers(browser, e2e_server)
    try:
        await a.page.locator("#logout-all-btn").click()
        modal = a.page.locator(".confirm-modal")
        await modal.get_by_role("button", name="Cancel").click()
        await modal.wait_for(state="detached", timeout=5_000)
        assert await a.page.locator("#app:not(.hidden)").count() == 1
        assert await _status(a) == 200 and await _status(b) == 200
        assert a.js_errors == [], a.js_errors
    finally:
        await ctx_a.close()
        await ctx_b.close()


async def test_failed_revocation_stays_and_says_so(browser, e2e_server):
    ctx_a, a, ctx_b, b = await _two_browsers(browser, e2e_server)
    try:
        # The server's own 503 (session store not writable) is covered by the
        # unit tests; here the page must not pretend the sessions are gone.
        await a.page.route(
            "**/auth/admin/logout-all",
            lambda route: route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail": "Sessions could not be revoked."}',
            ),
        )
        await a.page.locator("#logout-all-btn").click()
        await a.page.locator(".confirm-modal").get_by_role("button", name=_OK).click()
        toast = a.page.locator(".toast.error", has_text="Could not log out everywhere")
        await toast.wait_for(timeout=5_000)
        assert "HTTP 503" in (await toast.text_content() or "")
        assert "/login" not in a.page.url
        assert await _status(a) == 200 and await _status(b) == 200
    finally:
        await ctx_a.close()
        await ctx_b.close()
