"""E2E — Pricing grid renders the seeded models; Playground is wired & ready.

The Playground test is intentionally hermetic: it asserts the model dropdown is
populated from ``/v1/models`` (the seeded models) and the Send control is ready,
WITHOUT sending a completion (the seeded provider has no real backend; real model
behaviour is covered by ``tests/live_model_tests``).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_seeded_pricing_grid_renders(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("pricing")
    await page.wait_for_selector("#pricing-table-container table", timeout=10_000)
    for model in ("e2e-model-a", "e2e-model-b"):
        assert (
            await page.locator("#pricing-table-container table", has_text=model).count()
            >= 1
        ), model
    assert dashboard.js_errors == [], dashboard.js_errors


async def test_playground_dropdown_populated_and_send_ready(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("playground")
    # The dropdown is filled from /v1/models (seeded e2e-model-a/b).
    await page.wait_for_function(
        "() => document.querySelectorAll('#pg-model option').length > 0",
        timeout=10_000,
    )
    values = await page.eval_on_selector_all(
        "#pg-model option", "els => els.map(o => o.value).filter(Boolean)"
    )
    assert any("e2e-model" in v for v in values), values
    assert await page.locator("#btn-send-chat").is_enabled()
    assert dashboard.js_errors == [], dashboard.js_errors
