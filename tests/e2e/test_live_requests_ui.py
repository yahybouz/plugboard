"""E2E — the dashboard shows that a slow call is running (PLU-13).

Hermetic: the completion is answered by the browser after a delay, so the test
sees the Playground status line while the call is "running", then its result.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.e2e.test_playground_chat import _completion, _open_playground

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_playground_shows_running_then_result(dashboard):
    page = dashboard.page
    model = (await _open_playground(dashboard, stream=False))[0]
    seen_surface = []

    async def slow_answer(route):
        seen_surface.append(route.request.headers.get("x-plugboard-surface"))
        await asyncio.sleep(2.5)
        await route.fulfill(
            status=200, json=_completion(route.request.post_data_json["model"])
        )

    await page.route("**/v1/chat/completions", slow_answer)
    await page.select_option("#pg-model", model)
    await page.fill("#pg-input", "slow one")
    await page.click("#btn-send-chat")

    status = page.locator("#pg-status")
    # While the call runs: a ticking "Running <model> - N s", not a frozen line.
    await page.wait_for_function(
        "() => /^Running .* - [1-9][0-9]* s/.test(document.getElementById('pg-status').textContent)",
        timeout=8_000,
    )
    assert await status.get_attribute("data-state") == "sending"
    # The corner pill counts calls in flight and opens the list.
    pill = page.locator("[data-testid='live-pill']")
    assert await pill.count() == 1
    await pill.click()
    assert await page.locator("[data-testid='live-panel']").is_visible()

    # Then the outcome replaces the ticker.
    await page.wait_for_function(
        "() => document.getElementById('pg-status').dataset.state === 'done'",
        timeout=10_000,
    )
    assert "Running" not in await status.text_content()
    assert seen_surface == ["playground"]
    assert dashboard.js_errors == [], dashboard.js_errors
