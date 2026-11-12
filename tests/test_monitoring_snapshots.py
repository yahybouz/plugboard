"""Availability snapshots: written on a schedule, never by the admin page."""

from __future__ import annotations

import asyncio

import pytest

import plugboard.monitoring as mon

AUTH = {"Authorization": "Bearer testkey"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point monitoring.db at a temporary directory."""
    path = tmp_path / "monitoring.db"
    monkeypatch.setattr(mon, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(mon, "_DB_PATH", path)
    return path


def _use_providers(monkeypatch, providers):
    monkeypatch.setattr(mon, "load_providers", lambda: providers)


def _use_probe(monkeypatch, probe):
    probed: list[str] = []

    async def fake(provider_id):
        probed.append(provider_id)
        return await probe(provider_id)

    monkeypatch.setattr(mon, "check_provider_health", fake)
    return probed


@pytest.mark.asyncio
async def test_record_snapshot_writes_one_row_per_active_provider(db, monkeypatch):
    _use_providers(
        monkeypatch,
        [
            {"id": "up", "is_active": True, "models": ["a", "b"]},
            {"id": "down", "is_active": True},
            {"id": "off", "is_active": False},
            {"id": "openclaw", "is_active": True},
        ],
    )

    async def probe(pid):
        if pid == "down":
            raise RuntimeError("boom")
        return {"provider_id": pid, "ok": True, "latency_ms": 12.5, "error": None}

    probed = _use_probe(monkeypatch, probe)

    result = await mon.record_snapshot()

    assert sorted(probed) == ["down", "up"]
    assert set(result) == {"ts", "providers", "healthy"}
    assert result["providers"] == 2
    assert result["healthy"] == 1
    assert result["ts"].endswith("Z")
    rows = {r["provider_id"]: r for r in mon.get_monitoring_history()}
    assert set(rows) == {"up", "down"}
    assert rows["up"]["is_healthy"] == 1
    assert rows["up"]["latency_ms"] == 12.5
    assert rows["up"]["error"] == ""
    assert rows["up"]["models_up"] == 2
    assert rows["down"]["is_healthy"] == 0
    assert rows["down"]["error"] == "boom"
    assert rows["up"]["ts"] == rows["down"]["ts"] == result["ts"]


@pytest.mark.asyncio
async def test_uptime_over_24h_comes_from_scheduled_snapshots(db, monkeypatch):
    _use_providers(monkeypatch, [{"id": "p1", "is_active": True}])
    outcomes = iter([True, True, True, False])

    async def probe(pid):
        ok = next(outcomes)
        return {"provider_id": pid, "ok": ok, "error": None if ok else "HTTP 503"}

    _use_probe(monkeypatch, probe)

    for _ in range(4):
        await mon.record_snapshot()

    stats = mon.get_uptime_stats("p1", 24)
    assert stats["checks"] == 4
    assert stats["uptime_pct"] == 75.0


@pytest.mark.asyncio
async def test_admin_monitoring_writes_no_snapshot(client, db, monkeypatch):
    _use_providers(monkeypatch, [{"id": "p1", "is_active": True}])

    async def probe(pid):
        return {"provider_id": pid, "ok": True, "latency_ms": 5.0, "error": None}

    probed = _use_probe(monkeypatch, probe)

    for _ in range(2):
        resp = await client.get("/admin/monitoring", headers=AUTH)
        assert resp.status_code == 200

    assert probed == ["p1", "p1"]
    assert mon.get_monitoring_history() == []


@pytest.mark.asyncio
async def test_a_hanging_probe_counts_as_down(db, monkeypatch):
    monkeypatch.setattr(mon, "PROBE_TIMEOUT_S", 0.05)
    _use_providers(
        monkeypatch,
        [{"id": "slow", "is_active": True}, {"id": "fast", "is_active": True}],
    )

    async def probe(pid):
        if pid == "slow":
            await asyncio.sleep(1)
        return {"provider_id": pid, "ok": True, "error": None}

    _use_probe(monkeypatch, probe)

    result = await mon.record_snapshot()

    rows = {r["provider_id"]: r for r in mon.get_monitoring_history()}
    assert rows["slow"]["is_healthy"] == 0
    assert rows["slow"]["error"] == "probe timed out"
    assert rows["fast"]["is_healthy"] == 1
    assert result["providers"] == 2
    assert result["healthy"] == 1


@pytest.mark.asyncio
async def test_no_active_provider_writes_nothing(db, monkeypatch):
    _use_providers(
        monkeypatch,
        [{"id": "off", "is_active": False}, {"id": "openclaw", "is_active": True}],
    )

    async def probe(pid):  # pragma: no cover - must not be called
        raise AssertionError(f"{pid} must not be probed")

    probed = _use_probe(monkeypatch, probe)

    result = await mon.record_snapshot()

    assert probed == []
    assert result["providers"] == 0
    assert result["healthy"] == 0
    assert not db.exists()
