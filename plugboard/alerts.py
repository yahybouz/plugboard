"""Alerts -- webhook notifications for spending limits and provider failures."""

from __future__ import annotations

import logging
import os
import time as _time
from typing import Dict

import aiohttp

log = logging.getLogger(__name__)

_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()

# -- Provider failure tracking ------------------------------------------------

_ALERT_THRESHOLD = int(os.environ.get("ALERT_FAILURE_THRESHOLD", "3"))
_provider_failures: Dict[str, int] = {}
_provider_alerted: Dict[str, float] = {}  # last alert timestamp (monotonic)
_ALERT_COOLDOWN = 300.0  # 5 minutes between alerts for same provider


async def record_provider_failure(provider_id: str) -> None:
    """Increment failure count and alert if threshold reached."""
    _provider_failures[provider_id] = _provider_failures.get(provider_id, 0) + 1
    count = _provider_failures[provider_id]

    if count >= _ALERT_THRESHOLD:
        now = _time.monotonic()
        last = _provider_alerted.get(provider_id, 0)
        if now - last > _ALERT_COOLDOWN:
            _provider_alerted[provider_id] = now
            await _send_provider_alert(provider_id, count)


def record_provider_success(provider_id: str) -> None:
    """Reset failure count on success."""
    _provider_failures[provider_id] = 0


async def _send_provider_alert(provider_id: str, failure_count: int) -> None:
    if not _WEBHOOK_URL:
        return
    payload = {
        "type": "provider_down",
        "provider_id": provider_id,
        "consecutive_failures": failure_count,
        "message": f"Provider '{provider_id}' has failed {failure_count} consecutive times",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _WEBHOOK_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                log.info(
                    "Provider down alert sent for %s (HTTP %d)",
                    provider_id,
                    resp.status,
                )
    except Exception as exc:
        log.warning("Provider alert webhook failed: %s", exc)


# -- Spending alerts ----------------------------------------------------------


async def check_and_alert(client_id: str, current_cost: float, limit: float) -> None:
    """Send alert if client has reached 80% of their spending limit."""
    if not _WEBHOOK_URL or limit <= 0:
        return
    pct = current_cost / limit
    if pct < 0.8:
        return
    status = "exceeded" if pct >= 1.0 else "warning"
    payload = {
        "type": "spending_alert",
        "status": status,
        "client_id": client_id,
        "current_cost_usd": round(current_cost, 4),
        "limit_usd": limit,
        "percentage": round(pct * 100, 1),
        "message": (
            f"Client '{client_id}' has used {pct * 100:.0f}% of their "
            f"${limit:.2f} monthly limit (${current_cost:.4f})"
        ),
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _WEBHOOK_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status < 300:
                    log.info("Alert sent for %s (%s)", client_id, status)
                else:
                    log.warning(
                        "Alert webhook returned %d for %s", resp.status, client_id
                    )
    except Exception as exc:
        log.warning("Alert webhook failed: %s", exc)
