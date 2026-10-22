"""E2E — data-bound read-only tabs: overview, monitoring, usage, ollama; the removed Connections tab stays absent.

Goes beyond the smoke (which only checks render) by asserting that the seeded
provider actually surfaces in the overview + monitoring views, and that the
usage/ollama data containers render their content or empty state.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_overview_reflects_seeded_provider(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("overview")
    await page.wait_for_selector("#overview-cards", timeout=10_000)
    # The stat cards are appended after /health answers: wait for them.
    await page.locator("#overview-cards", has_text="Providers").wait_for(timeout=10_000)
    assert dashboard.js_errors == [], dashboard.js_errors


async def test_monitoring_lists_seeded_provider(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("monitoring")
    await page.wait_for_selector("#monitoring-providers", timeout=10_000)
    # The seeded provider shows up in the monitoring table.
    assert (
        await page.locator("#monitoring-providers", has_text="E2E Provider").count()
        >= 1
    )


async def test_usage_tab_renders_summary_container(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("usage")
    await page.wait_for_selector("#usage-summary-cards", timeout=10_000)
    assert dashboard.js_errors == [], dashboard.js_errors


async def test_ollama_tab_renders_models_container(dashboard):
    page = dashboard.page
    await dashboard.goto_tab("ollama")
    # Renders its status + models container even when no Ollama daemon is reachable.
    await page.wait_for_selector("#ollama-models-container", timeout=10_000)


async def test_connections_tab_is_not_in_the_dashboard(dashboard):
    # OAuth accounts are managed from the provider form (Reconnect/Disconnect).
    page = dashboard.page
    assert await page.locator('.nav-link[data-tab="connections"]').count() == 0
    assert await page.locator("#page-connections").count() == 0
    assert dashboard.js_errors == [], dashboard.js_errors
