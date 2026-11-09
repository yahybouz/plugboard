"""Monitoring › Recent automatic changes renders every event type.

``price.estimated`` events (a model billed on an estimate) carry the price
at the top level and no old/new/pct/status: they were shown as
"— → — (undefined%, undefined)".
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

STATUS = {
    "pricing": {},
    "models": {},
    "catalog": {},
    "pending_suggestions": 2,
    "recent_changes": [
        {
            "at": "2026-11-09T19:42:52Z",
            "event": "price.estimated",
            "from": "azure_ai/claude-opus-5-5",
            "input_price": 4.0,
            "model": "claude-opus-5-5-low",
            "output_price": 20.0,
            "space": "",
        },
        {
            "at": "2026-11-09T20:09:05Z",
            "event": "pricing.changed",
            "model": "tencent/hy3",
            "new": {"input_price": 0.0825, "output_price": 0.33},
            "old": {"input_price": 0.132, "output_price": 0.528},
            "pct": 37.5,
            "provider": "openrouter",
            "status": "pending",
        },
        {
            "at": "2026-11-09T20:10:00Z",
            "event": "models.changed",
            "provider": "antigravity-via-agy-cli",
            "added": ["gemini-4-argon-low"],
            "removed": [],
        },
    ],
}


async def test_recent_changes_have_no_undefined(dashboard):
    page = dashboard.page

    async def fake(route):
        await route.fulfill(
            status=200, content_type="application/json", body=json.dumps(STATUS)
        )

    await page.route("**/admin/auto-update/status?*", fake)
    await dashboard.goto_tab("monitoring")
    changes = page.locator("#monitoring-changes")
    await changes.locator("tr").nth(3).wait_for()
    text = await changes.inner_text()
    assert "undefined" not in text, text
    assert "claude-opus-5-5-low: estimated $4.0000 / $20.0000" in text
    assert "from azure_ai/claude-opus-5-5" in text
    assert "tencent/hy3: $0.1320 / $0.5280 → $0.0825 / $0.3300 (37.5%, pending)" in text
    assert "+1 gemini-4-argon-low" in text
    card = await page.locator("#monitoring-auto-update").inner_text()
    assert "Pricing › Review" in card
