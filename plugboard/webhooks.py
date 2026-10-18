"""
Webhooks — notify external URLs on events.

Events:
- key.created, key.revoked, key.deleted, key.rotated
- provider.created, provider.updated, provider.deleted
- spending.warning, spending.exceeded
- provider.down, provider.recovered
- cache.hit (optional, high-volume)

Configuration:
  WEBHOOK_URLS: comma-separated list of URLs to POST to
  WEBHOOK_EVENTS: comma-separated list of events to send (default: all)
  WEBHOOK_SECRET: HMAC-SHA256 secret for signing payloads
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp

log = logging.getLogger(__name__)

_WEBHOOK_URLS = [
    u.strip() for u in os.environ.get("WEBHOOK_URLS", "").split(",") if u.strip()
]
_WEBHOOK_EVENTS = os.environ.get("WEBHOOK_EVENTS", "").strip()
_WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

# Parse allowed events (empty = all)
_ALLOWED_EVENTS: Optional[set] = None
if _WEBHOOK_EVENTS:
    _ALLOWED_EVENTS = {e.strip() for e in _WEBHOOK_EVENTS.split(",")}


def _sign_payload(payload_bytes: bytes) -> str:
    """HMAC-SHA256 signature for webhook verification."""
    if not _WEBHOOK_SECRET:
        return ""
    return hmac.new(_WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()


async def emit(event: str, data: Dict[str, Any]) -> int:
    """
    Send a webhook event to all configured URLs.
    Returns number of successful deliveries.
    """
    if not _WEBHOOK_URLS:
        return 0

    if _ALLOWED_EVENTS and event not in _ALLOWED_EVENTS:
        return 0

    payload = {
        "event": event,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "data": data,
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode()
    signature = _sign_payload(payload_bytes)

    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "X-Plugboard-Event": event,
        # Pre-rename names, kept for existing receivers (deprecated).
        "X-LLM-Enabler-Event": event,
    }
    if signature:
        headers["X-Plugboard-Signature"] = f"sha256={signature}"
        headers["X-LLM-Enabler-Signature"] = f"sha256={signature}"

    delivered = 0
    async with aiohttp.ClientSession() as session:
        for url in _WEBHOOK_URLS:
            try:
                async with session.post(
                    url,
                    data=payload_bytes,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status < 300:
                        delivered += 1
                    else:
                        log.warning(
                            "Webhook %s returned %d for event %s",
                            url,
                            resp.status,
                            event,
                        )
            except Exception as exc:
                log.warning("Webhook %s failed for event %s: %s", url, event, exc)

    return delivered


async def emit_fire_and_forget(event: str, data: Dict[str, Any]) -> None:
    """Fire-and-forget webhook emission (non-blocking)."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(emit(event, data))
    except Exception:
        pass
