"""``{month}`` admin paths accept YYYY-MM as well as the stored YYYY_MM."""

from __future__ import annotations

import pytest

ADMIN = {"Authorization": "Bearer testkey"}


@pytest.mark.asyncio
async def test_usage_month_with_dash_is_the_same_month(client, monkeypatch):
    import plugboard.usage as usage

    seen = []

    def fake_paginated(month, page=1, limit=100):
        seen.append(month)
        return [], 0

    monkeypatch.setattr(usage, "get_usage_paginated", fake_paginated)
    monkeypatch.setattr(usage, "summarize_usage_month", lambda m: {"total_entries": 0})
    for m in ("2026-11", "2026_11"):
        resp = await client.get(f"/admin/usage/{m}", headers=ADMIN)
        assert resp.status_code == 200, resp.text
        assert resp.json()["month"] == "2026_11"
    assert seen == ["2026_11", "2026_11"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["2026", "2026-13", "26-11", "2026_1", "..%2Fx"])
async def test_bad_month_is_422(client, bad):
    resp = await client.get(f"/admin/usage/{bad}", headers=ADMIN)
    assert resp.status_code in (404, 422), resp.text
    if resp.status_code == 422:
        assert "YYYY" in resp.text


@pytest.mark.asyncio
async def test_pricing_month_with_dash(client, monkeypatch):
    import plugboard.billing as billing

    asked = []
    real = billing.load_pricing

    def spy(month, *a, **kw):
        asked.append(month)
        return real(month, *a, **kw)

    monkeypatch.setattr(billing, "load_pricing", spy)
    await client.get("/admin/pricing/2026-11", headers=ADMIN)
    assert asked and asked[0] == "2026_11"
