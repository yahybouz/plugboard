"""
Comprehensive tests for provider_watchdog.py and OpenClaw launcher integration.

Covers:
- ProviderState lifecycle (healthy, failure, restart tracking)
- SQLite event logging (write + read)
- Restart stats aggregation
- Health check ping (mocked aiohttp)
- Restart provider (mocked subprocess)
- Watchdog loop logic (mocked providers)
- Watchdog start/stop lifecycle
- API endpoints (/admin/watchdog/*)
- Edge cases: max restarts per hour, cooldown, stale PID
- Launcher script existence detection
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from unittest.mock import MagicMock

import pytest


# ── ProviderState unit tests ────────────────────────────────────────────────


class TestProviderState:
    """Test the in-memory provider state tracking."""

    def test_initial_state(self):
        from plugboard.provider_watchdog import _ProviderState

        s = _ProviderState("test-provider")
        assert s.provider_id == "test-provider"
        assert s.consecutive_failures == 0
        assert s.is_healthy is False
        assert s.total_restarts == 0

    def test_record_healthy_resets_failures(self):
        from plugboard.provider_watchdog import _ProviderState

        s = _ProviderState("p1")
        s.record_failure()
        s.record_failure()
        assert s.consecutive_failures == 2
        assert s.is_healthy is False
        s.record_healthy()
        assert s.consecutive_failures == 0
        assert s.is_healthy is True
        assert s.last_healthy_ts is not None

    def test_record_failure_increments(self):
        from plugboard.provider_watchdog import _ProviderState

        s = _ProviderState("p1")
        for i in range(5):
            s.record_failure()
        assert s.consecutive_failures == 5
        assert s.is_healthy is False

    def test_can_restart_initially_true(self):
        from plugboard.provider_watchdog import _ProviderState

        s = _ProviderState("p1")
        assert s.can_restart() is True

    def test_can_restart_false_after_max(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_MAX_RESTARTS", 2)
        s = wd._ProviderState("p1")
        s.record_restart()
        s.record_restart()
        assert s.can_restart() is False

    def test_can_restart_false_during_cooldown(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_RESTART_COOLDOWN", 9999)
        s = wd._ProviderState("p1")
        s.record_restart()
        assert s.can_restart() is False

    def test_can_restart_resets_hourly(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_MAX_RESTARTS", 1)
        monkeypatch.setattr(wd, "_RESTART_COOLDOWN", 0)
        s = wd._ProviderState("p1")
        s.record_restart()
        assert s.can_restart() is False
        # Simulate hour passed
        s.restart_hour_start = time.monotonic() - 3601
        assert s.can_restart() is True

    def test_to_dict(self):
        from plugboard.provider_watchdog import _ProviderState

        s = _ProviderState("p1")
        s.record_healthy()
        s.record_restart()
        d = s.to_dict()
        assert d["provider_id"] == "p1"
        assert d["is_healthy"] is True
        assert d["total_restarts"] == 1
        assert d["restarts_this_hour"] == 1
        assert d["last_check_ago_s"] is not None
        assert d["last_healthy_ago_s"] is not None
        assert d["last_restart_ago_s"] is not None


# ── SQLite event logging ────────────────────────────────────────────────────


class TestEventLogging:
    """Test SQLite watchdog_events persistence."""

    def test_log_event_creates_db(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        wd._log_event("p1", "health_check", success=True, latency_ms=42.5)

        conn = sqlite3.connect(str(tmp_path / "watchdog.db"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM watchdog_events").fetchall()
        conn.close()
        assert len(rows) == 1
        r = dict(rows[0])
        assert r["provider_id"] == "p1"
        assert r["event_type"] == "health_check"
        assert r["success"] == 1
        assert r["latency_ms"] == 42.5

    def test_log_event_failure(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        wd._log_event("p1", "restart", success=False, details="timeout after 60s")

        events = wd.get_watchdog_events(provider_id="p1")
        assert len(events) == 1
        assert events[0]["success"] == 0
        assert "timeout" in events[0]["details"]

    def test_get_watchdog_events_filters(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        wd._log_event("p1", "health_check", success=True)
        wd._log_event("p1", "restart", success=True)
        wd._log_event("p2", "health_check", success=False)

        # Filter by provider
        p1_events = wd.get_watchdog_events(provider_id="p1")
        assert len(p1_events) == 2

        # Filter by event type
        restarts = wd.get_watchdog_events(event_type="restart")
        assert len(restarts) == 1
        assert restarts[0]["provider_id"] == "p1"

        # Both filters
        p2_health = wd.get_watchdog_events(provider_id="p2", event_type="health_check")
        assert len(p2_health) == 1

    def test_get_watchdog_events_limit(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        for i in range(10):
            wd._log_event("p1", "health_check", success=True, latency_ms=float(i))

        events = wd.get_watchdog_events(limit=3)
        assert len(events) == 3


# ── Restart stats ───────────────────────────────────────────────────────────


class TestRestartStats:
    """Test restart statistics aggregation."""

    def test_restart_stats_empty(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        stats = wd.get_restart_stats(hours=24)
        assert stats["period_hours"] == 24
        assert stats["providers"] == {}

    def test_restart_stats_with_data(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        wd._log_event("p1", "restart", success=True)
        wd._log_event("p1", "restart", success=True)
        wd._log_event("p1", "restart", success=False, details="timeout")
        wd._log_event("p2", "restart", success=True)

        stats = wd.get_restart_stats(hours=24)
        assert stats["providers"]["p1"]["total_restarts"] == 3
        assert stats["providers"]["p1"]["successful"] == 2
        assert stats["providers"]["p1"]["failed"] == 1
        assert stats["providers"]["p2"]["total_restarts"] == 1


# ── Health check (ping_provider) ────────────────────────────────────────────


class TestPingProvider:
    """Test provider health ping with mocked HTTP (shallow + deep)."""

    @pytest.mark.asyncio
    async def test_ping_provider_not_found(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "get_provider", lambda pid: None)
        result = await wd.ping_provider("ghost")
        assert result["ok"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_ping_shallow_healthy(self, monkeypatch):
        from aioresponses import aioresponses
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(
            wd,
            "get_provider",
            lambda pid: {"id": pid, "base_url": "http://fake-provider.test"},
        )
        # Force shallow only (counter not on deep interval)
        wd._check_counter["test-provider"] = 1
        monkeypatch.setattr(wd, "_DEEP_CHECK_INTERVAL", 5)

        with aioresponses() as mock:
            mock.get(
                "http://fake-provider.test/health", status=200, payload={"ok": True}
            )
            result = await wd.ping_provider("test-provider")

        assert result["ok"] is True
        assert result["check"] == "shallow"
        assert result["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_ping_shallow_unhealthy_500(self, monkeypatch):
        from aioresponses import aioresponses
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(
            wd,
            "get_provider",
            lambda pid: {"id": pid, "base_url": "http://fake-provider.test"},
        )

        with aioresponses() as mock:
            mock.get("http://fake-provider.test/health", status=500)
            result = await wd.ping_provider("test-provider")

        assert result["ok"] is False
        assert result["check"] == "shallow"

    @pytest.mark.asyncio
    async def test_ping_connection_error(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(
            wd,
            "get_provider",
            lambda pid: {"id": pid, "base_url": "http://unreachable.test"},
        )
        monkeypatch.setattr(wd, "_HEALTH_TIMEOUT", 1)
        result = await wd.ping_provider("dead-provider")
        assert result["ok"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_ping_deep_healthy(self, monkeypatch):
        """Deep check: shallow OK + completion OK → healthy."""
        from aioresponses import aioresponses
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(
            wd,
            "get_provider",
            lambda pid: {
                "id": pid,
                "base_url": "http://fake-provider.test",
                "bearer_token": "test-token",
            },
        )

        with aioresponses() as mock:
            mock.get(
                "http://fake-provider.test/health", status=200, payload={"ok": True}
            )
            mock.post(
                "http://fake-provider.test/v1/chat/completions",
                status=200,
                payload={
                    "choices": [{"message": {"content": "pong"}}],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
            result = await wd.ping_provider("test-provider", force_deep=True)

        assert result["ok"] is True
        assert result["check"] == "deep"

    @pytest.mark.asyncio
    async def test_ping_deep_fail_shallow_ok(self, monkeypatch):
        """Deep check: shallow OK but completion fails → unhealthy."""
        from aioresponses import aioresponses
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(
            wd,
            "get_provider",
            lambda pid: {
                "id": pid,
                "base_url": "http://fake-provider.test",
                "bearer_token": "test-token",
            },
        )

        with aioresponses() as mock:
            mock.get(
                "http://fake-provider.test/health", status=200, payload={"ok": True}
            )
            mock.post(
                "http://fake-provider.test/v1/chat/completions",
                status=502,
                body="Bad Gateway",
            )
            result = await wd.ping_provider("test-provider", force_deep=True)

        assert result["ok"] is False
        assert result["check"] == "deep"
        assert result.get("shallow_ok") is True

    @pytest.mark.asyncio
    async def test_ping_deep_timeout(self, monkeypatch):
        """Deep check: completion times out."""
        from aioresponses import aioresponses
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(
            wd,
            "get_provider",
            lambda pid: {
                "id": pid,
                "base_url": "http://fake-provider.test",
                "bearer_token": "test-token",
            },
        )
        monkeypatch.setattr(wd, "_DEEP_CHECK_TIMEOUT", 0.1)

        with aioresponses() as mock:
            mock.get(
                "http://fake-provider.test/health", status=200, payload={"ok": True}
            )
            mock.post(
                "http://fake-provider.test/v1/chat/completions",
                exception=asyncio.TimeoutError(),
            )
            result = await wd.ping_provider("test-provider", force_deep=True)

        assert result["ok"] is False
        assert "timeout" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_ping_deep_interval_counter(self, monkeypatch):
        """Deep check only runs every N checks."""
        from aioresponses import aioresponses
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(
            wd,
            "get_provider",
            lambda pid: {
                "id": pid,
                "base_url": "http://fake-provider.test",
                "bearer_token": "test-token",
            },
        )
        monkeypatch.setattr(wd, "_DEEP_CHECK_INTERVAL", 3)
        wd._check_counter["interval-test"] = 0

        checks = []
        for _ in range(6):
            with aioresponses() as mock:
                mock.get(
                    "http://fake-provider.test/health", status=200, payload={"ok": True}
                )
                mock.post(
                    "http://fake-provider.test/v1/chat/completions",
                    status=200,
                    payload={
                        "choices": [{"message": {"content": "ok"}}],
                    },
                )
                result = await wd.ping_provider("interval-test")
                checks.append(result["check"])

        # Every 3rd check should be deep
        assert checks == ["shallow", "shallow", "deep", "shallow", "shallow", "deep"]


class TestDeepCheckState:
    """Test deep check tracking in ProviderState."""

    def test_record_deep_check(self):
        from plugboard.provider_watchdog import _ProviderState

        s = _ProviderState("p1")
        assert s.last_deep_ok is None
        s.record_deep_check(True)
        assert s.last_deep_ok is True
        assert s.last_deep_check_ts is not None
        s.record_deep_check(False)
        assert s.last_deep_ok is False

    def test_to_dict_includes_deep(self):
        from plugboard.provider_watchdog import _ProviderState

        s = _ProviderState("p1")
        s.record_healthy()
        s.record_deep_check(True)
        d = s.to_dict()
        assert "last_deep_check_ago_s" in d
        assert d["last_deep_ok"] is True


# ── Restart provider (subprocess) ───────────────────────────────────────────


class TestRestartProvider:
    """Native lifecycle is external; supersedes launcher execution oracles."""

    def test_restart_unknown_provider(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "get_provider", lambda _: None)
        result = wd.restart_provider("unknown")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.parametrize(
        "provider",
        [
            {"id": "direct-http", "base_url": "http://direct.test"},
            {"id": "claude-code", "auth_source": "subprocess:claude"},
            {"id": "ollama", "provider_type": "sovereign"},
        ],
    )
    def test_restart_native_lifecycle_not_managed(self, provider, monkeypatch):
        import plugboard.provider_watchdog as wd
        import subprocess

        monkeypatch.setattr(wd, "get_provider", lambda _: provider)

        def reject_process(*args, **kwargs):
            pytest.fail("restart must not execute a host command")

        monkeypatch.setattr(subprocess, "run", reject_process)
        result = wd.restart_provider(provider["id"])
        assert result["success"] is False
        assert "not supported" in result["error"].lower()

    def test_restart_logs_failed_event(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "get_provider", lambda _: {"id": "p1"})
        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        wd.restart_provider("p1")
        events = wd.get_watchdog_events(event_type="restart")
        assert len(events) == 1
        assert events[0]["provider_id"] == "p1"
        assert events[0]["success"] == 0
        assert "not supported" in events[0]["details"].lower()


# ── Watchdog status ─────────────────────────────────────────────────────────


class TestWatchdogStatus:
    """Test get_watchdog_status()."""

    def test_status_when_stopped(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_running", False)
        monkeypatch.setattr(wd, "_watchdog_task", None)
        monkeypatch.setattr(wd, "_states", {})

        status = wd.get_watchdog_status()
        assert status["running"] is False
        assert status["check_interval_s"] > 0
        assert status["failure_threshold"] > 0
        assert status["providers"] == {}

    def test_status_with_providers(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_running", True)
        mock_task = MagicMock()
        mock_task.done.return_value = False
        monkeypatch.setattr(wd, "_watchdog_task", mock_task)
        monkeypatch.setattr(wd, "_states", {})

        state = wd._get_state("p1")
        state.record_healthy()

        status = wd.get_watchdog_status()
        assert status["running"] is True
        assert "p1" in status["providers"]
        assert status["providers"]["p1"]["is_healthy"] is True


# ── Watchdog start/stop lifecycle ───────────────────────────────────────────


class TestWatchdogLifecycle:
    """Test start_watchdog() and stop_watchdog()."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_watchdog_task", None)
        monkeypatch.setattr(wd, "_running", False)
        monkeypatch.setattr(wd, "_states", {})

        # Mock the loop so it doesn't actually run
        async def fake_loop():
            while wd._running:
                await asyncio.sleep(0.01)

        monkeypatch.setattr(wd, "_watchdog_loop", fake_loop)
        task = wd.start_watchdog()
        assert task is not None
        assert wd._running is True

        # Cleanup
        wd.stop_watchdog()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_watchdog_task", None)
        monkeypatch.setattr(wd, "_running", False)
        monkeypatch.setattr(wd, "_states", {})

        async def fake_loop():
            while wd._running:
                await asyncio.sleep(0.01)

        monkeypatch.setattr(wd, "_watchdog_loop", fake_loop)
        wd.start_watchdog()
        assert wd._running is True

        wd.stop_watchdog()
        assert wd._running is False
        assert wd._watchdog_task is None

    @pytest.mark.asyncio
    async def test_start_idempotent(self, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_watchdog_task", None)
        monkeypatch.setattr(wd, "_running", False)
        monkeypatch.setattr(wd, "_states", {})

        async def fake_loop():
            while wd._running:
                await asyncio.sleep(0.01)

        monkeypatch.setattr(wd, "_watchdog_loop", fake_loop)
        t1 = wd.start_watchdog()
        t2 = wd.start_watchdog()
        assert t1 is t2  # Same task returned

        wd.stop_watchdog()
        await asyncio.sleep(0.05)


# ── Watchdog loop integration ───────────────────────────────────────────────


class TestWatchdogLoop:
    """Test the core watchdog loop behavior with mocked providers."""

    @pytest.mark.asyncio
    async def test_loop_detects_unhealthy_and_restarts(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")
        monkeypatch.setattr(wd, "_CHECK_INTERVAL", 0)  # no delay
        monkeypatch.setattr(wd, "_FAILURE_THRESHOLD", 2)
        monkeypatch.setattr(wd, "_RESTART_COOLDOWN", 0)
        monkeypatch.setattr(wd, "_states", {})

        # Provider always unhealthy
        monkeypatch.setattr(
            wd,
            "load_providers",
            lambda: [
                {"id": "broken-p", "is_active": True, "base_url": "http://x.test"}
            ],
        )

        ping_count = 0

        async def fake_ping(pid):
            nonlocal ping_count
            ping_count += 1
            return {"ok": False, "error": "connection refused", "latency_ms": 0}

        monkeypatch.setattr(wd, "ping_provider", fake_ping)

        restart_called = []
        monkeypatch.setattr(
            wd,
            "restart_provider",
            lambda pid: restart_called.append(pid) or {"success": True},
        )

        # Run loop for a few iterations
        wd._running = True
        loop_count = 0
        original_sleep = asyncio.sleep

        async def counted_sleep(seconds):
            nonlocal loop_count
            loop_count += 1
            if loop_count >= 3:
                wd._running = False
            await original_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", counted_sleep)
        await wd._watchdog_loop()

        assert ping_count >= 2
        assert "broken-p" in restart_called

    @pytest.mark.asyncio
    async def test_loop_skips_inactive_providers(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")
        monkeypatch.setattr(wd, "_CHECK_INTERVAL", 0)
        monkeypatch.setattr(wd, "_states", {})

        monkeypatch.setattr(
            wd,
            "load_providers",
            lambda: [
                {"id": "inactive-p", "is_active": False, "base_url": "http://x.test"}
            ],
        )

        pinged = []

        async def fake_ping(pid):
            pinged.append(pid)
            return {"ok": True, "latency_ms": 5}

        monkeypatch.setattr(wd, "ping_provider", fake_ping)

        wd._running = True

        async def stop_after_one(_):
            wd._running = False

        monkeypatch.setattr(asyncio, "sleep", stop_after_one)
        await wd._watchdog_loop()

        assert pinged == []  # inactive provider was skipped

    @pytest.mark.asyncio
    async def test_loop_healthy_provider_no_restart(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")
        monkeypatch.setattr(wd, "_CHECK_INTERVAL", 0)
        monkeypatch.setattr(wd, "_FAILURE_THRESHOLD", 3)
        monkeypatch.setattr(wd, "_states", {})

        monkeypatch.setattr(
            wd,
            "load_providers",
            lambda: [
                {"id": "healthy-p", "is_active": True, "base_url": "http://x.test"}
            ],
        )

        async def fake_ping(pid):
            return {"ok": True, "latency_ms": 10}

        monkeypatch.setattr(wd, "ping_provider", fake_ping)

        restart_called = []
        monkeypatch.setattr(
            wd, "restart_provider", lambda pid: restart_called.append(pid)
        )

        wd._running = True
        count = 0

        async def stop_after_two(_):
            nonlocal count
            count += 1
            if count >= 2:
                wd._running = False

        monkeypatch.setattr(asyncio, "sleep", stop_after_two)
        await wd._watchdog_loop()

        assert restart_called == []


# ── Launcher script detection ───────────────────────────────────────────────


class TestLauncherDetection:
    """Reintroduced retired launchers remain inert in both historical paths."""

    @pytest.mark.parametrize("subdir", ["", "scripts"])
    def test_reintroduced_launcher_is_never_executed(
        self, subdir, tmp_path, monkeypatch
    ):
        import plugboard.provider_watchdog as wd

        folder = tmp_path / subdir
        folder.mkdir(exist_ok=True)
        sentinel = tmp_path / "launcher-ran"
        (folder / "openclaw_launcher.sh").write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
        monkeypatch.setattr(wd, "_PROJECT_DIR", tmp_path)
        monkeypatch.setattr(wd, "get_provider", lambda _: {"id": "p1"})
        result = wd.restart_provider("p1")
        assert result["success"] is False
        assert "not supported" in result["error"].lower()
        assert not sentinel.exists()


# ── API endpoint tests ──────────────────────────────────────────────────────


ADMIN = {"Authorization": "Bearer adminkey"}


class TestWatchdogAPI:
    """Test /admin/watchdog/* HTTP endpoints."""

    @pytest.mark.asyncio
    async def test_watchdog_status_requires_admin(self, client):
        resp = await client.get(
            "/admin/watchdog", headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_watchdog_status_endpoint(self, admin_client):
        resp = await admin_client.get("/admin/watchdog", headers=ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert "running" in body
        assert "check_interval_s" in body
        assert "failure_threshold" in body
        assert "providers" in body

    @pytest.mark.asyncio
    async def test_watchdog_start_endpoint(self, admin_client, monkeypatch):
        import plugboard.provider_watchdog as wd

        async def fake_loop():
            while wd._running:
                await asyncio.sleep(0.01)

        monkeypatch.setattr(wd, "_watchdog_loop", fake_loop)
        monkeypatch.setattr(wd, "_watchdog_task", None)
        monkeypatch.setattr(wd, "_running", False)

        resp = await admin_client.post("/admin/watchdog/start", headers=ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert body["running"] is True

        wd.stop_watchdog()
        await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_watchdog_stop_endpoint(self, admin_client, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_running", False)
        monkeypatch.setattr(wd, "_watchdog_task", None)

        resp = await admin_client.post("/admin/watchdog/stop", headers=ADMIN)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_watchdog_events_endpoint(self, admin_client, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        wd._log_event("test-provider", "health_check", success=True, latency_ms=5.0)

        resp = await admin_client.get("/admin/watchdog/events", headers=ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["entries"]) >= 1

    @pytest.mark.asyncio
    async def test_watchdog_events_filter(self, admin_client, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        wd._log_event("p1", "health_check", success=True)
        wd._log_event("p2", "restart", success=True)

        resp = await admin_client.get(
            "/admin/watchdog/events?event_type=restart", headers=ADMIN
        )
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        assert all(e["event_type"] == "restart" for e in entries)

    @pytest.mark.asyncio
    async def test_watchdog_restart_stats_endpoint(
        self, admin_client, tmp_path, monkeypatch
    ):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        resp = await admin_client.get(
            "/admin/watchdog/restarts?hours=48", headers=ADMIN
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["period_hours"] == 48

    @pytest.mark.asyncio
    async def test_watchdog_manual_restart_endpoint(
        self, admin_client, tmp_path, monkeypatch
    ):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        # Mock restart to succeed
        monkeypatch.setattr(
            wd, "restart_provider", lambda pid: {"success": True, "output": "restarted"}
        )

        resp = await admin_client.post(
            "/admin/watchdog/test-provider/restart", headers=ADMIN
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    @pytest.mark.asyncio
    async def test_watchdog_manual_restart_failure(
        self, admin_client, tmp_path, monkeypatch
    ):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        monkeypatch.setattr(
            wd, "restart_provider", lambda pid: {"success": False, "error": "no script"}
        )

        resp = await admin_client.post(
            "/admin/watchdog/test-provider/restart", headers=ADMIN
        )
        assert resp.status_code == 500


# ── Edge cases ──────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge cases and robustness tests."""

    def test_log_event_truncates_long_details(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")

        long_details = "x" * 1000
        wd._log_event("p1", "restart", success=False, details=long_details)

        events = wd.get_watchdog_events()
        assert len(events[0]["details"]) <= 500

    def test_get_state_creates_on_demand(self):
        import plugboard.provider_watchdog as wd

        # Clear states
        wd._states.clear()
        state = wd._get_state("new-provider")
        assert state.provider_id == "new-provider"
        assert "new-provider" in wd._states

    def test_restart_ignores_env_file(self, tmp_path, monkeypatch):
        import plugboard.provider_watchdog as wd
        from pathlib import Path

        (tmp_path / ".env").write_text("MY_VAR=hello_world\n")
        monkeypatch.setattr(wd, "_PROJECT_DIR", tmp_path)
        monkeypatch.setattr(wd, "get_provider", lambda _: {"id": "p1"})

        def reject_read(*args, **kwargs):
            pytest.fail("restart must not read the retired launcher environment")

        monkeypatch.setattr(Path, "read_text", reject_read)
        result = wd.restart_provider("p1")
        assert result["success"] is False
        assert "not supported" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_loop_handles_exception_gracefully(self, tmp_path, monkeypatch):
        """Watchdog loop should not crash on provider errors."""
        import plugboard.provider_watchdog as wd

        monkeypatch.setattr(wd, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")
        monkeypatch.setattr(wd, "_CHECK_INTERVAL", 0)
        monkeypatch.setattr(wd, "_states", {})

        # load_providers raises
        call_count = 0

        def exploding_load():
            nonlocal call_count
            call_count += 1
            raise RuntimeError("boom")

        monkeypatch.setattr(wd, "load_providers", exploding_load)

        wd._running = True
        count = 0

        async def stop_after_two(_):
            nonlocal count
            count += 1
            if count >= 2:
                wd._running = False

        monkeypatch.setattr(asyncio, "sleep", stop_after_two)
        # Should not raise
        await wd._watchdog_loop()
        assert call_count >= 2  # Loop continued after exception
