"""Shared health-check logic.

Centralises how we probe a provider's health so that the dashboard
endpoint (/providers/{id}/health), the background monitoring snapshot
(monitoring.full_health_check), and the watchdog (provider_watchdog.ping_provider)
all agree on what "healthy" means.

Four cases handled:
    1. subprocess:<kind> — no HTTP; CLI binary present + --version OK
    2. sovereign providers — base_url without /health (e.g. Ollama)
    3. cloud with a known override URL (GitHub Models /catalog/models, Anthropic /v1/models, etc.)
    4. default — GET <base_url>/health
"""

from __future__ import annotations

import time
from typing import Any, Dict

import aiohttp
from plugboard.legacy_connectors import (
    require_supported_model,
    require_supported_provider,
)

# Explicit URL overrides for providers without a /health endpoint.
# Keep in sync with app.py (single source of truth lives here).
_HEALTH_URL_OVERRIDES = {
    "http://localhost:11434": "http://localhost:11434/",
    "https://models.github.ai/inference": "https://models.github.ai/catalog/models",
    "https://api.anthropic.com/v1": "https://api.anthropic.com/v1/models",
    "https://api.openai.com/v1": "https://api.openai.com/v1/models",
    "https://generativelanguage.googleapis.com/v1beta/openai": "https://generativelanguage.googleapis.com/v1beta/openai/models",
}


def health_url_for(provider: Dict[str, Any]) -> str:
    base = (provider.get("base_url") or "").rstrip("/")
    if base in _HEALTH_URL_OVERRIDES:
        return _HEALTH_URL_OVERRIDES[base]
    return base + "/health"


async def _probe_subprocess(auth_source: str) -> Dict[str, Any]:
    from plugboard.cli_subprocess import check_cli_installed, probe_cli_version

    kind = auth_source.removeprefix("subprocess:")
    installed = check_cli_installed(kind)
    if not installed["ok"]:
        return {
            "ok": False,
            "error": installed.get("error", f"CLI '{kind}' not installed"),
            "url": f"subprocess://{kind}",
            "latency_ms": 0.0,
        }
    probe = await probe_cli_version(kind)
    return {
        "ok": probe["ok"],
        "status": "running" if probe["ok"] else "down",
        "version": probe.get("version"),
        "error": probe.get("error", "") if not probe["ok"] else None,
        "url": f"subprocess://{kind}",
        "latency_ms": round(probe.get("latency_ms", 0), 1),
    }


async def _probe_http(
    url: str, headers: Dict[str, str], timeout_s: float = 5.0
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                latency = (time.perf_counter() - t0) * 1000
                # Strict: only 2xx and 3xx are "up". No more "OK 404".
                ok = 200 <= resp.status < 400
                return {
                    "ok": ok,
                    "status": resp.status,
                    "url": url,
                    "latency_ms": round(latency, 1),
                }
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        return {
            "ok": False,
            "error": str(exc)[:200],
            "url": url,
            "latency_ms": round(latency, 1),
        }


async def probe_provider(provider: Dict[str, Any]) -> Dict[str, Any]:
    """Unified health probe honouring the provider's auth_source."""
    try:
        require_supported_provider(provider)
        require_supported_model(provider.get("default_model", ""), effective_name=True)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "latency_ms": 0.0}
    auth_source = provider.get("auth_source", "") or ""

    # Case 1: subprocess providers have no HTTP endpoint at all.
    if auth_source.startswith("subprocess:"):
        return await _probe_subprocess(auth_source)

    # Case 2+3+4: HTTP probe with auth header if available.
    url = health_url_for(provider)
    try:
        from plugboard.router import _build_auth_header_async

        headers = await _build_auth_header_async(provider)
    except Exception:
        headers = {}
    headers.setdefault("Accept", "application/json")
    return await _probe_http(url, headers)
