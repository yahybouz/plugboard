"""E2E — /suivi Kanban on desktop and at phone width (390×844).

Create a ticket, move it with the status select, see "termine" refused
without a proof and accepted after one; no horizontal page scroll on a
phone; no JS error.
"""

from __future__ import annotations

import uuid

import pytest

from tests.e2e.conftest import login_url

pytestmark = pytest.mark.asyncio(loop_scope="session")

_MOBILE = {"width": 390, "height": 844}


async def _wait_message(page, fragment: str) -> None:
    await page.wait_for_function(
        "f => document.getElementById('suivi-msg').textContent.includes(f)",
        arg=fragment,
        timeout=10_000,
    )


async def _column_of(page, ticket_id: str) -> str:
    card = page.locator(f'.suivi-card[data-id="{ticket_id}"]')
    return await card.evaluate("c => c.closest('.suivi-col').dataset.statut")


async def _exercise(page) -> None:
    await page.wait_for_selector('#suivi-board[aria-busy="false"]', timeout=15_000)
    titre = f"E2E {uuid.uuid4().hex[:8]}"
    await page.fill("#new-titre", titre)
    await page.click("#suivi-new button[type=submit]")
    card = page.locator(".suivi-card", has_text=titre)
    await card.wait_for(timeout=10_000)
    tid = await card.get_attribute("data-id")
    assert await _column_of(page, tid) == "a_trier"

    await page.select_option(f'[data-testid="suivi-status-{tid}"]', "en_cours")
    await _wait_message(page, "In progress")
    assert await _column_of(page, tid) == "en_cours"

    await page.select_option(f'[data-testid="suivi-status-{tid}"]', "termine")
    await _wait_message(page, "preuve")
    assert await _column_of(page, tid) == "en_cours"

    card = page.locator(f'.suivi-card[data-id="{tid}"]')
    await card.locator(".suivi-proofs summary").click()
    await page.fill(
        f'[data-testid="suivi-proof-{tid}"]', "ci journal plugboard e2e : vert"
    )
    await card.locator(".suivi-proof-form button").click()
    await _wait_message(page, "Evidence added")

    await page.select_option(f'[data-testid="suivi-status-{tid}"]', "termine")
    await _wait_message(page, "Done")
    assert await _column_of(page, tid) == "termine"


async def test_suivi_desktop(dashboard, e2e_server):
    page = dashboard.page
    await page.goto(f"{e2e_server}/suivi", wait_until="domcontentloaded")
    await _exercise(page)
    assert dashboard.js_errors == [], dashboard.js_errors


async def test_suivi_phone_width(browser, e2e_server):
    context = await browser.new_context(viewport=_MOBILE)
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        # Signed-out visit: auto-login brings the browser back to /suivi.
        await page.goto(login_url(e2e_server, "/suivi"), wait_until="domcontentloaded")
        assert page.url.endswith("/suivi")
        await _exercise(page)
        overflow = await page.evaluate(
            "document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 0, f"horizontal scroll of {overflow}px at 390px"
        assert errors == [], errors
    finally:
        await context.close()


async def test_sidebar_links_to_suivi(dashboard):
    link = dashboard.page.get_by_role("link", name="Tickets", exact=True)
    assert await link.get_attribute("href") == "/suivi"
