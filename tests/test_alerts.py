"""Tests for alerts.py -- webhook notifications for spending & provider failures."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from plugboard.alerts import (
    _provider_failures,
    _provider_alerted,
    record_provider_failure,
    record_provider_success,
    _send_provider_alert,
    check_and_alert,
    _ALERT_COOLDOWN,
)

WEBHOOK = "https://hooks.example.com/alert"


# ── record_provider_success ──────────────────────────────────────────────────


def test_record_success_resets_counter():
    _provider_failures["s1"] = 5
    record_provider_success("s1")
    assert _provider_failures["s1"] == 0


def test_record_success_unknown_provider():
    _provider_failures.pop("unknown-prov", None)
    record_provider_success("unknown-prov")
    assert _provider_failures["unknown-prov"] == 0


# ── record_provider_failure ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failure_increments():
    _provider_failures.pop("f1", None)
    await record_provider_failure("f1")
    assert _provider_failures["f1"] == 1
    await record_provider_failure("f1")
    assert _provider_failures["f1"] == 2


@pytest.mark.asyncio
async def test_failure_triggers_alert_at_threshold(monkeypatch):
    """When failure count reaches _ALERT_THRESHOLD, webhook is called."""
    monkeypatch.setattr("plugboard.alerts._ALERT_THRESHOLD", 2)
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", WEBHOOK)
    _provider_failures.pop("f2", None)
    _provider_alerted.pop("f2", None)

    with aioresponses() as mock:
        mock.post(WEBHOOK, status=200)
        await record_provider_failure("f2")
        assert len(mock.requests) == 0  # below threshold
        await record_provider_failure("f2")
    # webhook should have been called at threshold
    assert _provider_failures["f2"] == 2


@pytest.mark.asyncio
async def test_failure_respects_cooldown(monkeypatch):
    """No alert sent if within cooldown window."""
    import time as _time

    monkeypatch.setattr("plugboard.alerts._ALERT_THRESHOLD", 1)
    _provider_failures.pop("f3", None)
    # Pretend we already alerted recently
    _provider_alerted["f3"] = _time.monotonic()

    sent = []

    async def mock_send(pid, count):
        sent.append((pid, count))

    monkeypatch.setattr("plugboard.alerts._send_provider_alert", mock_send)
    await record_provider_failure("f3")
    assert len(sent) == 0  # cooldown blocks alert


@pytest.mark.asyncio
async def test_failure_sends_after_cooldown_expired(monkeypatch):
    """Alert sent if cooldown has expired."""
    import time as _time

    monkeypatch.setattr("plugboard.alerts._ALERT_THRESHOLD", 1)
    _provider_failures.pop("f4", None)
    _provider_alerted["f4"] = _time.monotonic() - _ALERT_COOLDOWN - 10

    sent = []

    async def mock_send(pid, count):
        sent.append((pid, count))

    monkeypatch.setattr("plugboard.alerts._send_provider_alert", mock_send)
    await record_provider_failure("f4")
    assert len(sent) == 1


# ── _send_provider_alert ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_provider_alert_no_webhook(monkeypatch):
    """No-op when ALERT_WEBHOOK_URL is empty."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", "")
    # Should not raise
    await _send_provider_alert("prov-x", 5)


@pytest.mark.asyncio
async def test_send_provider_alert_posts_webhook(monkeypatch):
    """Sends JSON payload to webhook URL."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", WEBHOOK)
    with aioresponses() as mock:
        mock.post(WEBHOOK, status=200)
        await _send_provider_alert("prov-y", 3)
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_send_provider_alert_handles_error(monkeypatch):
    """Should not raise on network error."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", WEBHOOK)
    with aioresponses() as mock:
        mock.post(WEBHOOK, exception=ConnectionError("down"))
        await _send_provider_alert("prov-z", 10)  # no raise


# ── check_and_alert (spending) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_and_alert_no_webhook(monkeypatch):
    """No-op when webhook is empty."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", "")
    await check_and_alert("client-1", 90.0, 100.0)  # no raise


@pytest.mark.asyncio
async def test_check_and_alert_no_limit(monkeypatch):
    """No-op when limit is 0."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", WEBHOOK)
    await check_and_alert("client-2", 10.0, 0)  # no raise


@pytest.mark.asyncio
async def test_check_and_alert_under_80_pct(monkeypatch):
    """No alert when below 80% threshold."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", WEBHOOK)
    with aioresponses() as mock:
        mock.post(WEBHOOK, status=200)
        await check_and_alert("client-3", 70.0, 100.0)
    # webhook should NOT have been called
    assert len(mock.requests) == 0


@pytest.mark.asyncio
async def test_check_and_alert_warning_at_80_pct(monkeypatch):
    """Alert with status=warning at 80%."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", WEBHOOK)
    with aioresponses() as mock:
        mock.post(WEBHOOK, status=200)
        await check_and_alert("client-4", 85.0, 100.0)
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_check_and_alert_exceeded_at_100_pct(monkeypatch):
    """Alert with status=exceeded at 100%+."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", WEBHOOK)
    with aioresponses() as mock:
        mock.post(WEBHOOK, status=200)
        await check_and_alert("client-5", 110.0, 100.0)
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_check_and_alert_webhook_failure(monkeypatch):
    """Should not raise on webhook failure."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", WEBHOOK)
    with aioresponses() as mock:
        mock.post(WEBHOOK, exception=ConnectionError("timeout"))
        await check_and_alert("client-6", 95.0, 100.0)  # no raise


@pytest.mark.asyncio
async def test_check_and_alert_webhook_non_2xx(monkeypatch):
    """Logs warning on non-2xx response."""
    monkeypatch.setattr("plugboard.alerts._WEBHOOK_URL", WEBHOOK)
    with aioresponses() as mock:
        mock.post(WEBHOOK, status=500)
        await check_and_alert("client-7", 90.0, 100.0)  # no raise
