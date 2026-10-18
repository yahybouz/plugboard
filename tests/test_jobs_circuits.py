"""Tests for circuit_breaker and jobs modules."""

from __future__ import annotations

import time
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from plugboard import circuit_breaker as cb
from plugboard import jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_circuits(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Inject an empty _circuits dict and return it."""
    circuits: Dict[str, Any] = {}
    monkeypatch.setattr(cb, "_circuits", circuits)
    return circuits


# ===================================================================
# circuit_breaker.py tests
# ===================================================================


class TestCircuitStartsClosed:
    """1 — new circuit state is CLOSED."""

    def test_circuit_starts_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fresh_circuits(monkeypatch)
        result = cb.check_circuit("test-model")
        assert result["state"] == cb.CLOSED
        assert result["allowed"] is True
        assert result["fallback"] is None


class TestRecordSuccessResetsFailures:
    """2 — success resets failure count."""

    def test_record_success_resets_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_circuits(monkeypatch)
        cb.record_failure("m")
        cb.record_failure("m")
        assert cb._get_circuit("m").failures == 2
        cb.record_success("m")
        assert cb._get_circuit("m").failures == 0


class TestRecordFailureIncrements:
    """3 — failure increments count."""

    def test_record_failure_increments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fresh_circuits(monkeypatch)
        cb.record_failure("m")
        assert cb._get_circuit("m").failures == 1
        cb.record_failure("m")
        assert cb._get_circuit("m").failures == 2


class TestCircuitOpensAfterThreshold:
    """4 — 3 failures opens circuit."""

    def test_circuit_opens_after_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_circuits(monkeypatch)
        for _ in range(3):
            cb.record_failure("m")
        assert cb._get_circuit("m").state == cb.OPEN


class TestOpenCircuitRejects:
    """5 — check_circuit returns allowed=False when open."""

    def test_open_circuit_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fresh_circuits(monkeypatch)
        for _ in range(3):
            cb.record_failure("m")
        result = cb.check_circuit("m")
        assert result["allowed"] is False
        assert result["state"] == cb.OPEN


class TestOpenCircuitSuggestsFallback:
    """6 — fallback from FALLBACK_MAP."""

    def test_open_circuit_suggests_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_circuits(monkeypatch)
        model = "claude-sonnet-4-5"
        for _ in range(3):
            cb.record_failure(model)
        result = cb.check_circuit(model)
        assert result["allowed"] is False
        assert result["fallback"] == "claude-haiku-4-5"


class TestCircuitHalfOpenAfterCooldown:
    """7 — after cooldown, state=half_open."""

    def test_circuit_half_open_after_cooldown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_circuits(monkeypatch)
        for _ in range(3):
            cb.record_failure("m")

        # Simulate time passing beyond cooldown
        circuit = cb._get_circuit("m")
        mono_calls = iter([circuit.opened_at + 61.0])
        original_monotonic = time.monotonic
        monkeypatch.setattr(
            time, "monotonic", lambda: next(mono_calls, original_monotonic())
        )

        result = cb.check_circuit("m")
        assert result["state"] == cb.HALF_OPEN
        assert result["allowed"] is True


class TestHalfOpenSuccessCloses:
    """8 — success in half_open closes circuit."""

    def test_half_open_success_closes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fresh_circuits(monkeypatch)
        circuit = cb._get_circuit("m")
        circuit.state = cb.HALF_OPEN
        circuit.failures = 3
        cb.record_success("m")
        assert circuit.state == cb.CLOSED
        assert circuit.failures == 0


class TestHalfOpenFailureReopens:
    """9 — failure in half_open reopens."""

    def test_half_open_failure_reopens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fresh_circuits(monkeypatch)
        circuit = cb._get_circuit("m")
        circuit.state = cb.HALF_OPEN
        cb.record_failure("m")
        assert circuit.state == cb.OPEN


class TestGetAllCircuits:
    """10 — returns all tracked circuits."""

    def test_get_all_circuits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fresh_circuits(monkeypatch)
        cb.record_failure("model-a")
        cb.record_failure("model-b")
        result = cb.get_all_circuits()
        assert "model-a" in result
        assert "model-b" in result
        assert result["model-a"]["failures"] == 1


class TestResetCircuit:
    """11 — manual reset works."""

    def test_reset_circuit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fresh_circuits(monkeypatch)
        for _ in range(3):
            cb.record_failure("m")
        assert cb._get_circuit("m").state == cb.OPEN
        cb.reset_circuit("m")
        assert cb._get_circuit("m").state == cb.CLOSED
        assert cb._get_circuit("m").failures == 0


class TestGetModelTimeout:
    """12 — returns tier-based timeout."""

    def test_get_model_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_info = MagicMock(return_value={"tier": "reasoning"})
        monkeypatch.setattr(
            "plugboard.circuit_breaker.get_model_info",
            mock_info,
            raising=False,
        )
        # We need to mock the import inside the function
        import plugboard.benchmarks as bm

        monkeypatch.setattr(bm, "get_model_info", mock_info)
        result = cb.get_model_timeout("o3")
        assert result == 900.0


class TestExtractSuffix:
    """13 — extracts model suffix from full ID."""

    def test_extract_suffix_double_dash(self) -> None:
        assert cb._extract_suffix("provider--claude-sonnet-4-5") == "claude-sonnet-4-5"

    def test_extract_suffix_slash(self) -> None:
        assert cb._extract_suffix("anthropic/claude-sonnet-4-5") == "claude-sonnet-4-5"

    def test_extract_suffix_plain(self) -> None:
        assert cb._extract_suffix("claude-sonnet-4-5") == "claude-sonnet-4-5"


# ===================================================================
# jobs.py tests
# ===================================================================


class TestGetJobLogNotFound:
    """15 — unknown job returns error."""

    def test_get_job_log_not_found(self) -> None:
        result = jobs.get_job_log("nonexistent_job")
        assert "error" in result
        assert "not found" in result["error"]


class TestRunJobNotFound:
    """18 — unknown job returns error."""

    @pytest.mark.anyio
    async def test_run_job_not_found(self) -> None:
        result = await jobs.run_job("nonexistent_job")
        assert "error" in result
        assert "not found" in result["error"]


class _FakeScheduler:
    def __init__(self, **status):
        self.status = {
            "disabled": False,
            "refresh_interval_h": 6,
            "running_since": None,
            **status,
        }
        self.running_since = self.status["running_since"]
        self.runs = []

    def status_dict(self):
        return dict(self.status)

    async def run_now(self, kind="manual"):
        self.runs.append(kind)


class _State:
    pass


class TestInProcessJobs:
    """The Jobs page lists the in-process schedulers, not crontab scripts."""

    def test_status_reflects_the_schedulers(self) -> None:
        state = _State()
        state.pricing_scheduler = _FakeScheduler(
            last_run_at="2026-10-18T06:00:00+00:00",
            last_success_at="2026-10-18T06:00:00+00:00",
        )
        # One real scheduler exposes status_dict as a property.
        prop = _FakeScheduler(last_error="boom")
        prop.status_dict = prop.status_dict()
        state.model_availability_scheduler = prop
        by_id = {j["id"]: j for j in jobs.get_jobs_status(state)}
        assert set(by_id) == {"pricing_refresh", "model_refresh", "provider_watchdog"}
        assert by_id["pricing_refresh"]["last_status"] == "success"
        assert by_id["pricing_refresh"]["schedule"] == "every 6 h (in-process)"
        assert by_id["pricing_refresh"]["next_run"] == "2026-10-18 12:00 UTC"
        assert by_id["model_refresh"]["last_status"] == "error"
        assert by_id["provider_watchdog"]["next_run"] == "continuous"

    def test_without_schedulers_jobs_show_not_started(self) -> None:
        by_id = {j["id"]: j for j in jobs.get_jobs_status(None)}
        assert by_id["pricing_refresh"]["schedule"] == "not started"
        assert by_id["pricing_refresh"]["last_status"] == "disabled"

    @pytest.mark.anyio
    async def test_run_now_starts_the_scheduler(self) -> None:
        import asyncio

        state = _State()
        state.pricing_scheduler = _FakeScheduler()
        result = await jobs.run_job("pricing_refresh", state)
        assert result["status"] == "started"
        await asyncio.sleep(0)
        assert state.pricing_scheduler.runs == ["manual"]

    @pytest.mark.anyio
    async def test_run_now_while_running_does_not_start_twice(self) -> None:
        state = _State()
        state.model_availability_scheduler = _FakeScheduler(
            running_since="2026-10-18T10:00:00+00:00"
        )
        result = await jobs.run_job("model_refresh", state)
        assert result["status"] == "running"
        assert state.model_availability_scheduler.runs == []

    @pytest.mark.anyio
    async def test_watchdog_cannot_be_started(self) -> None:
        result = await jobs.run_job("provider_watchdog", _State())
        assert result["status"] == "error"

    def test_details_show_the_last_result(self) -> None:
        state = _State()
        state.pricing_scheduler = _FakeScheduler(last_result={"added": 3})
        detail = jobs.get_job_log("pricing_refresh", app_state=state)
        assert detail["status"] == "never_run"
        assert '"added": 3' in detail["output"]
        assert "error" in jobs.get_job_log("nope")
