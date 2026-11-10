"""E2E — BUG-13: the cloud catalogue does not offer models while Ollama is offline."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

CATALOG = "**/admin/ollama/cloud-catalog"


def _catalogue(local: bool) -> str:
    return json.dumps(
        {
            "models": [
                {"name": "gpt-oss:20b", "id": "gpt-oss:20b-cloud", "installed": False}
            ],
            "count": 1,
            "local_list_available": local,
            "note": "",
        }
    )


async def _open_catalogue(dashboard, local: bool):
    page = dashboard.page

    async def fulfil(route):
        await route.fulfill(
            status=200, content_type="application/json", body=_catalogue(local)
        )

    await page.route(CATALOG, fulfil)
    try:
        await dashboard.goto_tab("ollama")
        container = page.locator("#ollama-cloud-container")
        await container.locator("text=gpt-oss:20b-cloud").wait_for(timeout=10_000)
        return container
    finally:
        await page.unroute(CATALOG)


async def test_offline_ollama_offers_no_cloud_model(dashboard):
    container = await _open_catalogue(dashboard, local=False)
    assert await container.get_by_role("button", name="Add").count() == 0
    assert await container.locator("text=Ollama offline").count() >= 1
    assert await container.locator("text=available").count() == 0


async def test_online_ollama_offers_the_cloud_model(dashboard):
    container = await _open_catalogue(dashboard, local=True)
    assert await container.get_by_role("button", name="Add").count() == 1
