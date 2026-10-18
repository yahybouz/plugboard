"""Tests for monitoring module — provider health, history, uptime."""

from __future__ import annotations

import pytest

AUTH = {"Authorization": "Bearer testkey"}


# ── Unit tests ───────────────────────────────────────────────────────────────


def test_monitoring_history_empty(tmp_path, monkeypatch):
    """get_monitoring_history returns empty list when no data."""
    import plugboard.monitoring as mon

    monkeypatch.setattr(mon, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(mon, "_DB_PATH", tmp_path / "monitoring.db")

    history = mon.get_monitoring_history()
    assert history == []


def test_uptime_stats_no_data(tmp_path, monkeypatch):
    """get_uptime_stats returns 100% when no snapshots exist."""
    import plugboard.monitoring as mon

    monkeypatch.setattr(mon, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(mon, "_DB_PATH", tmp_path / "monitoring.db")

    stats = mon.get_uptime_stats("test-provider", hours=24)
    assert stats["uptime_pct"] == 100.0
    assert stats["checks"] == 0


# ── API endpoint tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_monitoring_endpoint_requires_admin(client):
    """GET /admin/monitoring without admin key returns 401."""
    resp = await client.get("/admin/monitoring")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_monitoring_history_endpoint_requires_admin(client):
    """GET /admin/monitoring/history without admin key returns 401."""
    resp = await client.get("/admin/monitoring/history")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_monitoring_endpoint_returns_report(client, tmp_path, monkeypatch):
    """GET /admin/monitoring returns a structured report."""
    import plugboard.monitoring as mon

    monkeypatch.setattr(mon, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(mon, "_DB_PATH", tmp_path / "monitoring.db")

    resp = await client.get("/admin/monitoring", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "providers" in body
    assert "summary" in body
    assert "ts" in body


@pytest.mark.asyncio
async def test_monitoring_history_endpoint(client, tmp_path, monkeypatch):
    """GET /admin/monitoring/history returns entries list."""
    import plugboard.monitoring as mon

    monkeypatch.setattr(mon, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(mon, "_DB_PATH", tmp_path / "monitoring.db")

    resp = await client.get("/admin/monitoring/history", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "entries" in body
    assert isinstance(body["entries"], list)
