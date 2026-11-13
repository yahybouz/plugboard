"""Alerts -- webhook notifications for spending limits and provider failures."""

from __future__ import annotations

import logging
import os
import time as _time
from typing import Dict

import aiohttp

from plugboard import envcompat

log = logging.getLogger(__name__)

_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()

# -- Provider failure tracking ------------------------------------------------

_ALERT_THRESHOLD = envcompat.get_int("ALERT_FAILURE_THRESHOLD", default=3)
_provider_failures: Dict[str, int] = {}
_provider_alerted: Dict[str, float] = {}  # last alert timestamp (monotonic)
_ALERT_COOLDOWN = 300.0  # 5 minutes between alerts for same provider


async def record_provider_failure(provider_id: str) -> None:
    """Increment failure count and alert if threshold reached.

    Call it only for a failure of the provider itself, judged by the circuit
    breaker's rule (circuit_breaker.is_provider_fault): an error the request
    caused (HTTP 400, 413, 422, a prompt too long...) neither counts nor
    resets the count.
    """
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


async def send_retention_held_alert(reason: str) -> None:
    """Alert that the daily retention pass is held by its clock check (BUG-81)."""
    if not _WEBHOOK_URL:
        return
    payload = {"type": "retention_held", "message": f"Data retention pass {reason}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _WEBHOOK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                log.info("Retention held alert sent (HTTP %d)", resp.status)
    except Exception as exc:
        log.warning("Retention held alert webhook failed: %s", exc)


async def send_egress_refused_alert(provider_id: str, host: str) -> None:
    """Alert that a protected provider's host resolves to a non-public address."""
    if not _WEBHOOK_URL:
        return
    payload = {
        "type": "provider_egress_refused",
        "provider_id": provider_id,
        "host": host,
        "message": (
            f"Provider '{provider_id}': {host} resolves to a non-public address; "
            "requests are refused"
        ),
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _WEBHOOK_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                log.info(
                    "Egress refused alert sent for %s (HTTP %d)",
                    provider_id,
                    resp.status,
                )
    except Exception as exc:
        log.warning("Egress refused alert webhook failed: %s", exc)


async def send_secret_unreadable_alert(provider_id: str) -> None:
    """Alert that a provider's stored key cannot be decrypted (BUG-57)."""
    if not _WEBHOOK_URL:
        return
    payload = {
        "type": "provider_secret_unreadable",
        "provider_id": provider_id,
        "message": (
            f"Provider '{provider_id}' is turned off: its stored key cannot be "
            "decrypted with the current ENCRYPTION_KEY"
        ),
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _WEBHOOK_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                log.info(
                    "Unreadable key alert sent for %s (HTTP %d)",
                    provider_id,
                    resp.status,
                )
    except Exception as exc:
        log.warning("Unreadable key alert webhook failed: %s", exc)


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
