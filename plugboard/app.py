"""
Plugboard -- self-hosted LLM gateway: one API for every provider.

Endpoints:
  POST /v1/chat/completions    -- proxy to configured provider (streaming supported)
  GET  /v1/models              -- list all available models
  GET  /health                 -- liveness check
  GET  /usage/me               -- token usage for the calling client (current month)
  GET  /v1/me                  -- client info + usage + spending limit

  GET  /providers              -- list providers (admin)
  POST /providers              -- add a provider (admin)
  PUT  /providers/{id}         -- update a provider (admin)
  DELETE /providers/{id}       -- delete a provider (admin)
  GET  /providers/{id}/health  -- ping a provider (admin)

  GET  /metrics                -- request metrics (admin)

  GET  /admin/keys             -- list client API keys (admin)
  POST /admin/keys             -- create a client API key (admin)
  POST /admin/keys/{key_id}/revoke  -- revoke a key (admin)
  DELETE /admin/keys/{key_id}  -- delete a key (admin)

  GET  /admin/pricing/{month}  -- get pricing grid for a month (admin)
  PUT  /admin/pricing/{month}  -- upsert pricing grid for a month (admin)

  GET    /admin/usage/{month}               -- all usage for a month (admin, paginated)
  DELETE /admin/usage/{month}               -- delete a month's usage data (admin)
  GET    /admin/usage/{month}/{client_id}   -- usage for one client (admin)

Authentication:
  ADMIN_KEY env var (required) -- required for admin endpoints.
  Client API keys stored in data/api_keys.json -- required for /v1/* endpoints.
  API_KEY env var -- legacy fallback if no client keys exist.
  Set API_KEY="" to disable client auth (admin auth remains enforced).
"""

from __future__ import annotations

import asyncio as _asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import pathlib
import re
import secrets
import time
import uuid
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware

from plugboard import __version__
from plugboard import admin_sessions
from plugboard import billing as _billing
from plugboard import usage as _usage
from plugboard.alerts import check_and_alert as _check_and_alert
from plugboard.audit import count_audit_log, get_audit_log, log_action as _audit
from plugboard.webhooks import emit_fire_and_forget as _webhook
from plugboard.apikeys import (
    KEY_PREFIXES,  # ARCH-DEBT-029
    create_key,
    delete_key,
    get_key_record,
    get_keys_by_client,
    get_keys_by_org,
    get_orgs,
    load_keys,
    mask_key,
    revoke_key,
    rotate_key,
)
from plugboard import config as _cfg_mod
from plugboard.config import (
    create_provider,
    delete_provider,
    dotenv_permission_warning,
    get_provider,
    load_providers,
    mask_token,
    update_provider,
)
from plugboard.logging_config import _request_id_var, setup_logging
from plugboard.metrics import get_stats
from plugboard.ratelimit import check_client_rate_limit, check_rate_limit
from plugboard.decision_api import router as decision_router
from plugboard.legacy_connectors import (
    REMOVED_CONNECTOR_ERROR,
    is_removed_provider,
    is_removed_model,
    require_supported_model,
    require_supported_provider,
)
from plugboard.decisions import is_decision_model
from plugboard.cli_auth import AuthError
from plugboard.cli_subprocess import CliSubprocessError
from plugboard.router import (
    discover_models,
    forward,
    forward_stream,
    invalidate_models_cache,
    list_models,
    resolve_provider_and_model,
    served_by as _served_by,
)
from plugboard.tokenizer import (
    count_tokens,
    estimate_completion_tokens,
    estimate_prompt_tokens,
)

setup_logging()
log = logging.getLogger("plugboard.app")


# ARCH-DEBT-026 closed (opt-in): timing side-channel mitigation for cache HIT.
# When > 0, a HIT response is delayed until at least this many ms have elapsed
# since the request started. Hides "this prompt is cached" from a LAN-level
# attacker timing the response. Default 0 = off (the leak requires a
# co-located adversary which is out of scope for most deployments).
_CACHE_RESPONSE_FLOOR_MS = max(
    0, int(os.environ.get("PLUGBOARD_CACHE_RESPONSE_FLOOR_MS", "0") or "0")
)


async def _pad_cache_hit_response(t0: float) -> None:
    """Sleep enough to reach the configured cache-response floor.

    No-op if the floor is unset/zero or already exceeded by the request's
    own work. Uses asyncio.sleep so we don't block the event loop.
    """
    if _CACHE_RESPONSE_FLOOR_MS <= 0:
        return
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    remaining_ms = _CACHE_RESPONSE_FLOOR_MS - elapsed_ms
    if remaining_ms > 0:
        await _asyncio.sleep(remaining_ms / 1000.0)


def _is_comment_text(value: Optional[str]) -> bool:
    """True for "#" or "#" + whitespace: a .env comment read as a value.

    python-dotenv reads ``KEY=   # note`` as the value ``"# note"``, so a key
    copied unfilled from an older .env.example holds public text. A real key
    may still start with "#".
    """
    value = (value or "").lstrip()
    return value == "#" or (value.startswith("#") and value[1:2].isspace())


def _reject_comment_valued_secrets(env) -> None:
    """Check secrets for .env comment text read as a value.

    Raises for API_KEY/ADMIN_KEY (anyone could authenticate with them);
    only logs CRITICAL for ENCRYPTION_KEY (see below).
    """
    for name in ("API_KEY", "ADMIN_KEY"):
        if _is_comment_text(env.get(name)):
            raise RuntimeError(
                f"{name} holds comment text from .env.example, which is public: "
                "set a random value (openssl rand -hex 24) and restart"
            )
    # Changing ENCRYPTION_KEY would make stored tokens unreadable: warn only.
    if _is_comment_text(env.get("ENCRYPTION_KEY")):
        log.critical(
            "ENCRYPTION_KEY holds comment text from .env.example: provider "
            "tokens, OAuth credentials and the admin MFA seed are encrypted with "
            "a key anyone can derive. Keep the value for now (changing it makes "
            "them unreadable and breaks MFA login) and protect data/; a "
            "re-encryption tool is tracked in TODOS.md."
        )


_reject_comment_valued_secrets(os.environ)


# API_KEY default semantics (decision 2026-04-23, TM-007):
#
# * Empty default. No well-known sentinel like "local" (previous value)
#   that attackers could guess. When unset, client auth is explicitly
#   disabled and the server screams about it.
# * `PLUGBOARD_DEV=1` is the explicit opt-in for local development:
#   the warning fires once at boot and the `/health` banner stays off.
# * Any other context (no DEV flag) = treat as production: warning is
#   promoted to CRITICAL, repeated periodically by the lifespan, and
#   `/health` advertises `auth_disabled: true` so load balancers /
#   deployment gates can refuse traffic to the instance.
_API_KEY = os.environ.get("API_KEY", "")
_ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
_DEV_MODE = os.environ.get("PLUGBOARD_DEV", "").strip() == "1"
_ADMIN_COOKIE_NAME = "plugboard_admin_session"


_ADMIN_KEY_MIN_LENGTH = 16


def _require_admin_key_outside_dev(admin_key: str, dev_mode: bool) -> None:
    """Secure by default: without ADMIN_KEY the admin API and dashboard would
    be open (both keys empty) or unusable. Only PLUGBOARD_DEV=1 allows it."""
    if not admin_key and not dev_mode:
        raise RuntimeError(
            "ADMIN_KEY is not set: run `make init` (or set a random ADMIN_KEY in "
            ".env). For throwaway local work only, PLUGBOARD_DEV=1 allows "
            "starting without it."
        )
    if admin_key and not dev_mode and len(admin_key) < _ADMIN_KEY_MIN_LENGTH:
        raise RuntimeError(
            f"ADMIN_KEY is shorter than {_ADMIN_KEY_MIN_LENGTH} characters: use a "
            "random value (`make init` generates 48 hex characters)."
        )


_require_admin_key_outside_dev(_ADMIN_KEY, _DEV_MODE)
_ADMIN_SESSION_MAX_AGE = int(os.environ.get("ADMIN_SESSION_MAX_AGE", "28800"))  # 8h
_COOKIE_SECURE_MODE = os.environ.get("COOKIE_SECURE", "auto").strip().lower()

if not _API_KEY:
    if _DEV_MODE:
        log.warning(
            "API_KEY is empty — client auth disabled. "
            "Running in DEV mode (PLUGBOARD_DEV=1). Never deploy like this."
        )
    else:
        log.critical(
            "API_KEY is empty and PLUGBOARD_DEV is not set — CLIENT AUTH DISABLED. "
            "Any request to /v1/* will be accepted without a bearer token. "
            "Set API_KEY to a random secret (or PLUGBOARD_DEV=1 for local work)."
        )

# ARCH-DEBT-027 closed (LITE): AUTH_MODE is now a derived module-level
# string surfaced for observability + future enum migration. Three modes:
#   - "DISABLED" : API_KEY="" — /v1/* OPEN, only admin cookie/key gate
#   - "LEGACY"   : API_KEY set + permissive cache (PLUGBOARD_ALLOW_PERMISSIVE=1)
#   - "ENFORCED" : API_KEY set + strict tenant (default v3.0.0+, ADR-032)
# Full enum + isolation of the 3 branches in _resolve_client moves to a
# follow-up sprint when a 4th comportement is needed (manifesto §30.5).
from plugboard.cache_common import strict_tenant_mode as _strict_tenant_mode  # noqa: E402

if not _API_KEY:
    AUTH_MODE = "DISABLED"
elif _strict_tenant_mode():
    AUTH_MODE = "ENFORCED"
else:
    AUTH_MODE = "LEGACY"
log.info("auth.mode=%s api_key_set=%s", AUTH_MODE, bool(_API_KEY))


# v2.8.4 MF1 — toxic combination guard:
# Strict tenant mode demands every cache call carry a non-empty client_id.
# With API_KEY="" and an anonymous request (no bearer), _resolve_client
# returns None, which becomes "" downstream → cache.py raises ValueError →
# the chat endpoint crashes with a 500. Refuse the combination at boot so
# operators learn about the misconfig before serving traffic.
_STRICT_TENANT_LEGACY_RAW = (
    os.environ.get("PLUGBOARD_STRICT_TENANT", "").strip().lower()
)
if _strict_tenant_mode() and not _API_KEY:
    # Keep backward compatibility with tests/operators that explicitly force
    # strict mode via the legacy variable: this configuration is refused.
    if _STRICT_TENANT_LEGACY_RAW in {"1", "true", "yes", "on"}:
        raise RuntimeError(
            "Invalid configuration: STRICT_TENANT explicitly enabled while API_KEY is empty. "
            "Set API_KEY or disable strict mode (PLUGBOARD_ALLOW_PERMISSIVE=1 / "
            "legacy PLUGBOARD_STRICT_TENANT=0)."
        )
    log.critical(
        "Invalid configuration detected: strict tenant mode is enabled while API_KEY is empty. "
        "Cache access may fail for anonymous requests. Set API_KEY or "
        "PLUGBOARD_ALLOW_PERMISSIVE=1 (legacy: PLUGBOARD_STRICT_TENANT=0)."
    )

# Issue #7: CORS -- restrict to known origins
# Dev mode (no auth): default to * for convenience
# Prod mode (auth enabled): default to empty (no CORS) — must be explicit
_cors_raw = os.environ.get("CORS_ORIGINS", "").strip()
if not _cors_raw:
    _cors_raw = "*" if (not _API_KEY and not _ADMIN_KEY) else ""
if _cors_raw and _API_KEY and _ADMIN_KEY and _cors_raw == "*":
    log.warning("CORS_ORIGINS=* in production — restrict to specific domains")
_CORS_ORIGINS = (
    [o.strip() for o in _cors_raw.split(",") if o.strip()] if _cors_raw else []
)

# SEC-2026-10: `.env` holds ADMIN_KEY and ENCRYPTION_KEY; warn at boot when
# other local users can read it.
_dotenv_warning = dotenv_permission_warning()
if _dotenv_warning:
    log.warning(_dotenv_warning)


@asynccontextmanager
async def _lifespan(application: FastAPI):  # noqa: ARG001
    """Start background services on startup, stop on shutdown."""
    # Auto-start provider watchdog
    from plugboard.provider_watchdog import start_watchdog, stop_watchdog

    start_watchdog()
    log.info("Provider watchdog auto-started")
    # Auto-register Ollama if detected
    from plugboard.ollama import bootstrap_ollama

    ollama_prov = await bootstrap_ollama()
    if ollama_prov:
        log.info(
            "Ollama auto-registered: %s (%s)",
            ollama_prov["id"],
            ollama_prov.get("base_url"),
        )
    # Warm up installed subprocess CLIs (gemini/codex/claude) so the first
    # user request saves the cold-start cost (Node.js init, OAuth read, DNS).
    # Runs in a background task so boot isn't blocked if a CLI is slow.
    import asyncio as _asyncio
    from plugboard.cli_subprocess import warm_up_all_installed

    # Fire-and-forget boot tasks. The event loop only keeps a weak reference
    # to a task, so one that nothing holds can be garbage-collected while it
    # still runs; these references keep them alive until they finish, and
    # whatever is still pending is cancelled at shutdown.
    _boot_tasks: set[_asyncio.Task] = set()

    def _spawn_boot_task(coro, name: str) -> None:
        task = _asyncio.create_task(coro, name=name)
        _boot_tasks.add(task)
        task.add_done_callback(_boot_tasks.discard)

    async def _warm():
        try:
            results = await warm_up_all_installed(timeout_s=10.0)
            for kind, r in results.items():
                if r.get("ok"):
                    log.info(
                        "subprocess warm-up %s: %s (%.2fs)",
                        kind,
                        r.get("version", "?"),
                        r.get("warmup_elapsed_s", 0),
                    )
                else:
                    log.warning(
                        "subprocess warm-up %s failed: %s", kind, r.get("error")
                    )
        except Exception as exc:
            log.debug("subprocess warm-up background task error: %s", exc)

    _spawn_boot_task(_warm(), "subprocess-warm-up")

    # Repeat the auth-disabled warning every 5 minutes in non-dev contexts so
    # long-running deployments can't forget they are wide open (TM-007).
    async def _auth_nag():
        # Cancelled at shutdown; CancelledError propagates on its own.
        while True:
            await _asyncio.sleep(300)
            if not _API_KEY and not _DEV_MODE:
                log.critical(
                    "API_KEY still empty — /v1/* remains OPEN. "
                    "Set API_KEY or PLUGBOARD_DEV=1."
                )

    _nag_task = None
    if not _API_KEY and not _DEV_MODE:
        _nag_task = _asyncio.create_task(_auth_nag())

    # PricingScheduler — refreshes published prices in-process (no crontab)
    # (manifesto §29.7). Bootstraps current month's pricing once providers
    # are loaded, then refreshes daily at PRICING_DAILY_HOUR_UTC. Opt-out
    # via PRICING_SCHEDULER_DISABLED=1. Status exposed at
    # /admin/pricing/scheduler/status for SRE diagnostics.
    from plugboard.pricing_scheduler import PricingScheduler

    _pricing_scheduler = PricingScheduler()
    app.state.pricing_scheduler = _pricing_scheduler
    await _pricing_scheduler.start()
    log.info(
        "PricingScheduler started (disabled=%s, daily_hour_utc=%s)",
        _pricing_scheduler.disabled,
        _pricing_scheduler.daily_hour_utc,
    )

    # ModelAvailabilityScheduler — daily account-aware catalog refresh.
    # Pricing/native discovery can tell us that a model exists; this scheduler
    # verifies subscription-backed CLI models against the logged-in account and
    # syncs native catalogs such as Ollama's installed model list.
    from plugboard.model_availability import ModelAvailabilityScheduler

    _model_availability_scheduler = ModelAvailabilityScheduler()
    app.state.model_availability_scheduler = _model_availability_scheduler
    await _model_availability_scheduler.start()
    log.info(
        "ModelAvailabilityScheduler started (disabled=%s, daily_hour_utc=%s)",
        _model_availability_scheduler.disabled,
        _model_availability_scheduler.daily_hour_utc,
    )

    # Boot-time internet probe — auto-refresh internet_access flags so the
    # playground dropdown displays correct 🌐/💻 icons without admin click.
    # Runs in background after schedulers are up so boot isn't blocked.
    # Opt-out via PLUGBOARD_INTERNET_CHECK_ON_BOOT=0.
    _internet_boot_enabled = os.environ.get(
        "PLUGBOARD_INTERNET_CHECK_ON_BOOT", "1"
    ).strip() not in ("0", "false", "no", "")

    async def _internet_boot_probe():
        # Small delay so warm-up + ollama sync settle first, and so a quick
        # restart loop doesn't immediately burn quota.
        await _asyncio.sleep(5.0)
        try:
            log.info("internet_check_on_boot: starting bulk probe (persist=True)")
            summary = await check_all_models_internet_access({"persist": True})
            log.info(
                "internet_check_on_boot: completed total=%s online=%s offline=%s failed=%s elapsed=%.1fs",
                summary.get("total"),
                summary.get("online_count"),
                summary.get("offline_count"),
                summary.get("failed_count"),
                summary.get("elapsed_s", 0.0),
            )
        except HTTPException as exc:
            # 409 if a manual bulk run is already in flight — non-fatal.
            log.warning(
                "internet_check_on_boot: skipped (HTTP %s: %s)",
                exc.status_code,
                exc.detail,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("internet_check_on_boot: failed: %s", exc)

    if _internet_boot_enabled:
        _spawn_boot_task(_internet_boot_probe(), "internet-boot-probe")
    else:
        log.info(
            "internet_check_on_boot: disabled via PLUGBOARD_INTERNET_CHECK_ON_BOOT=0"
        )

    yield

    await _model_availability_scheduler.stop()
    await _pricing_scheduler.stop()
    # Webhooks still being delivered get a short grace period.
    from plugboard import change_events

    try:
        await _asyncio.wait_for(change_events.drain(), timeout=10)
    except _asyncio.TimeoutError:
        log.warning("Shutdown: some change webhooks were not delivered")
    if _nag_task:
        _nag_task.cancel()
    for task in _boot_tasks:
        task.cancel()
    stop_watchdog()
    log.info("Provider watchdog stopped")
    # Close the shared HTTP pool so pooled sockets don't leak at shutdown.
    from plugboard.http_pool import close_session

    await close_session()
    log.info("HTTP pool closed")


app = FastAPI(
    title="Plugboard",
    description="Self-hosted LLM gateway -- one standard chat API, every provider behind it",
    version=__version__,
    docs_url=None,  # disable default /docs — we serve it auth-protected below
    redoc_url=None,  # disable default /redoc
    openapi_url=None,  # disable default /openapi.json
    lifespan=_lifespan,
)
app.include_router(decision_router)


# ── SEC-001 violation handler ───────────────────────────────────────────────
#
# Strict multi-tenant mode (ADR-032, default ON since v3.0.0) raises a
# ValueError when an internal caller reaches the cache layer without a valid
# client_id. Without an explicit handler this propagates as a bare Starlette
# 500 with no body, making operator and client diagnosis impossible (observed
# during 2026-05 model benchmarks: bench runner forgot to pass client_id and
# the symptom looked like an upstream provider failure on claude-opus/sonnet).
# Convert it to a structured 500 with X-Error-Detail so the cause is obvious.


@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError):
    from fastapi.responses import JSONResponse

    msg = str(exc)
    if "SEC-001" in msg or "empty client_id" in msg:
        log.error(
            "sec001_violation",
            extra={
                "endpoint": str(request.url.path),
                "http_method": request.method,
                "violation_detail": msg,
            },
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "error": {
                    "type": "sec001_violation",
                    "code": "SEC-001",
                    "message": msg,
                    "remediation": (
                        "Caller must propagate a valid client_id. "
                        "See ADR-032 multi-tenant strict mode."
                    ),
                }
            },
            headers={"X-Error-Detail": "sec001-missing-client-id"},
        )
    # Other ValueErrors fall back to default 500 to preserve existing behavior.
    return JSONResponse(
        status_code=500,
        content={"error": {"type": "value_error", "message": msg}},
    )


_MAX_BODY_BYTES = int(
    os.environ.get("MAX_BODY_BYTES", str(10 * 1024 * 1024))
)  # 10 MB default


# ── Inflight cap on /v*/chat/completions (TM-003) ───────────────────────────
#
# A burst of concurrent chat requests — each one possibly fanning out to a
# subprocess CLI and eating RAM — can DoS the instance before any per-IP or
# per-key limit kicks in. PLUGBOARD_MAX_INFLIGHT_CHAT (default 0 = off)
# enforces a hard global cap: once saturated, additional requests wait up
# to PLUGBOARD_INFLIGHT_WAIT_S for a slot and then get 503 + Retry-After.


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default)) or str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default)) or str(default)))
    except ValueError:
        return default


_INFLIGHT_CHAT_MAX = _env_int("PLUGBOARD_MAX_INFLIGHT_CHAT", 0)
_INFLIGHT_WAIT_S = _env_float("PLUGBOARD_INFLIGHT_WAIT_S", 0.0)
_inflight_chat_sem: Optional["_asyncio.Semaphore"] = None
_inflight_chat_active = 0  # observability counter; only accurate when sem is used


def _get_inflight_sem() -> Optional["_asyncio.Semaphore"]:
    """Lazily create the semaphore so it binds to the running event loop
    (prod uvicorn loop, pytest-asyncio loop, etc.) instead of whatever
    loop existed at import time."""
    global _inflight_chat_sem
    if _INFLIGHT_CHAT_MAX <= 0:
        return None
    if _inflight_chat_sem is None:
        _inflight_chat_sem = _asyncio.Semaphore(_INFLIGHT_CHAT_MAX)
    return _inflight_chat_sem


async def _inflight_chat_dep():
    """FastAPI dependency enforcing the global inflight cap.

    Uses the yield-style contract: code before `yield` runs before the
    handler, code after `yield` runs after the response body is fully
    delivered (including streaming chunks). That means a StreamingResponse
    keeps its slot for the entire stream duration — critical for DoS
    protection, since streaming is where the real resource cost lives.
    """
    sem = _get_inflight_sem()
    if sem is None:
        yield
        return
    wait_s = _INFLIGHT_WAIT_S
    try:
        # wait_for(timeout=0) races with the acquire; use a 1 ms floor so
        # a free slot is consistently grabbed while a saturated sem rejects.
        await _asyncio.wait_for(sem.acquire(), timeout=wait_s if wait_s > 0 else 0.001)
    except _asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Server overloaded: too many concurrent chat requests",
            headers={"Retry-After": "5"},
        )
    global _inflight_chat_active
    _inflight_chat_active += 1
    try:
        yield
    finally:
        _inflight_chat_active -= 1
        sem.release()


def _csp_for(nonce: str) -> str:
    """Build the CSP header for this request.

    Every response advertises a per-request ``'nonce-<value>'`` alongside
    ``'self'`` and the jsdelivr CDN. Pages that need inline bootstrap code
    (login, Swagger init, oauth-callback postMessage) stamp the same nonce
    onto their ``<script>`` tags; every other page has no inline scripts
    and the nonce just sits unused. No route uses ``'unsafe-inline'``.
    """
    return " ".join(
        [
            "default-src 'self';",
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net;",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;",
            "font-src 'self' https://fonts.gstatic.com;",
            "img-src 'self' data:;",
            "connect-src 'self';",
            "frame-ancestors 'none'",
        ]
    )


def _new_csp_nonce() -> str:
    """Per-request nonce. 16 bytes urlsafe-b64 -> 22 chars, 128 bits of entropy."""
    return secrets.token_urlsafe(16)


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        nonce = _new_csp_nonce()
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains"
        )
        response.headers["Content-Security-Policy"] = _csp_for(nonce)
        return response


class _BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds MAX_BODY_BYTES (default 10 MB).

    Trusts `Content-Length` as a fast reject path, and for chunked
    payloads (no Content-Length) wraps the ASGI receive callable so the
    counted bytes are enforced as the body streams in. This closes the
    gap where a client sends Transfer-Encoding: chunked without
    Content-Length and could otherwise exhaust memory.
    """

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > _MAX_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": (
                                f"Request body too large (max {_MAX_BODY_BYTES} bytes)"
                            )
                        },
                    )
            except ValueError:
                # Malformed Content-Length — let downstream reject.
                pass
        else:
            # No Content-Length → streaming/chunked. Wrap receive() so
            # we hard-cap the cumulative body bytes.
            original_receive = request.receive
            counter = {"n": 0}

            async def counting_receive():
                msg = await original_receive()
                if msg.get("type") == "http.request":
                    counter["n"] += len(msg.get("body") or b"")
                    if counter["n"] > _MAX_BODY_BYTES:
                        raise HTTPException(
                            413,
                            f"Request body too large (max {_MAX_BODY_BYTES} bytes)",
                        )
                return msg

            request._receive = counting_receive
        return await call_next(request)


class _RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = uuid.uuid4().hex[:16]
        token = _request_id_var.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            _request_id_var.reset(token)


class _RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ip = request.client.host if request.client else "unknown"
        if not check_rate_limit(ip):
            return JSONResponse(
                status_code=429, content={"detail": "Rate limit exceeded"}
            )
        return await call_next(request)


app.add_middleware(_SecurityHeadersMiddleware)
app.add_middleware(_BodySizeLimitMiddleware)
app.add_middleware(_RequestIdMiddleware)
app.add_middleware(_RateLimitMiddleware)
# Added last = outermost: 429/413 answers and preflights also get CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -- Auth ---------------------------------------------------------------------


def _effective_admin_key() -> str:
    return _ADMIN_KEY


def _is_admin_key(token: str) -> bool:
    effective_admin = _effective_admin_key()
    return bool(
        token and effective_admin and hmac.compare_digest(token, effective_admin)
    )


def _create_admin_session_token(mfa: bool = False) -> str:
    """Sign a new session and register it server-side (revocable).

    ``mfa`` records that a TOTP code was checked to open it.
    """
    effective_admin = _effective_admin_key()
    if not effective_admin:
        return ""
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(18)
    payload = f"{issued_at}.{nonce}"
    signature = hmac.new(
        effective_admin.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    from plugboard import admin_mfa as _mfa

    admin_sessions.register(
        nonce,
        issued_at,
        mfa=mfa,
        max_age=_ADMIN_SESSION_MAX_AGE,
        mfa_generation=_mfa.generation(),
    )
    return f"{payload}.{signature}"


def _admin_session_nonce(token: str) -> str:
    """The nonce of a correctly signed, unexpired session token, else ""."""
    effective_admin = _effective_admin_key()
    if not token or not effective_admin:
        return ""
    try:
        issued_at_raw, nonce, signature = token.split(".", 2)
        issued_at = int(issued_at_raw)
    except (ValueError, TypeError):
        return ""
    if not nonce or not signature:
        return ""

    payload = f"{issued_at_raw}.{nonce}"
    expected_signature = hmac.new(
        effective_admin.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        return ""

    now = int(time.time())
    if issued_at > now + 60 or now - issued_at > _ADMIN_SESSION_MAX_AGE:
        return ""
    return nonce


def _is_admin_session(token: str) -> bool:
    """Signed, unexpired, still registered, opened under the current MFA
    generation, and opened with TOTP when MFA is required (a session from
    before enforcement does not count)."""
    nonce = _admin_session_nonce(token)
    if not nonce:
        return False
    record = admin_sessions.lookup(nonce)
    if record is None:
        return False
    from plugboard import admin_mfa as _mfa

    if record.get("mfa_generation") != _mfa.generation():
        return False
    return bool(record.get("mfa")) or not _mfa.is_required()


def _should_use_secure_cookie(request: Request) -> bool:
    """Resolve cookie secure policy from env; default 'auto' secures non-local hosts."""
    if _COOKIE_SECURE_MODE in {"true", "1", "yes", "on"}:
        return True
    if _COOKIE_SECURE_MODE in {"false", "0", "no", "off"}:
        return False
    host = (request.url.hostname or "").lower()
    return host not in {"localhost", "127.0.0.1", "::1", "test"}


def _get_request_key_record(
    request: Request, client_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Resolve the key record backing this request.

    Priority: state.key_record (stashed by _resolve_client) → state.key_id
    lookup → client_id match (cheapest-first for a client with multiple keys).
    Stashes the result on request.state so downstream checks skip rediscovery.
    """
    rec = getattr(request.state, "key_record", None)
    if isinstance(rec, dict):
        return rec
    key_id = getattr(request.state, "key_id", "")
    if key_id and key_id != "admin":
        for k in load_keys():
            if k.get("key_id") == key_id:
                request.state.key_record = k
                return k
    if client_id and client_id != "admin":
        for k in load_keys():
            if k.get("client_id") == client_id and k.get("is_active", True):
                request.state.key_record = k
                return k
    return None


def _apply_key_record(request: Request, rec: Dict[str, Any], *, dev_mode: bool) -> str:
    """ARCH-DEBT-028 closed: stash key_id + key_record on request.state and
    log a structured success line. Was duplicated across the two lle_ paths
    in `_resolve_client`. Returns the resolved client_id."""
    request.state.key_id = rec.get("key_id", "")
    request.state.key_record = rec
    log.debug(
        "auth resolved client_id=%s via=lle_token dev_mode=%s key_id=%s",
        rec["client_id"],
        dev_mode,
        request.state.key_id,
    )
    return rec["client_id"]


def _resolve_client(request: Request) -> Optional[str]:
    """
    Resolve caller identity from bearer token.
    Returns client_id string, or None if auth is disabled.
    Also stores key_id in request.state for usage tracking.
    Raises 401 if token is invalid.

    ARCH-DEBT-030 (closes v2.9.1) : every resolution outcome is logged
    structurally — DEBUG on success, WARNING on rejection. Successful key
    resolution logs the nonsecret key_id; rejections use closed reason codes.
    Bearer contents, including prefixes, are never passed to the logger.
    """
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    request.state.key_id = ""
    request.state.key_record = None

    dev_mode = not _API_KEY

    # SEC-001 v2.8.4 MF3: an explicit lle_* bearer takes precedence over an
    # admin cookie session. Otherwise an admin user testing with a lle_* key
    # in the Playground would silently land in the shared "admin" cache
    # scope, leaking responses across admin users.
    bearer_is_lle = token.startswith(KEY_PREFIXES)

    # Admin cookie session (dashboard Playground) — treated as admin client.
    # Checked BEFORE the API_KEY gate so the admin Playground works even
    # when client auth is enforced. Skipped when a lle_* bearer is present
    # (explicit per-tenant intent wins).
    if not bearer_is_lle:
        session_token = request.cookies.get(_ADMIN_COOKIE_NAME, "")
        if _is_admin_session(session_token):
            request.state.key_id = "admin"
            log.debug(
                "auth resolved client_id=admin via=admin_cookie dev_mode=%s",
                dev_mode,
            )
            return "admin"
        # v3.0.0 BREAKING (ADR-033): the legacy `llm_admin_key` cookie that
        # carried the raw admin key in cleartext is no longer accepted.
        # Migrate to the modern session via POST /auth/admin/login → cookie
        # `plugboard_admin_session`.

    # Client auth disabled when API_KEY is empty (admin auth still enabled elsewhere).
    # SEC-001 v2.8.3: even in this mode, if a valid lle_* key is presented we
    # still resolve its client_id — otherwise multi-tenant cache isolation
    # collapses to "" for everyone in dev mode, recreating the v2.8.2 leak.
    # SEC-001 v2.8.4 MF2: presenting an lle_* prefix is an explicit auth
    # intent — an unknown / revoked / expired key must NOT silently degrade
    # to "anonymous". Reject with 401 so the caller learns the key is bad.
    if not _API_KEY:
        if _is_admin_key(token):
            request.state.key_id = "admin"
            log.debug("auth resolved client_id=admin via=admin_token dev_mode=True")
            return "admin"
        if token.startswith(KEY_PREFIXES):
            rec = get_key_record(token)
            if rec:
                return _apply_key_record(request, rec, dev_mode=True)
            log.warning("auth rejected reason=invalid_lle dev_mode=True")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
            )
        log.debug("auth resolved client_id=None via=anonymous dev_mode=True")
        return None

    # Admin key always valid (constant-time comparison)
    if _is_admin_key(token):
        request.state.key_id = "admin"
        log.debug("auth resolved client_id=admin via=admin_token dev_mode=False")
        return "admin"

    rec = get_key_record(token)
    if rec:
        return _apply_key_record(request, rec, dev_mode=False)

    log.warning("auth rejected reason=invalid_bearer dev_mode=False")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
    )


_ADMIN_TOTP_HEADER = "X-Plugboard-Admin-TOTP"


def _admin_request_auth(request: Request) -> str:
    """Admin authentication of a request: "ok", "denied", "mfa_required"
    (admin key without a valid TOTP header while MFA is required) or
    "locked" (too many wrong codes from this address)."""
    if _is_admin_session(request.cookies.get(_ADMIN_COOKIE_NAME, "")):
        return "ok"
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if not _is_admin_key(token):
        return "denied"
    from plugboard import admin_mfa as _mfa

    if not _mfa.is_required():
        return "ok"
    code = request.headers.get(_ADMIN_TOTP_HEADER, "").strip()
    if not code:
        return "mfa_required"
    from plugboard.ratelimit import check_auth_rate_limit, record_auth_failure

    client_ip = request.client.host if request.client else "unknown"
    if not check_auth_rate_limit(client_ip):
        return "locked"
    if _mfa.verify(code):
        return "ok"
    locked = record_auth_failure(client_ip)
    _audit(
        "auth.mfa_failed",
        details=f"header IP: {client_ip}, locked={locked}",
        ip=client_ip,
    )
    return "mfa_required"


def _require_admin(request: Request) -> None:
    """Dependency for admin-only endpoints.

    With MFA required the admin key alone is refused: use a session opened
    with TOTP, or send a fresh code in ``X-Plugboard-Admin-TOTP``.
    """
    # Auth fully disabled when BOTH keys are empty (dev/localhost mode)
    if not _API_KEY and not _ADMIN_KEY:
        return

    if not _effective_admin_key():
        raise HTTPException(status_code=503, detail="ADMIN_KEY is not configured")

    outcome = _admin_request_auth(request)
    if outcome == "ok":
        return
    if outcome == "locked":
        raise HTTPException(
            status_code=429, detail="Too many failed attempts. Try again later."
        )
    if outcome == "mfa_required":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Admin MFA required: sign in, or send {_ADMIN_TOTP_HEADER}",
        )

    # v3.0.0 BREAKING (ADR-033): legacy `llm_admin_key` cookie removed.
    # Use POST /auth/admin/login to get the modern session cookie.
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
    )


# -- Spending limit -----------------------------------------------------------


def _spending_scope(
    client_id: str, key_record: Optional[Dict[str, Any]] = None
) -> tuple:
    """(monthly_limit_usd, key_id) that applies to the caller; 0 = unlimited."""
    if key_record:
        return (
            float(key_record.get("monthly_limit_usd", 0) or 0),
            key_record.get("key_id", ""),
        )
    for k in load_keys():
        if k.get("client_id") == client_id and k.get("monthly_limit_usd", 0) > 0:
            return float(k["monthly_limit_usd"]), k.get("key_id", "")
    return 0.0, ""


def _check_spending_limit(
    client_id: str, key_record: Optional[Dict[str, Any]] = None
) -> float:
    """Raise 429 if caller exceeded their monthly spending limit. Returns current total spend."""
    limit, key_id = _spending_scope(client_id, key_record)
    if limit <= 0:
        return 0.0  # unlimited
    month = _billing.current_pricing_month()
    if key_id:
        total = _usage.get_total_cost_by_key(month, key_id)
    else:
        total = _usage.get_total_cost_by_client(month, client_id)
    if total >= limit:
        scope = f"key '{key_id}'" if key_id else f"client '{client_id}'"
        raise HTTPException(
            status_code=429,
            detail=f"Monthly spending limit exceeded for {scope} (${total:.4f} >= ${limit:.4f})",
        )
    return total


_UNPRICED_DETAIL = "Model price unavailable for this key"


def _check_price_known(
    client_id: str, model_str: str, key_record: Optional[Dict[str, Any]] = None
) -> None:
    """Raise 402 when a budgeted caller asks for a model without a known price.

    An unpriced call is recorded at 0, so monthly_limit_usd would never stop
    it. Every route forward() may try is checked, before any provider call.
    Callers without a monthly limit are not affected.
    """
    from plugboard.router import _resolve_all_providers

    limit, key_id = _spending_scope(client_id, key_record)
    if limit <= 0:
        return
    try:
        routes = _resolve_all_providers(model_str)
    except ValueError:
        return  # forward() stops on the same routing error, before any call
    month = _billing.current_pricing_month()
    for provider, real_model in routes:
        match = _billing.resolve_price(real_model, month, provider=provider)
        if _billing.budget_price_unknown(real_model, match):
            log.warning(
                "Refused unpriced model for budgeted %s: provider=%s model=%s",
                f"key '{key_id}'" if key_id else f"client '{client_id}'",
                provider.get("id", ""),
                real_model,
            )
            raise HTTPException(status_code=402, detail=_UNPRICED_DETAIL)


def _check_client_rpm(
    client_id: str, key_record: Optional[Dict[str, Any]] = None
) -> None:
    """Raise 429 when the caller's key exceeded its rpm_limit; counts one call."""
    rpm = 0
    if key_record:
        rpm = int(key_record.get("rpm_limit", 0) or 0)
        scope = key_record.get("key_id", client_id)
    else:
        scope = client_id
        for k in load_keys():
            if k.get("client_id") == client_id:
                rpm = int(k.get("rpm_limit", 0) or 0)
                break
    if rpm and not check_client_rate_limit(scope, rpm):
        raise HTTPException(429, f"Client rate limit exceeded ({rpm} req/min)")


# -- Streaming billing -------------------------------------------------------


def _bill_streaming(
    client_id: str, body: dict, raw_sse: bytes, key_id: str = ""
) -> Dict[str, Any]:
    """Record usage from accumulated SSE chunks and return the billing dict.

    Returns a dict with keys {prompt_tokens, completion_tokens, total_tokens,
    cost_usd, model, upstream_usage_present}. The caller (streaming
    generator) uses this to append a final OpenAI-compatible usage chunk
    when the upstream didn't provide one — so the chat dashboard can
    display tokens + cost for every provider, not just subprocess.
    """
    import json as _json

    content_parts = []
    upstream_usage: Dict[str, Any] = {}
    for line in raw_sse.decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            chunk = _json.loads(line[6:])
            delta = chunk.get("choices", [{}])[0].get("delta", {})
            if delta.get("content"):
                content_parts.append(delta["content"])
            # OpenAI stream_options.include_usage populates `usage` on the
            # last event; grab it so we don't re-emit a duplicate.
            if chunk.get("usage"):
                upstream_usage = chunk["usage"]
        except (ValueError, KeyError, IndexError, TypeError):
            continue

    completion_text = "".join(content_parts)
    # Prefer real upstream counts if present, fall back to estimates.
    prompt_tokens = upstream_usage.get("prompt_tokens") or estimate_prompt_tokens(
        body.get("messages", [])
    )
    completion_tokens = upstream_usage.get("completion_tokens") or (
        count_tokens(completion_text) if completion_text else 0
    )

    provider, real_model = _billing_target(body)
    pricing_month = _billing.current_pricing_month()
    match = _billing.resolve_price(real_model, pricing_month, provider=provider)
    cost = _billing.calculate_cost(
        prompt_tokens, completion_tokens, match.entry if match else None
    )
    _usage.append_usage(
        client_id=client_id,
        model=real_model,
        provider_id=provider["id"],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        pricing_month=pricing_month,
        cost_usd=cost,
        key_id=key_id,
        price_key=match.price_key if match else "",
        price_source=match.source if match else "",
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": round(cost, 6),
        "model": real_model,
        "upstream_usage_present": bool(upstream_usage),
    }


def _bill_completion(
    client_id: str, key_id: str, body: Dict[str, Any], result: Dict[str, Any]
) -> Dict[str, Any]:
    """Record the usage of a non-streamed completion; return the billing dict."""
    provider, real_model = _billing_target(body)
    usage_data = result.get("usage") or {}
    prompt_tokens = usage_data.get("prompt_tokens", 0)
    completion_tokens = usage_data.get("completion_tokens", 0)

    # Estimate tokens if backend returned 0
    if prompt_tokens == 0:
        prompt_tokens = estimate_prompt_tokens(body.get("messages", []))
    if completion_tokens == 0:
        completion_tokens = estimate_completion_tokens(result)

    pricing_month = _billing.current_pricing_month()
    match = _billing.resolve_price(real_model, pricing_month, provider=provider)
    cost = _billing.calculate_cost(
        prompt_tokens, completion_tokens, match.entry if match else None
    )
    _usage.append_usage(
        client_id=client_id,
        model=real_model,
        provider_id=provider["id"],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        pricing_month=pricing_month,
        cost_usd=cost,
        key_id=key_id,
        price_key=match.price_key if match else "",
        price_source=match.source if match else "",
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd": cost,
        "model": real_model,
        "pricing_month": pricing_month,
        "price_key": match.price_key if match else "",
    }


def _billing_target(body: Dict[str, Any]) -> tuple:
    """(provider, real_model) to bill: the one that served the call.

    forward() may fall back to another provider or rewrite the model through
    the circuit breaker; router.served_by() records what actually answered.
    """
    served = _served_by()
    if served is not None:
        return served
    return resolve_provider_and_model(body.get("model", ""))


# -- Model access check -----------------------------------------------------


def _check_model_access(
    client_id: str,
    model_str: str,
    key_record: Optional[Dict[str, Any]] = None,
) -> None:
    """Raise 403 unless the resolved effective identity is allowed."""
    guard = _model_route_guard(client_id, key_record)
    if guard is not None:
        guard(*resolve_provider_and_model(model_str))


def _model_route_guard(client_id, key_record=None):
    """Snapshot the calling key's policy, never request-body state."""
    if not client_id or client_id == "admin":
        return None
    record = key_record
    if record is None:
        record = next((k for k in load_keys() if k.get("client_id") == client_id), None)
    allowed = tuple((record or {}).get("allowed_models", []))
    if not allowed:
        return None

    def guard(provider, native):
        from plugboard.model_identity import model_access_matches

        if not model_access_matches(allowed, provider["id"], native):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Model '{provider['id']}:{native}' not allowed for "
                    f"client '{client_id}'. Allowed: {list(allowed)}"
                ),
            )

    return guard


def _model_is_compatible(
    model_str: str,
    client_id: str,
    key_record: Optional[Dict[str, Any]] = None,
) -> bool:
    """Use chat's effective-route policy for catalogue compatibility."""
    guard = _model_route_guard(client_id, key_record)
    if guard is None:
        return True
    try:
        guard(*resolve_provider_and_model(model_str))
    except (HTTPException, ValueError):
        return False
    return True


# -- Spending alert helper ---------------------------------------------------


def _send_spending_alert(
    client_id: str,
    pricing_month: str,
    key_record: Optional[Dict[str, Any]] = None,
    current_total_usd: Optional[float] = None,
) -> None:
    """Fire-and-forget: check if client needs a spending alert."""
    import asyncio

    limit = 0.0
    key_id = ""
    if key_record:
        key_id = key_record.get("key_id", "")
        limit = float(key_record.get("monthly_limit_usd", 0) or 0)
    else:
        for k in load_keys():
            if k.get("client_id") == client_id and k.get("monthly_limit_usd", 0) > 0:
                key_id = k.get("key_id", "")
                limit = float(k["monthly_limit_usd"])
                break

    if limit <= 0:
        return
    if current_total_usd is not None:
        total = current_total_usd
    elif key_id:
        total = _usage.get_total_cost_by_key(pricing_month, key_id)
    else:
        total = _usage.get_total_cost_by_client(pricing_month, client_id)
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_check_and_alert(client_id, total, limit))
    except Exception as exc:
        log.debug("Failed to schedule spending alert check for %s: %s", client_id, exc)


# -- /v1/chat/completions ----------------------------------------------------


def _for_caller(completion: Dict[str, Any], client_id: Optional[str]) -> Dict[str, Any]:
    """The completion as the caller may see it.

    ``billing.price_key`` names the grid entry that priced the call, which
    reveals provider routing: admins see it (like ``X-Price-Key`` and the
    entries of ``/usage/me``), client keys do not. Applied to every answer,
    including those served from the cache.
    """
    bill = completion.get("billing")
    if client_id == "admin" or not isinstance(bill, dict) or "price_key" not in bill:
        return completion
    return {
        **completion,
        "billing": {k: v for k, v in bill.items() if k != "price_key"},
    }


@app.post("/v1/chat/completions", dependencies=[Depends(_inflight_chat_dep)])
async def chat_completions(
    request: Request,
    client_id: Optional[str] = Depends(_resolve_client),
):
    # SEC-001 (v3.0.0): strict tenant mode requires a valid client_id for any
    # cache-scoped endpoint. Anonymous requests would crash the cache layer with
    # ValueError and leak as a bare 500. Gate early so callers get 401/403.
    if not client_id and _strict_tenant_mode():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: missing client_id.",
        )

    # ARCH-DEBT-026 closed (opt-in): track request start so a cache HIT can
    # be artificially padded to the configured floor. Eliminates the timing
    # side-channel where 5ms HIT vs 500ms MISS leaks "this prompt is
    # cached" to a co-located attacker. Off by default (floor=0) since the
    # leak requires LAN-level adversary; opt-in for high-sensitivity
    # deployments via PLUGBOARD_CACHE_RESPONSE_FLOOR_MS.
    _request_t0 = time.perf_counter()
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    if not isinstance(body.get("model", ""), str):
        raise HTTPException(400, "model must be a string")
    providers = load_providers()
    try:
        require_supported_model(
            body.get("model", ""), provider_ids=(p["id"] for p in providers)
        )
        for provider in sorted(providers, key=lambda p: len(p["id"]), reverse=True):
            if body.get("model", "").startswith(provider["id"] + ":"):
                require_supported_provider(provider)
                break
        # Validate the effective default/dated alias before a cached answer can
        # bypass routing. Other admission errors keep their existing handling.
        try:
            resolve_provider_and_model(body.get("model", ""))
        except ValueError as exc:
            if str(exc) == REMOVED_CONNECTOR_ERROR:
                raise
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if is_decision_model(
        body.get("model", ""), provider_ids=(p["id"] for p in load_providers())
    ):
        raise HTTPException(400, "Use /v1/decisions for decision models")
    # Pre-resolve once: downstream spending/model/rpm checks all receive
    # the same record and skip their own load_keys() fallback scans.
    key_record = _get_request_key_record(request, client_id=client_id)
    spending_total = 0.0

    # PERF-1: opt-in server-side session continuity.
    # Client passes a session id via header X-LLM-Session-ID or body.session_id;
    # we prepend previously stored history so they only need to send the new
    # turn. Anonymous clients can't use sessions (no owner → no isolation).
    session_id: Optional[str] = (
        request.headers.get("X-LLM-Session-ID")
        or request.headers.get("x-llm-session-id")
        or body.pop("session_id", None)
    )
    if session_id and client_id:
        from plugboard.session_store import merge_history_into_body

        body = merge_history_into_body(client_id, session_id, body)

    # Issue #5: check spending limit before processing request
    if client_id and client_id != "admin":
        spending_total = _check_spending_limit(client_id, key_record=key_record)

    # Check model access (per-key allowed_models)
    route_guard = _model_route_guard(client_id, key_record)
    route_options = {}
    if route_guard is not None:
        route_guard(*resolve_provider_and_model(body.get("model", "")))
        route_options["route_guard"] = route_guard

    # A budget cannot hold a call whose price is unknown (402, nothing called)
    if client_id and client_id != "admin":
        _check_price_known(client_id, body.get("model", ""), key_record=key_record)

    # Per-client rate limit (rpm_limit on key record)
    if client_id and client_id != "admin":
        _check_client_rpm(client_id, key_record=key_record)

    # Response cache — disabled only when caller sets cache=false.
    # For streaming requests we still check the cache; on HIT we replay the
    # cached response as SSE chunks so the Playground still "streams" but
    # returns instantly. Only the WRITE-side of streaming is skipped (we
    # don't cache streamed responses yet — they're replayed from the
    # non-stream cache when the same prompt was asked non-stream first).
    _cache_enabled = body.pop("cache", True) is not False
    _is_streaming = body.get("stream") is True
    # Historical cache entries do not bind the effective serving identity.
    # Restricted keys must not reuse responses from another route or policy.
    _use_cache = _cache_enabled and route_guard is None
    _cache_key = None
    if _use_cache:
        from plugboard.cache import compute_cache_key, get_cached

        _cache_key = compute_cache_key(
            body.get("model", ""),
            body.get("messages", []),
            client_id=client_id or "",
            temperature=body.get("temperature"),
            max_tokens=body.get("max_tokens"),
            top_p=body.get("top_p"),
            stop=body.get("stop"),
        )
        cached = get_cached(_cache_key, client_id=client_id or "")
        if not cached and not _is_streaming:
            # Exact miss → try semantic (paraphrase) match. Off by default;
            # enable via PLUGBOARD_SEMANTIC_CACHE=1. Phase 2 (embedding-
            # based cosine fallback) requires PLUGBOARD_SEMANTIC_EMBED=1
            # plus EMBED_PROVIDER_ID + EMBED_MODEL. Streams not supported
            # in this phase (would need SSE replay like the subprocess path).
            try:
                from plugboard import semantic_cache as _sem

                if _sem.is_enabled():
                    sem_cached = await _sem.lookup_similar_async(
                        body.get("model", ""),
                        body.get("messages", []),
                        client_id=client_id or "",
                    )
                    if sem_cached:
                        meta = sem_cached.get("_cache") or {}
                        sim = meta.get("similarity")
                        layer = "EMBED" if meta.get("embedding") else "MINHASH"
                        log.info(
                            "Semantic Cache HIT | layer=%s | model=%s | sim=%s | client_id=%s",
                            layer,
                            body.get("model", "?"),
                            sim,
                            client_id or "-",
                        )
                        resp = JSONResponse(content=_for_caller(sem_cached, client_id))
                        resp.headers["X-Cache"] = f"SEMANTIC-HIT-{layer}"
                        if sim is not None:
                            resp.headers["X-Cache-Similarity"] = str(sim)
                        await _pad_cache_hit_response(_request_t0)  # ARCH-DEBT-026
                        return resp
            except Exception as exc:
                log.warning("Semantic cache lookup error (non-fatal): %s", exc)
        if cached:
            log.info(
                "Cache HIT | model=%s | stream=%s | client_id=%s",
                body.get("model", "?"),
                _is_streaming,
                client_id or "-",
            )
            if not _is_streaming:
                resp = JSONResponse(content=_for_caller(cached, client_id))
                resp.headers["X-Cache"] = "HIT"
                await _pad_cache_hit_response(_request_t0)  # ARCH-DEBT-026
                return resp
            # Stream replay from cache ONLY for subprocess providers — HTTP
            # providers are expected to stream live tokens from their backend.
            try:
                _replay_provider, _ = resolve_provider_and_model(body.get("model", ""))
                _replay_allowed = str(
                    _replay_provider.get("auth_source", "")
                ).startswith("subprocess:")
            except Exception:
                _replay_allowed = False
            if not _replay_allowed:
                # Skip the cache on stream requests for non-subprocess providers —
                # fall through to the live streaming path below.
                _cache_key = None
            else:
                # Replay the cached non-stream response as SSE chunks.
                pass  # fall into the replay block
        if cached and _cache_key is not None and _is_streaming:
            # (subprocess-only replay path)
            import json as _json_mod

            content = (
                (cached.get("choices") or [{}])[0].get("message", {}).get("content", "")
            )
            usage = cached.get("usage") or {}
            completion_id = cached.get("id", "chatcmpl-cache")
            created = cached.get("created", int(time.time()))
            model_name = cached.get("model", body.get("model", ""))

            async def _replay_cached_as_stream():
                tokens = content.split(" ")
                for i, tok in enumerate(tokens):
                    delta = tok + (" " if i < len(tokens) - 1 else "")
                    evt = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {_json_mod.dumps(evt)}\n\n".encode("utf-8")
                final_evt = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": usage,
                }
                yield f"data: {_json_mod.dumps(final_evt)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                _replay_cached_as_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Cache": "HIT",
                },
            )

    if body.get("stream") is True:
        # Fast path: subprocess providers execute synchronously (CLI returns
        # the full text), so we can call forward() to populate the non-stream
        # cache AND replay the result as SSE. Future identical requests will
        # hit the cache instantly (see the "Cache HIT + stream=true" branch
        # above) — saving ~10s per repeat query.
        try:
            _provider_for_stream, _ = resolve_provider_and_model(body.get("model", ""))
            _stream_bypass = str(
                _provider_for_stream.get("auth_source", "")
            ).startswith("subprocess:")
        except Exception:
            _stream_bypass = False

        if _stream_bypass:
            try:
                result = await forward({**body, "stream": False}, **route_options)
            except HTTPException:
                raise
            except Exception as exc:
                log.error("subprocess stream forward error: %s", exc)
                err_str = str(exc)
                if "timeout" in err_str.lower():
                    return JSONResponse(
                        status_code=503,
                        content={"detail": err_str, "retry_after": 10},
                        headers={"Retry-After": "10"},
                    )
                if isinstance(exc, AuthError):
                    raise HTTPException(status_code=exc.status_code, detail=err_str)
                if isinstance(exc, CliSubprocessError):
                    return JSONResponse(
                        status_code=502,
                        content={
                            "detail": err_str,
                            "exit_code": exc.exit_code,
                            "stderr": exc.stderr,
                            "pid": exc.pid,
                            "provider_name": exc.provider_name,
                            "model_id": exc.model_id,
                        },
                    )
                raise HTTPException(status_code=502, detail=err_str)

            # Billing + cache (mirror non-stream path)
            _billing_for_cache = {}
            try:
                if client_id:
                    provider, real_model = _billing_target(body)
                    usage_data = result.get("usage") or {}
                    prompt_tokens = usage_data.get(
                        "prompt_tokens", 0
                    ) or estimate_prompt_tokens(body.get("messages", []))
                    completion_tokens = usage_data.get(
                        "completion_tokens", 0
                    ) or estimate_completion_tokens(result)
                    pricing_month = _billing.current_pricing_month()
                    match = _billing.resolve_price(
                        real_model, pricing_month, provider=provider
                    )
                    cost = _billing.calculate_cost(
                        prompt_tokens, completion_tokens, match.entry if match else None
                    )
                    _key_id = getattr(request.state, "key_id", "")
                    _usage.append_usage(
                        client_id=client_id,
                        model=real_model,
                        provider_id=provider["id"],
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        pricing_month=pricing_month,
                        cost_usd=cost,
                        key_id=_key_id,
                        price_key=match.price_key if match else "",
                        price_source=match.source if match else "",
                    )
                    _billing_for_cache = {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "cost_usd": cost,
                        "model": real_model,
                        "pricing_month": pricing_month,
                    }
                    result["billing"] = _billing_for_cache
                    # Ensure usage in result matches billing (so cache replay emits
                    # the same numbers).
                    result["usage"] = {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "cost_usd": round(cost, 6),
                    }
            except Exception as exc:
                log.warning("subprocess stream billing error (non-fatal): %s", exc)

            # Write to cache (same cache the non-stream branch uses, so future
            # requests — stream or not — hit instantly).
            if _use_cache and _cache_key:
                try:
                    from plugboard.cache import set_cached

                    _cost = _billing_for_cache.get("cost_usd", 0)
                    set_cached(
                        _cache_key,
                        body.get("model", ""),
                        _cache_key[:16],
                        result,
                        cost_usd=_cost,
                        client_id=client_id or "",
                    )
                    try:
                        from plugboard import semantic_cache as _sem

                        await _sem.store_async(
                            body.get("model", ""),
                            body.get("messages", []),
                            result,
                            cost_usd=_cost,
                            client_id=client_id or "",
                        )
                    except Exception as exc:
                        log.warning("Semantic cache write error (non-fatal): %s", exc)
                except Exception as exc:
                    log.warning("Cache write error (non-fatal): %s", exc)

            # Replay as SSE
            import json as _json_mod

            content = (result.get("choices") or [{}])[0].get("message", {}).get(
                "content", ""
            ) or ""
            usage = result.get("usage") or {}
            completion_id = result.get("id", "chatcmpl-subproc")
            created = result.get("created", int(time.time()))
            model_name = result.get("model", body.get("model", ""))

            async def _subproc_stream():
                tokens = content.split(" ")
                for i, tok in enumerate(tokens):
                    delta = tok + (" " if i < len(tokens) - 1 else "")
                    evt = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {_json_mod.dumps(evt)}\n\n".encode("utf-8")
                final_evt = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": usage,
                }
                yield f"data: {_json_mod.dumps(final_evt)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                _subproc_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Cache": "MISS",
                },
            )

        try:
            gen = forward_stream(body, **route_options)
            first = await gen.__anext__()
        except StopAsyncIteration:
            return StreamingResponse(
                iter([]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        except HTTPException:
            raise
        except Exception as exc:
            log.error("stream error: %s", exc)
            err_str = str(exc)
            if "timeout" in err_str.lower():
                return JSONResponse(
                    status_code=503,
                    content={"detail": err_str, "retry_after": 10},
                    headers={"Retry-After": "10"},
                )
            if isinstance(exc, AuthError):
                raise HTTPException(status_code=exc.status_code, detail=err_str)
            raise HTTPException(status_code=502, detail=err_str)

        _key_id = getattr(request.state, "key_id", "")

        async def _stream_and_bill(first_chunk: bytes, rest, cid, req_body, kid):
            """Yield chunks in real-time and append a final usage chunk.

            OpenAI's `stream_options.include_usage` is honoured by OpenAI
            and GitHub Models but NOT by Ollama or most other providers.
            Without this injection, the Playground
            never sees tokens/cost for those upstreams.

            We must preserve the real-time streaming UX — so we yield each
            chunk as it arrives and only buffer enough state at the end
            (the tail around [DONE]) to splice the usage chunk in.
            """
            import json as _json_mod

            accumulated = bytearray()
            # 1) First chunk is already consumed by the caller's probe —
            # accumulate and yield immediately. A short answer can arrive
            # whole in this one read: then `[DONE]` is held back with the
            # tail, or the usage chunk would land after it and OpenAI
            # clients, which stop reading at `[DONE]`, would never see it.
            accumulated.extend(first_chunk)
            done_in_first = first_chunk.rfind(b"data: [DONE]")
            if done_in_first == -1:
                yield first_chunk
            elif done_in_first:
                yield first_chunk[:done_in_first]
            # 2) Keep the very last bytes buffered so we can detect `[DONE]`.
            # Most upstreams end with `data: [DONE]\n\n` — 16 bytes is enough
            # to disambiguate. We buffer the trailing window only.
            TAIL = 64
            tail_buf = bytearray(
                first_chunk[done_in_first:] if done_in_first != -1 else b""
            )
            async for chunk in rest:
                accumulated.extend(chunk)
                tail_buf.extend(chunk)
                if len(tail_buf) > TAIL:
                    overflow = bytes(tail_buf[:-TAIL])
                    yield overflow
                    del tail_buf[:-TAIL]
            # At this point `tail_buf` holds the last <=TAIL bytes and has
            # NOT been yielded yet.

            bill = None
            if cid:
                try:
                    bill = _bill_streaming(
                        cid, req_body, bytes(accumulated), key_id=kid
                    )
                except Exception as exc:
                    log.warning("Stream billing error (non-fatal): %s", exc)

            usage_needed = bill is not None and not bill["upstream_usage_present"]
            if not usage_needed:
                if tail_buf:
                    yield bytes(tail_buf)
                return

            usage_evt = {
                "id": "chatcmpl-injected",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": bill["model"],
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": bill["prompt_tokens"],
                    "completion_tokens": bill["completion_tokens"],
                    "total_tokens": bill["total_tokens"],
                    "cost_usd": bill["cost_usd"],
                },
            }
            usage_sse = f"data: {_json_mod.dumps(usage_evt)}\n\n".encode("utf-8")

            done_idx = tail_buf.rfind(b"data: [DONE]")
            if done_idx == -1:
                if tail_buf:
                    yield bytes(tail_buf)
                yield usage_sse
            else:
                yield bytes(tail_buf[:done_idx])
                yield usage_sse
                yield bytes(tail_buf[done_idx:])

        return StreamingResponse(
            _stream_and_bill(first, gen, client_id, body, _key_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    t0 = time.perf_counter()
    try:
        result = await forward(body, **route_options)
    except HTTPException:
        raise
    except Exception as exc:
        log.error("forward error: %s", exc)
        err_str = str(exc)
        # 503 + Retry-After for timeouts (client can retry), 502 for other errors
        if "timeout" in err_str.lower():
            return JSONResponse(
                status_code=503,
                content={"detail": err_str, "retry_after": 10},
                headers={"Retry-After": "10"},
            )
        if isinstance(exc, AuthError):
            raise HTTPException(status_code=exc.status_code, detail=err_str)
        if isinstance(exc, CliSubprocessError):
            return JSONResponse(
                status_code=502,
                content={
                    "detail": err_str,
                    "exit_code": exc.exit_code,
                    "stderr": exc.stderr,
                    "pid": exc.pid,
                    "provider_name": exc.provider_name,
                    "model_id": exc.model_id,
                },
            )
        raise HTTPException(status_code=502, detail=err_str)
    elapsed = time.perf_counter() - t0
    log.info("<- OK | model=%s | %.2fs", body.get("model", "?"), elapsed)

    # -- Billing --------------------------------------------------------------
    billing_info = {}
    try:
        if client_id:
            billing_info = _bill_completion(
                client_id, getattr(request.state, "key_id", ""), body, result
            )
            # Spending alert (non-blocking)
            if client_id != "admin":
                _send_spending_alert(
                    client_id,
                    billing_info["pricing_month"],
                    key_record=key_record,
                    current_total_usd=spending_total + billing_info["cost_usd"],
                )
    except Exception as exc:
        log.warning("Billing error (non-fatal): %s", exc)

    # Inject billing into response + headers
    if billing_info:
        result["billing"] = billing_info

    # PERF-1: record the exchange so the next call with the same session_id
    # sees the updated transcript. Non-blocking — never bubble errors.
    if session_id and client_id:
        try:
            from plugboard.session_store import record_exchange

            assistant_text = ""
            try:
                choice = (result.get("choices") or [{}])[0]
                assistant_text = (choice.get("message") or {}).get("content") or ""
            except (IndexError, TypeError):
                pass
            record_exchange(
                client_id,
                session_id,
                list(body.get("messages", []) or []),
                assistant_text,
            )
        except Exception as exc:
            log.warning("session record error (non-fatal): %s", exc)

    # Cache the response
    if _use_cache and _cache_key:
        try:
            from plugboard.cache import set_cached

            cost = billing_info.get("cost_usd", 0) if billing_info else 0
            set_cached(
                _cache_key,
                body.get("model", ""),
                _cache_key[:16],
                result,
                cost_usd=cost,
                client_id=client_id or "",
            )
            try:
                from plugboard import semantic_cache as _sem

                await _sem.store_async(
                    body.get("model", ""),
                    body.get("messages", []),
                    result,
                    cost_usd=cost,
                    client_id=client_id or "",
                )
            except Exception as exc:
                log.warning("Semantic cache write error (non-fatal): %s", exc)
        except Exception as exc:
            log.warning("Cache write error (non-fatal): %s", exc)

    resp = JSONResponse(content=_for_caller(result, client_id))
    resp.headers["X-Cache"] = "MISS"
    if billing_info:
        resp.headers["X-Cost-USD"] = str(billing_info["cost_usd"])
        resp.headers["X-Prompt-Tokens"] = str(billing_info["prompt_tokens"])
        resp.headers["X-Completion-Tokens"] = str(billing_info["completion_tokens"])
        # Which grid entry priced the call: useful to admins, not shown to
        # client keys (it reveals provider routing).
        if client_id == "admin" and billing_info.get("price_key"):
            resp.headers["X-Price-Key"] = billing_info["price_key"]
    return resp


# -- /v1/models --------------------------------------------------------------


@app.get("/v1/models")
async def models(request: Request, client_id: Optional[str] = Depends(_resolve_client)):
    from plugboard.legacy_connectors import is_removed_catalogue_model
    from plugboard.model_identity import catalogue_native_model

    items = await list_models()
    provider_ids = tuple(p["id"] for p in load_providers())
    items = [
        item
        for item in items
        if not is_decision_model(
            catalogue_native_model(item, provider_ids=provider_ids), effective_name=True
        )
        and not is_removed_catalogue_model(item)
    ]
    key_record = _get_request_key_record(request, client_id=client_id)
    for item in items:
        item["account_compatible"] = _model_is_compatible(
            item.get("id", ""), client_id or "", key_record=key_record
        )
    return {"object": "list", "data": items}


class _PreflightItem(BaseModel):
    provider: str
    model: str


_PREFLIGHT_MAX_ITEMS = 10


class _PreflightRequest(BaseModel):
    items: List[_PreflightItem] = Field(max_length=_PREFLIGHT_MAX_ITEMS)


def _preflight_refusal(
    client_id: str, model_str: str, key_record: Optional[Dict[str, Any]]
) -> Optional[str]:
    """Closed error code of the first chat admission check the item fails."""
    checks = (
        ("budget_exceeded", lambda: _check_spending_limit(client_id, key_record)),
        (
            "forbidden_model",
            lambda: _check_model_access(client_id, model_str, key_record=key_record),
        ),
        (
            "price_unavailable",
            lambda: _check_price_known(client_id, model_str, key_record),
        ),
        ("rate_limited", lambda: _check_client_rpm(client_id, key_record)),
    )
    for code, check in checks:
        try:
            check()
        except HTTPException:
            return code
    return None


@app.post("/v1/preflight", dependencies=[Depends(_inflight_chat_dep)])
async def preflight(
    request: Request,
    body: _PreflightRequest,
    client_id: Optional[str] = Depends(_resolve_client),
):
    """Smoke-test a batch of provider+model pairs before a long benchmark.

    Each ping goes through the chat admission checks (allowed_models, budget,
    known price, rpm; the inflight cap holds for the whole batch) and is billed
    like a chat call. Errors are closed codes, never the provider's text.
    """
    key_record = _get_request_key_record(request, client_id=client_id)
    key_id = getattr(request.state, "key_id", "")
    route_guard = _model_route_guard(client_id, key_record)
    route_options = {}
    if route_guard is not None:
        route_options["route_guard"] = route_guard
    results: List[Dict[str, Any]] = []
    for item in body.items:
        t0 = time.perf_counter()
        model_str = f"{item.provider}:{item.model}"
        error: Optional[str] = None
        try:
            resolve_provider_and_model(model_str)
        except Exception:
            error = "unknown_model"
        if error is None and client_id and client_id != "admin":
            error = _preflight_refusal(client_id, model_str, key_record)
        if error is None:
            # Minimal ping to verify the pipe is open
            ping_body = {
                "model": model_str,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
            try:
                result = await forward(ping_body, timeout=10.0, **route_options)
            except HTTPException:
                error = "forbidden_model"
            except Exception as exc:
                log.warning("preflight error | model=%s | %s", model_str, exc)
                error = "upstream_error"
            else:
                if client_id:
                    try:
                        _bill_completion(client_id, key_id, ping_body, result)
                    except Exception as exc:
                        log.warning("Preflight billing error (non-fatal): %s", exc)
        entry: Dict[str, Any] = {
            "provider": item.provider,
            "model": item.model,
            "ok": error is None,
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        }
        if error is not None:
            entry["error"] = error
        results.append(entry)
    return {"results": results}


@app.get("/v1/models/benchmarks")
async def models_with_benchmarks(client_id: Optional[str] = Depends(_resolve_client)):
    """List all models enriched with benchmarks, capabilities, and metadata."""
    from plugboard.benchmarks import enrich_models_list

    items = await list_models()
    return {"object": "list", "data": enrich_models_list(items)}


@app.get("/v1/models/{model_id:path}/info")
async def model_info(
    model_id: str, client_id: Optional[str] = Depends(_resolve_client)
):
    """Get detailed info for a specific model: benchmarks, capabilities, best_for."""
    from plugboard.benchmarks import get_model_info

    info = get_model_info(model_id)
    if not info:
        raise HTTPException(404, f"No benchmark data for model '{model_id}'")
    return info


# -- /v2/ aliases (API versioning) -------------------------------------------
# v2 endpoints are identical to v1 for now. The version header allows
# clients to opt-in to future breaking changes.


@app.post("/v2/chat/completions", dependencies=[Depends(_inflight_chat_dep)])
async def chat_completions_v2(
    request: Request, client_id: Optional[str] = Depends(_resolve_client)
):
    resp = await chat_completions(request, client_id)
    resp.headers["X-API-Version"] = "2"
    return resp


@app.get("/v2/models")
async def models_v2(client_id: Optional[str] = Depends(_resolve_client)):
    items = await list_models()
    return {"object": "list", "data": items, "api_version": "2"}


@app.get("/v2/models/benchmarks")
async def models_benchmarks_v2(client_id: Optional[str] = Depends(_resolve_client)):
    from plugboard.benchmarks import enrich_models_list

    items = await list_models()
    return {"object": "list", "data": enrich_models_list(items), "api_version": "2"}


# -- /health -----------------------------------------------------------------


@app.get("/health")
async def health():
    providers = load_providers()
    active = [
        p["id"]
        for p in providers
        if p.get("is_active", True) and not is_removed_provider(p)
    ]
    payload = {
        "ok": True,
        "version": __version__,
        "providers": len(active),
        "active": active,
    }
    # Surface auth posture so load balancers / deploy gates can refuse
    # traffic to an instance running with open client auth (TM-007).
    if not _API_KEY:
        payload["auth_disabled"] = True
        payload["dev_mode"] = _DEV_MODE
    return payload


@app.get("/health/detailed")
async def health_detailed(_: None = Depends(_require_admin)):
    """Detailed health check — tests connectivity to all active providers."""
    import aiohttp

    providers = load_providers()
    results = []
    all_ok = True
    for p in providers:
        if not p.get("is_active", True):
            continue
        try:
            require_supported_provider(p)
            require_supported_model(p.get("default_model", ""), effective_name=True)
        except ValueError as exc:
            results.append({"provider": p["id"], "ok": False, "error": str(exc)})
            all_ok = False
            continue
        url = p["base_url"].rstrip("/") + "/health"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    ok = resp.status < 500
                    results.append(
                        {"provider": p["id"], "ok": ok, "status": resp.status}
                    )
                    if not ok:
                        all_ok = False
        except Exception as exc:
            results.append({"provider": p["id"], "ok": False, "error": str(exc)})
            all_ok = False
    month = _billing.current_pricing_month()
    usage_summary = _usage.summarize_usage_month(month)
    return {
        "ok": all_ok,
        "version": __version__,
        "providers": results,
        "usage_month": month,
        "usage_entries": usage_summary["total_entries"],
        "keys_count": len(load_keys()),
    }


# -- /usage/me ---------------------------------------------------------------


@app.get("/usage/me")
async def usage_me(
    month: Optional[str] = None,
    client_id: Optional[str] = Depends(_resolve_client),
):
    if client_id is None:
        return {"entries": [], "summary": {}}
    m = month or _billing.current_pricing_month()
    # Which grid entry priced a call reveals provider routing: admin only.
    entries = [
        {k: v for k, v in e.items() if k not in ("price_key", "price_source")}
        for e in _usage.get_usage_by_client(m, client_id)
    ]
    return {
        "month": m,
        "client_id": client_id,
        "entries": entries,
        "summary": _usage.summarize_usage(entries),
    }


# -- /v1/me -----------------------------------------------------------------


@app.get("/v1/me")
async def client_info(
    request: Request, client_id: Optional[str] = Depends(_resolve_client)
):
    if not client_id:
        return {"client_id": None}
    info: Dict[str, Any] = {"client_id": client_id}
    key_record = _get_request_key_record(request)
    if key_record:
        info["key_id"] = key_record.get("key_id", "")
        info["monthly_limit_usd"] = key_record.get("monthly_limit_usd", 0)
        info["allowed_models"] = key_record.get("allowed_models", [])
        info["rpm_limit"] = key_record.get("rpm_limit", 0)
        info["org_id"] = key_record.get("org_id", "")
    else:
        for k in load_keys():
            if k.get("client_id") == client_id:
                info["monthly_limit_usd"] = k.get("monthly_limit_usd", 0)
                info["allowed_models"] = k.get("allowed_models", [])
                info["rpm_limit"] = k.get("rpm_limit", 0)
                info["org_id"] = k.get("org_id", "")
                break

    month = _billing.current_pricing_month()
    key_id = info.get("key_id", "")
    if key_id:
        summary = _usage.summarize_usage_by_key(month, key_id)
    else:
        summary = _usage.summarize_usage_by_client(month, client_id)
    info["current_month"] = month
    info["usage"] = summary
    limit = info.get("monthly_limit_usd", 0)
    info["remaining_usd"] = (
        max(0, limit - summary["total_cost_usd"]) if limit and limit > 0 else None
    )
    return info


@app.get("/v1/me/keys")
async def client_keys(client_id: Optional[str] = Depends(_resolve_client)):
    """List the calling client's own keys (masked)."""
    if not client_id:
        return []
    keys = get_keys_by_client(client_id)
    return [
        {
            "key_id": k["key_id"],
            "key_display": mask_key(k.get("key_prefix", "")),
            "label": k.get("label", ""),
            "is_active": k.get("is_active", True),
            "created_at": k.get("created_at", ""),
        }
        for k in keys
    ]


# -- /metrics ----------------------------------------------------------------


@app.get("/metrics")
async def get_metrics(_: None = Depends(_require_admin)):
    return get_stats()


@app.get("/admin/circuits")
async def get_circuits(_: None = Depends(_require_admin)):
    """View circuit breaker status for all tracked models."""
    from plugboard.circuit_breaker import (
        get_all_circuits,
        TIER_TIMEOUTS,
        FALLBACK_MAP,
    )

    return {
        "circuits": get_all_circuits(),
        "tier_timeouts": TIER_TIMEOUTS,
        "fallback_map": FALLBACK_MAP,
    }


@app.post("/admin/circuits/{model}/reset")
async def reset_circuit_endpoint(model: str, _: None = Depends(_require_admin)):
    """Manually reset a circuit breaker to closed."""
    from plugboard.circuit_breaker import reset_circuit

    reset_circuit(model)
    _audit("circuit.reset", target_type="model", target_id=model)
    return {"model": model, "state": "closed"}


@app.post("/admin/models/check-internet")
async def check_model_internet_access(
    body: Dict[str, Any], _: None = Depends(_require_admin)
):
    """Probe a model to check for live internet/web-search access.

    Detection uses a nonce-based approach: a unique token is embedded in the
    prompt and the model must echo it back.  Only a model with real-time web
    access can retrieve today's date from an external source AND return the
    exact nonce — a model that merely repeats the prompt cannot satisfy both
    constraints simultaneously.

    By default the result is **not** persisted to providers.json.  Pass
    ``"persist": true`` in the request body to save ``internet_access`` for
    the model.  Name-based heuristics (kimi, glm …) are intentionally absent;
    the decision is driven solely by the actual response.
    """
    import datetime as _dt

    model_id = body.get("model_id", "")
    if not model_id:
        raise HTTPException(400, "Missing model_id")

    persist: bool = bool(body.get("persist", False))

    # 1. Resolve provider and real model name
    try:
        provider, real_model = resolve_provider_and_model(model_id)
    except Exception as exc:
        raise HTTPException(404, f"Model '{model_id}' not found: {exc}")

    has_internet = False
    status_str = "offline"
    error_str = None
    confidence = "low"  # low | medium | high — persisted as internet_access_confidence

    # ── PROBE METHODOLOGY v2 (2026-05-31) ────────────────────────────────
    # Core matcher logic now lives in plugboard.probes.internet (extracted
    # 2026-05-31 P0-02 so the live-test framework and scripts/probe_all_models
    # share a single source of truth). This endpoint only wires together the
    # HTTP call + retry-on-fail + persistence policy.
    from plugboard.probes.internet import (
        build_internet_probe_body,
        evaluate_probe_response,
    )

    probe_body = build_internet_probe_body(real_model or model_id)

    try:
        # Retry-on-fail guard (2026-05-31 — user observed claude-opus-4-6
        # flapping between online/offline on consecutive probes even with
        # temperature=0). One LLM call is not enough to confidently mark a
        # model as offline: a single truncated or evasive reply produces a
        # false negative that then sticks via persist=True.
        PROBE_MAX_ATTEMPTS = 2
        last_result = None
        for attempt in range(1, PROBE_MAX_ATTEMPTS + 1):
            response = await forward(probe_body, timeout=30.0)
            last_result = evaluate_probe_response(response)
            if last_result.has_internet:
                break
            log.info(
                "probe_attempt %d/%d failed for %s: missing %s",
                attempt,
                PROBE_MAX_ATTEMPTS,
                model_id,
                ", ".join(last_result.attempts_missing),
            )

        has_internet = last_result.has_internet
        status_str = last_result.status
        confidence = last_result.confidence
        if not has_internet:
            error_str = f"after {PROBE_MAX_ATTEMPTS} attempts — " + (
                last_result.error or "no signal"
            )

    except Exception as exc:
        reason = str(exc)
        from plugboard.model_quarantine import (
            is_account_unsupported_error,
            is_terminal_model_unavailable_error,
            record_terminal_unavailable,
        )

        is_terminal = (
            "404" in reason
            or "not found" in reason.lower()
            or "model_not_found" in reason
            or "401" in reason
            or "403" in reason
            or "unauthorized" in reason.lower()
            or "forbidden" in reason.lower()
            or "key" in reason.lower()
            or "timeout" in reason.lower()
            or "timed out" in reason.lower()
            or "exited" in reason.lower()
            or is_account_unsupported_error(reason)
            or is_terminal_model_unavailable_error(reason)
        )

        if is_terminal:
            record_terminal_unavailable(real_model, reason=reason[:200])
            invalidate_models_cache()
            return {
                "model_id": model_id,
                "internet_access": False,
                "status": "quarantined",
                "error": reason,
            }

        error_str = reason

    # 3. Optionally persist the result back into providers.json.
    if persist:
        models_list = provider.get("models", []) or []
        updated = False
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        for m in models_list:
            if isinstance(m, dict) and m.get("id") == real_model:
                m["internet_access"] = has_internet
                m["internet_access_confidence"] = confidence
                m["last_internet_probe_at"] = now_iso
                if error_str:
                    m["internet_access_reason"] = error_str
                else:
                    m.pop("internet_access_reason", None)
                updated = True
                break

        if updated:
            update_provider(provider["id"], {"models": models_list})
            invalidate_models_cache()
            status_str = "updated"

    return {
        "model_id": model_id,
        "internet_access": has_internet,
        "internet_access_confidence": confidence,
        "status": status_str,
        "error": error_str,
    }


# Concurrency limit for the bulk internet check. Each probe is a real LLM
# call; running 30+ in parallel would saturate provider rate limits. Tunable
# via env. Cooldown is shared with /admin/models/refresh to prevent admins
# from spamming both endpoints.
_BULK_INTERNET_CONCURRENCY = max(
    1, int(os.environ.get("PLUGBOARD_BULK_INTERNET_CONCURRENCY", "3") or "3")
)
_BULK_INTERNET_PER_MODEL_TIMEOUT_S = max(
    5.0,
    float(os.environ.get("PLUGBOARD_BULK_INTERNET_PER_MODEL_TIMEOUT_S", "25") or "25"),
)
_BULK_INTERNET_LOCK = _asyncio.Lock()


@app.post("/admin/models/check-internet/all")
async def check_all_models_internet_access(
    body: Optional[Dict[str, Any]] = None,
    _: None = Depends(_require_admin),
):
    """Bulk-probe every active model for live internet access.

    Iterates over every model returned by ``list_models()`` and calls
    :func:`check_model_internet_access` with bounded concurrency
    (``PLUGBOARD_BULK_INTERNET_CONCURRENCY``, default 3). A single global
    lock prevents two bulk runs from overlapping — a second concurrent call
    returns 409.

    Pass ``"persist": true`` (default ``false``) to write ``internet_access``
    back into ``providers.json`` for each tested model. Pass ``"provider":
    "<provider_id>"`` to restrict the scan to one provider.
    """
    import time as _time

    body = body or {}
    persist: bool = bool(body.get("persist", False))
    provider_filter: Optional[str] = body.get("provider")

    if _BULK_INTERNET_LOCK.locked():
        raise HTTPException(
            status_code=409,
            detail="A bulk internet check is already running. Retry once it completes.",
        )

    async with _BULK_INTERNET_LOCK:
        started_at = _time.monotonic()
        items = await list_models()
        if provider_filter:
            items = [
                m
                for m in items
                if (
                    m.get("owned_by") == provider_filter
                    or m.get("provider") == provider_filter
                )
            ]

        semaphore = _asyncio.Semaphore(_BULK_INTERNET_CONCURRENCY)
        results: List[Dict[str, Any]] = []

        async def _probe_one(m: Dict[str, Any]) -> Dict[str, Any]:
            mid = m.get("id", "")
            async with semaphore:
                try:
                    coro = check_model_internet_access(
                        {"model_id": mid, "persist": persist},
                        _=None,
                    )
                    return await _asyncio.wait_for(
                        coro, timeout=_BULK_INTERNET_PER_MODEL_TIMEOUT_S
                    )
                except _asyncio.TimeoutError:
                    return {
                        "model_id": mid,
                        "internet_access": False,
                        "status": "timeout",
                        "error": f"per-model timeout {_BULK_INTERNET_PER_MODEL_TIMEOUT_S}s",
                    }
                except HTTPException as exc:
                    return {
                        "model_id": mid,
                        "internet_access": False,
                        "status": "error",
                        "error": f"HTTP {exc.status_code}: {exc.detail}",
                    }
                except Exception as exc:  # noqa: BLE001
                    return {
                        "model_id": mid,
                        "internet_access": False,
                        "status": "error",
                        "error": str(exc)[:300],
                    }

        gathered = await _asyncio.gather(*(_probe_one(m) for m in items))
        results.extend(gathered)

        elapsed = _time.monotonic() - started_at
        online = sum(1 for r in results if r.get("internet_access"))
        failed = sum(1 for r in results if r.get("status") in ("error", "timeout"))
        offline = len(results) - online - failed

        # Invalidate models cache so the UI immediately sees the persisted flag.
        if persist:
            try:
                from plugboard.router import invalidate_models_cache

                invalidate_models_cache()
            except Exception:
                pass

        log.info(
            "bulk_internet_check completed: total=%d online=%d offline=%d failed=%d elapsed=%.1fs persist=%s",
            len(results),
            online,
            offline,
            failed,
            elapsed,
            persist,
        )

        return {
            "total": len(results),
            "online_count": online,
            "offline_count": offline,
            "failed_count": failed,
            "elapsed_s": elapsed,
            "persisted": persist,
            "results": results,
        }


# -- /admin/cache ------------------------------------------------------------


@app.get("/admin/cache/stats")
async def cache_stats(_: None = Depends(_require_admin)):
    """Return cache statistics (entries, hits, cost saved).

    The semantic cache is opt-in and may not be initialised (missing dep,
    chmod failure, schema migration crash). We isolate its failure so the
    exact-cache stats remain available.
    """
    from plugboard.cache import get_cache_stats

    semantic_stats: Dict[str, Any]
    try:
        from plugboard import semantic_cache as _sem

        semantic_stats = _sem.get_stats()
    except Exception as exc:
        log.warning("semantic_cache.get_stats failed: %s", exc)
        semantic_stats = {"enabled": False, "error": str(exc)}

    return {
        "exact": get_cache_stats(),
        "semantic": semantic_stats,
    }


@app.delete("/admin/cache", status_code=200)
async def clear_cache_endpoint(_: None = Depends(_require_admin)):
    """Clear all cached responses."""
    from plugboard.cache import clear_cache

    deleted = clear_cache()
    _audit("cache.clear", details=f"deleted={deleted}")
    return {"deleted": deleted}


# -- /admin/monitoring -------------------------------------------------------


@app.get("/admin/monitoring")
async def admin_monitoring(_: None = Depends(_require_admin)):
    """Full health check of all providers + metrics + uptime."""
    from plugboard.monitoring import full_health_check, get_uptime_stats

    report = await full_health_check()

    # Add uptime stats for each provider
    for p in report["providers"]:
        if p.get("active"):
            p["uptime_24h"] = get_uptime_stats(p["id"], hours=24)
            p["uptime_7d"] = get_uptime_stats(p["id"], hours=168)

    return report


@app.get("/admin/monitoring/history")
async def admin_monitoring_history(
    provider_id: Optional[str] = None,
    limit: int = 100,
    _: None = Depends(_require_admin),
):
    """Retrieve monitoring history snapshots."""
    from plugboard.monitoring import get_monitoring_history

    return {"entries": get_monitoring_history(provider_id=provider_id, limit=limit)}


# -- /admin/watchdog ---------------------------------------------------------


@app.get("/admin/watchdog")
async def admin_watchdog_status(_: None = Depends(_require_admin)):
    """Get watchdog status and all provider states."""
    from plugboard.provider_watchdog import get_watchdog_status

    return get_watchdog_status()


@app.post("/admin/watchdog/start")
async def admin_watchdog_start(_: None = Depends(_require_admin)):
    """Start the provider watchdog background task."""
    from plugboard.provider_watchdog import start_watchdog, get_watchdog_status

    start_watchdog()
    _audit("watchdog.start")
    return get_watchdog_status()


@app.post("/admin/watchdog/stop")
async def admin_watchdog_stop(_: None = Depends(_require_admin)):
    """Stop the provider watchdog background task."""
    from plugboard.provider_watchdog import stop_watchdog, get_watchdog_status

    stop_watchdog()
    _audit("watchdog.stop")
    return get_watchdog_status()


@app.post("/admin/watchdog/{provider_id}/restart")
async def admin_watchdog_restart(provider_id: str, _: None = Depends(_require_admin)):
    """Manually trigger a provider restart via the launcher script."""
    from plugboard.provider_watchdog import restart_provider

    result = restart_provider(provider_id)
    _audit(
        "watchdog.restart",
        target_type="provider",
        target_id=provider_id,
        details="success" if result.get("success") else result.get("error", "failed"),
    )
    if not result.get("success"):
        raise HTTPException(500, result.get("error", "Restart failed"))
    return result


@app.get("/admin/watchdog/events")
async def admin_watchdog_events(
    provider_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
    _: None = Depends(_require_admin),
):
    """Retrieve watchdog event history."""
    from plugboard.provider_watchdog import get_watchdog_events

    return {
        "entries": get_watchdog_events(
            provider_id=provider_id, event_type=event_type, limit=limit
        )
    }


@app.get("/admin/watchdog/restarts")
async def admin_watchdog_restart_stats(
    hours: int = 24,
    _: None = Depends(_require_admin),
):
    """Get restart statistics over the last N hours."""
    from plugboard.provider_watchdog import get_restart_stats

    return get_restart_stats(hours=hours)


# -- /admin/models/status and /admin/models/refresh --------------------------

_MODEL_REFRESH_LOCK = _asyncio.Lock()
_MODEL_REFRESH_TIMEOUT_S = 120
_MODEL_REFRESH_COOLDOWN_S: float = float(
    os.environ.get("PLUGBOARD_REFRESH_COOLDOWN_S", "60") or "60"
)
_LAST_REFRESH_AT: float = 0.0  # time.monotonic() stamp of last successful refresh


_REASON_NORMALISATION = {
    # Map verbose/variable reason strings to stable short codes.
    # Keeps the public API surface stable regardless of internal wording changes.
    "empty completion": "empty_response",
    "subprocess timeout": "timeout",
    "ollama_daemon_unreachable": "daemon_unreachable",
}


def _normalise_reason(raw: Optional[str]) -> Optional[str]:
    """Return a normalised short reason code, or None."""
    if not raw:
        return None
    lower = raw.lower()
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if "404" in raw or "not found" in lower:
        return "http_404"
    if "empty" in lower:
        return "empty_response"
    if "ollama_daemon_unreachable" in lower or "daemon_unreachable" in lower:
        return "daemon_unreachable"
    return _REASON_NORMALISATION.get(raw, raw)


@app.get("/admin/models/status")
async def admin_models_status(_: None = Depends(_require_admin)):
    """Return persisted model connectivity status and scheduler metadata.

    Note: the ``status_file`` path is intentionally omitted from the response
    to avoid leaking absolute filesystem paths (MN-2). The ``reason`` field
    is normalised to short codes (``timeout``, ``http_404``, ``empty_response``,
    ``daemon_unreachable``) for API stability; detailed messages are in logs.
    """
    from plugboard.model_availability import load_model_status

    status = load_model_status()
    scheduler_meta: dict = {}
    try:
        sched = getattr(app.state, "model_availability_scheduler", None)
        if sched is not None:
            scheduler_meta = sched.status_dict
    except Exception:
        pass

    # Normalise reason codes — strip verbose/internal details from public API.
    raw_models: Dict[str, Any] = status.get("models", {})
    normalised_models = {
        k: {**v, "reason": _normalise_reason(v.get("reason"))}
        for k, v in raw_models.items()
    }

    return {
        "last_run_at": status.get("last_run_at"),
        "model_count": len(raw_models),
        "models": normalised_models,
        "scheduler": scheduler_meta,
    }


@app.post("/admin/models/refresh")
async def admin_models_refresh(_: None = Depends(_require_admin)):
    """Trigger a synchronous connectivity re-probe for all active providers.

    Blocks until complete (timeout: 120s). Returns the updated status.
    A 60-second cooldown (configurable via PLUGBOARD_REFRESH_COOLDOWN_S)
    prevents sequential spam: repeated calls within the cooldown window
    receive HTTP 429 with a Retry-After header.
    """
    from fastapi.responses import JSONResponse

    from plugboard.model_availability import run_connectivity_probes

    global _LAST_REFRESH_AT

    if _MODEL_REFRESH_LOCK.locked():
        raise HTTPException(status_code=409, detail="A refresh is already in progress")

    elapsed = time.monotonic() - _LAST_REFRESH_AT
    remaining = _MODEL_REFRESH_COOLDOWN_S - elapsed
    if remaining > 0:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(int(remaining) + 1)},
            content={
                "detail": f"Refresh cooldown active, retry in {int(remaining) + 1}s"
            },
        )

    async with _MODEL_REFRESH_LOCK:
        try:
            result = await _asyncio.wait_for(
                run_connectivity_probes(), timeout=_MODEL_REFRESH_TIMEOUT_S
            )
        except _asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"Connectivity probe timed out after {_MODEL_REFRESH_TIMEOUT_S}s",
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    _LAST_REFRESH_AT = time.monotonic()
    _audit("models.refresh")
    invalidate_models_cache()
    return {
        "last_run_at": result.get("last_run_at"),
        "model_count": len(result.get("models", {})),
        "models": result.get("models", {}),
    }


# -- /admin/audit ------------------------------------------------------------


@app.get("/admin/audit")
async def admin_audit_log(
    limit: int = 50,
    offset: int = 0,
    action: Optional[str] = None,
    _: None = Depends(_require_admin),
):
    """Retrieve admin audit trail (newest first)."""
    entries = get_audit_log(limit=limit, offset=offset, action=action)
    total = count_audit_log(action=action)
    return {"entries": entries, "total": total, "limit": limit, "offset": offset}


@app.get("/metrics/prometheus", include_in_schema=False)
async def prometheus_metrics(_: None = Depends(_require_admin)):
    from plugboard.metrics import format_prometheus

    return PlainTextResponse(
        content=format_prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# -- Provider management -----------------------------------------------------


class ProviderCreate(BaseModel):
    name: str
    base_url: str
    bearer_token: str
    default_model: str = ""
    is_active: bool = True
    timeout: float = 60.0
    provider_type: str = "cloud"
    auth_header_name: str = "Authorization"
    auth_header_prefix: str = "Bearer"


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    bearer_token: Optional[str] = None
    default_model: Optional[str] = None
    is_active: Optional[bool] = None
    timeout: Optional[float] = None
    provider_type: Optional[str] = None
    auth_header_name: Optional[str] = None
    auth_header_prefix: Optional[str] = None
    models: Optional[List[Dict[str, Any]]] = None
    # Refresh this provider's model list automatically (GET /models).
    auto_discover: Optional[bool] = None
    # Models never removed by an automatic refresh.
    pinned_models: Optional[
        List[Annotated[str, Field(min_length=1, max_length=200)]]
    ] = Field(default=None, max_length=500)


def _mask(p: Dict[str, Any]) -> Dict[str, Any]:
    masked = {**p, "bearer_token": mask_token(p.get("bearer_token", ""))}
    if is_removed_provider(p):
        masked.update(
            is_active=False, migration_required=True, error=REMOVED_CONNECTOR_ERROR
        )
    return masked


@app.get("/providers")
async def get_providers(_: None = Depends(_require_admin)):
    return [_mask(p) for p in load_providers()]


@app.post("/providers", status_code=201)
async def add_provider(body: ProviderCreate, _: None = Depends(_require_admin)):
    try:
        record = create_provider(
            name=body.name,
            base_url=body.base_url,
            bearer_token=body.bearer_token,
            default_model=body.default_model,
            is_active=body.is_active,
            timeout=body.timeout,
            provider_type=body.provider_type,
            auth_header_name=body.auth_header_name,
            auth_header_prefix=body.auth_header_prefix,
        )
    except _cfg_mod.ProviderDomainNotAllowed as exc:
        _audit(
            "provider.domain.rejected",
            target_type="provider",
            target_id="",  # provider was never created
            details=json.dumps(
                {
                    "name": body.name,
                    "base_url": body.base_url,
                    "reason": str(exc),
                }
            ),
        )
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    invalidate_models_cache()
    _audit(
        "provider.create",
        target_type="provider",
        target_id=record["id"],
        details=body.name,
    )
    await _webhook("provider.created", {"provider_id": record["id"], "name": body.name})
    return _mask(record)


@app.put("/providers/{provider_id}")
async def patch_provider(
    provider_id: str, body: ProviderUpdate, _: None = Depends(_require_admin)
):
    updates = body.model_dump(exclude_none=True)
    before = get_provider(provider_id)
    try:
        record = update_provider(provider_id, updates)
    except _cfg_mod.ProviderDomainNotAllowed as exc:
        _audit(
            "provider.domain.rejected",
            target_type="provider",
            target_id=provider_id,
            details=json.dumps(
                {"base_url": updates.get("base_url", ""), "reason": str(exc)}
            ),
        )
        raise HTTPException(400, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if record is None:
        raise HTTPException(404, f"Provider '{provider_id}' not found")
    invalidate_models_cache()
    _audit(
        "provider.update",
        target_type="provider",
        target_id=provider_id,
        details=json.dumps({"fields": sorted(updates.keys())}),
    )
    if before is not None and "base_url" in updates:
        old_url = (before.get("base_url") or "").rstrip("/")
        new_url = (updates["base_url"] or "").rstrip("/")
        if old_url != new_url:
            _audit(
                "provider.base_url.change",
                target_type="provider",
                target_id=provider_id,
                details=json.dumps({"old": old_url, "new": new_url}),
            )
    return _mask(record)


@app.delete("/providers/{provider_id}", status_code=204)
async def remove_provider(provider_id: str, _: None = Depends(_require_admin)):
    if not delete_provider(provider_id):
        raise HTTPException(404, f"Provider '{provider_id}' not found")
    invalidate_models_cache()
    _audit("provider.delete", target_type="provider", target_id=provider_id)
    await _webhook("provider.deleted", {"provider_id": provider_id})


@app.get("/providers/{provider_id}/health")
async def provider_health(provider_id: str, _: None = Depends(_require_admin)):
    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(404, f"Provider '{provider_id}' not found")
    try:
        require_supported_provider(provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    from plugboard.health_probe import probe_provider

    result = await probe_provider(provider)
    return {"provider": provider_id, **result}


@app.post("/providers/{provider_id}/discover")
async def discover_provider_models(
    provider_id: str, dry_run: bool = False, _: None = Depends(_require_admin)
):
    """Discover a provider's models and write them; removals apply at once.

    ``dry_run=true`` returns what would be added and removed without writing,
    so the dashboard can ask for confirmation first.
    """
    from plugboard import change_events, model_diff

    provider = get_provider(provider_id)
    if provider is None:
        raise HTTPException(404, f"Provider '{provider_id}' not found")
    try:
        require_supported_provider(provider)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    extra: Dict[str, Any] = {}
    default_model = None
    if (provider.get("auth_source", "") or "").startswith("subprocess:"):
        from plugboard.model_availability import refresh_subprocess_provider_models

        report = await refresh_subprocess_provider_models(provider)
        models = report.get("models") or []
        default_model = report.get("default_model") or None
        extra = {
            "verified": report.get("verified_count", 0),
            "dropped": report.get("dropped", []),
        }
        trusted = True  # each model was probed
    else:
        from plugboard.router import last_discovery_trusted

        models = await discover_models(provider)
        trusted = last_discovery_trusted()
    if not models:
        raise HTTPException(502, f"Could not discover models from '{provider_id}'")
    diff = model_diff.apply(
        provider_id,
        models,
        "manual",
        default_model=default_model,
        dry_run=dry_run,
        allow_removals=trusted,
    )
    if diff.error:
        raise HTTPException(502, diff.error)
    if not dry_run:
        invalidate_models_cache()
        if diff.changed and not diff.first_population:
            batch = change_events.ChangeBatch()
            batch.add(
                "models.changed",
                {"provider": provider_id, "added": diff.added, "removed": diff.removed},
            )
            batch.flush_in_background()
        _audit(
            "provider.discover",
            target_type="provider",
            target_id=provider_id,
            details=f"added={len(diff.added)} removed={len(diff.removed)}",
        )
    return {
        "provider": provider_id,
        "discovered": len(models),
        "models": [m["id"] for m in models],
        "added": diff.added,
        "removed": diff.removed,
        "removals_allowed": trusted,
        "dry_run": dry_run,
        **extra,
    }


# -- Admin: OAuth broker (in-dashboard OAuth to Google/GitHub/OpenAI) --------


class OAuthStartRequest(BaseModel):
    preset: str  # preset key from direct_providers (e.g. "google-broker")


@app.post("/admin/oauth/start")
async def oauth_start(body: OAuthStartRequest, _: None = Depends(_require_admin)):
    """Start an OAuth flow for a broker preset. Returns auth_url or device code."""
    from plugboard.direct_providers import get_preset, is_broker_preset
    from plugboard.oauth_manager import start_flow

    preset = get_preset(body.preset)
    if preset is None:
        raise HTTPException(404, f"Unknown preset: {body.preset}")
    if not is_broker_preset(preset):
        raise HTTPException(422, f"Preset '{body.preset}' is not a broker preset")
    source = preset.get("oauth_source", "")
    try:
        result = await start_flow(source)
    except Exception as exc:
        raise HTTPException(502, f"Failed to start OAuth flow: {exc}")
    _audit(
        "oauth.start",
        target_type="oauth",
        target_id=source,
        details=f"preset:{body.preset}",
    )
    return {
        "flow": result.flow,
        "state": result.state,
        "auth_url": result.auth_url or None,
        "user_code": result.user_code or None,
        "verification_uri": result.verification_uri or None,
        "interval": result.interval,
        "expires_in": result.expires_in,
        "preset": body.preset,
    }


@app.get("/oauth/callback/{source}", include_in_schema=False)
async def oauth_callback(
    request: Request,
    source: str,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """OAuth redirect callback (Authorization Code flow).

    Public endpoint (not admin-protected) — CSRF-protected via state token.
    Exchanges the code for tokens and returns an HTML page that closes
    the popup and notifies the parent via postMessage.

    Security: every interpolated value is html-escaped for the body and
    json-encoded for the postMessage payload. The target origin is pinned
    to the current server origin instead of "*" so a hostile parent page
    that opened the popup cannot intercept the message.
    """
    import html as _html
    import json as _json

    nonce = request.state.csp_nonce

    def _render(title: str, body_html: str, payload: Dict[str, Any], close_delay: int):
        # Build the JSON payload via json.dumps so control chars and
        # quotes from untrusted inputs are escaped safely. We then
        # escape `<` → `\u003c` so an attacker-controlled value like
        # `"</script><script>alert(1)</script>"` cannot break out of
        # the surrounding <script> block (JSON is a subset of JS but
        # the parser is permissive about literal `</script>`).
        payload_js = (
            _json.dumps(payload)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
        )
        # Pin target origin to the server itself. Anything else lets a
        # cross-origin opener intercept tokens/account info.
        return (
            "<!DOCTYPE html>\n"
            '<html><head><meta charset="utf-8">'
            f"<title>OAuth — {_html.escape(title)}</title>\n"
            "<style>body{font-family:-apple-system,sans-serif;padding:2rem;"
            "text-align:center;background:#f9fafb;}"
            ".box{background:white;border-radius:12px;padding:2rem;"
            "max-width:420px;margin:0 auto;box-shadow:0 2px 8px rgba(0,0,0,0.08);}"
            ".ok{color:#059669;font-size:1.1rem;} .err{color:#dc2626;font-size:1rem;}"
            "code{background:#f3f4f6;padding:0.2rem 0.4rem;border-radius:4px;"
            "font-size:0.85rem;}</style></head><body>"
            f'<div class="box">{body_html}</div>\n'
            f'<script nonce="{nonce}">\n'
            "try {\n"
            f"  var payload = {payload_js};\n"
            "  if (window.opener) {\n"
            "    window.opener.postMessage(payload, window.location.origin);\n"
            "  }\n"
            "} catch(e) {}\n"
            f"setTimeout(function(){{ window.close(); }}, {int(close_delay)});\n"
            "</script></body></html>"
        )

    safe_source = _html.escape(source)

    if error:
        safe_error = _html.escape(error)
        body_html = (
            f'<div class="err">❌ OAuth error</div><p><code>{safe_error}</code></p>'
        )
        payload = {"type": "oauth-error", "source": source, "error": error}
        return Response(
            content=_render("Error", body_html, payload, close_delay=5000),
            media_type="text/html",
        )

    if not code or not state:
        body_html = '<div class="err">❌ Missing code or state</div>'
        payload = {"type": "oauth-error", "source": source, "error": "missing_code"}
        return Response(
            content=_render("Error", body_html, payload, close_delay=5000),
            media_type="text/html",
        )

    from plugboard.oauth_manager import complete_callback

    try:
        cred = await complete_callback(source, state, code)
    except Exception as exc:
        safe_exc = _html.escape(str(exc))
        body_html = f'<div class="err">❌ Token exchange failed</div><p><code>{safe_exc}</code></p>'
        payload = {
            "type": "oauth-error",
            "source": source,
            "error": "exchange_failed",
        }
        return Response(
            content=_render("Failed", body_html, payload, close_delay=8000),
            media_type="text/html",
        )

    # Auto-create the provider record if not exists
    _ensure_broker_provider(source)

    safe_account = _html.escape(cred.account)
    body_html = (
        f'<div class="ok">✓ Connected as<br><strong>{safe_account}</strong></div>'
        f'<p style="color:#6b7280;font-size:0.85rem;margin-top:1rem;">'
        f"Provider <code>{safe_source}-broker</code> created. "
        f"You can close this window.</p>"
    )
    payload = {"type": "oauth-success", "source": source, "account": cred.account}
    return Response(
        content=_render("Connected", body_html, payload, close_delay=2000),
        media_type="text/html",
    )


@app.get("/admin/oauth/device/poll/{state}")
async def oauth_device_poll(state: str, _: None = Depends(_require_admin)):
    """Poll a Device Flow (GitHub). Returns status + account when done."""
    from plugboard.oauth_manager import poll_device

    try:
        result = await poll_device(state)
    except Exception as exc:
        raise HTTPException(502, f"Poll failed: {exc}")
    if result.get("status") == "success":
        source = result.get("source", "")
        if source:
            _ensure_broker_provider(source)
    return result


@app.get("/admin/oauth/connections")
async def oauth_connections(_: None = Depends(_require_admin)):
    """List all OAuth connections (no secrets)."""
    from plugboard.oauth_store import list_credentials

    return {"connections": list_credentials()}


@app.delete("/admin/oauth/{source}", status_code=204)
async def oauth_disconnect(source: str, _: None = Depends(_require_admin)):
    """Revoke an OAuth connection.

    The cached tokens of that source (and of ``broker:github+copilot`` for
    ``github``) are dropped at once. Related broker provider records are kept:
    their requests fail with "Auth not available" until the source is
    connected again.
    """
    from plugboard.oauth_store import delete_credential

    deleted = delete_credential(source)
    if not deleted:
        raise HTTPException(404, f"No connection for source: {source}")
    _audit("oauth.disconnect", target_type="oauth", target_id=source)
    invalidate_models_cache()


def _ensure_broker_provider(oauth_source: str) -> None:
    """Create a Provider record for a broker-based preset after successful OAuth.

    Finds all broker presets bound to this oauth_source and creates providers
    that don't already exist.
    """
    from plugboard.direct_providers import PRESETS

    matching = [
        p
        for p in PRESETS.values()
        if p.get("oauth_source") == oauth_source
        and p.get("auth_source", "").startswith("broker:")
    ]
    for preset in matching:
        existing = get_provider(_slugify_for_broker(preset["name"]))
        if existing is not None:
            continue
        from plugboard.direct_providers import create_from_preset

        try:
            create_from_preset(preset["key"])
        except Exception as exc:
            log.warning("Failed to auto-create provider for %s: %s", preset["key"], exc)
    invalidate_models_cache()


def _slugify_for_broker(name: str) -> str:
    import re

    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# -- Admin: Direct providers (Anthropic, OpenAI, Gemini, GitHub Models) ------


class DirectProviderCreate(BaseModel):
    api_key: str = ""  # empty for OAuth presets (cli:* auth_source)
    is_active: bool = True  # "Save anyway" after a failed test creates it inactive


class DirectProviderTest(BaseModel):
    preset_key: str = ""
    base_url: str = ""  # custom OpenAI-compatible endpoint when no preset
    api_key: str = ""
    send_test_token: bool = False  # one billed token, opt-in


_TEST_DOCS = "docs/guide/providers-and-subscriptions.md#test-the-connection"


async def _require_public_host(base_url: str) -> list:
    """Refuse a catalog preset whose host resolves to a non-public address.

    Catalog base URLs come from a third-party feed; an admin key must not be
    sent to an internal address because that feed says so.
    """
    from urllib.parse import urlsplit

    from plugboard.llm_pricing import UnsafeURL, _resolve_public

    parts = urlsplit(base_url)
    try:
        # The checked addresses are returned so the caller connects to them
        # and nothing else (no second lookup that DNS rebinding could change).
        return await _resolve_public(parts.hostname or "", parts.port or 443)
    except UnsafeURL as exc:
        if str(exc).startswith("cannot resolve"):
            raise HTTPException(503, f"Catalog preset host unreachable: {exc}")
        raise HTTPException(422, f"Catalog preset refused: {exc}")


async def _test_openai_endpoint(
    base_url: str,
    api_key: str,
    send_test_token: bool,
    pinned: Optional[list] = None,
    preset: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """GET /models (and optionally a 1-token completion) against an endpoint.

    A preset's own auth header and extra headers are used (Anthropic takes
    ``x-api-key`` plus ``anthropic-version``, not a bearer token).
    """
    from urllib.parse import urlsplit

    import aiohttp

    from plugboard.endpoints import openai_url

    host = urlsplit(base_url).hostname or base_url
    headers = {"Accept": "application/json"}
    preset = preset or {}
    for name, value in (preset.get("extra_headers") or {}).items():
        headers[str(name)] = str(value)
    if api_key:
        header_name = preset.get("auth_header_name") or "Authorization"
        prefix = preset.get("auth_header_prefix")
        prefix = "Bearer" if prefix is None else prefix
        headers[header_name] = f"{prefix} {api_key}".strip() if prefix else api_key
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        connector = None
        if pinned:
            from plugboard.llm_pricing import _PinnedResolver

            connector = aiohttp.TCPConnector(
                resolver=_PinnedResolver(host, pinned), use_dns_cache=False
            )
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector
        ) as session:
            async with session.get(
                openai_url(base_url, "models"), headers=headers
            ) as resp:
                if resp.status in (401, 403):
                    return {
                        "status": "invalid_key",
                        "message": f"{resp.status} invalid key: check that this key belongs to {host} "
                        "(some providers issue separate keys per region or project).",
                        "docs": _TEST_DOCS,
                    }
                body = None
                if resp.status == 200:
                    try:
                        body = await resp.json(content_type=None)
                    except ValueError:  # HTML page, plain text...
                        body = None
                if (
                    resp.status != 200
                    or not isinstance(body, dict)
                    or not isinstance(body.get("data"), list)
                ):
                    return {
                        "status": "not_openai_compatible",
                        "message": f"Not a standard chat API: {host} did not answer GET /models in the "
                        f"standard format (HTTP {resp.status}). Add it as a custom endpoint or check its base URL.",
                        "docs": _TEST_DOCS,
                    }
            models = [
                m.get("id") for m in body["data"] if isinstance(m, dict) and m.get("id")
            ]
            result: Dict[str, Any] = {
                "status": "ok" if models else "ok_no_models",
                "models_count": len(models),
                "models": models[:50],
                "message": f"{len(models)} models found"
                if models
                else "Key valid, 0 models",
            }
            if send_test_token and models:
                chat_model = next(
                    (
                        mid
                        for mid in models
                        if not is_decision_model(mid, effective_name=True)
                        and not is_removed_model(mid, effective_name=True)
                    ),
                    None,
                )
                if chat_model is None:
                    result["completion_skipped"] = (
                        "removed_legacy_connector"
                        if any(
                            is_removed_model(mid, effective_name=True) for mid in models
                        )
                        else "decision_model"
                    )
                    return result
                async with session.post(
                    openai_url(base_url, "chat/completions"),
                    headers={**headers, "Content-Type": "application/json"},
                    json={
                        "model": chat_model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                    },
                ) as resp:
                    result["completion_ok"] = resp.status == 200
                    result["completion_status"] = resp.status
            return result
    except (aiohttp.ClientError, _asyncio.TimeoutError) as exc:
        return {
            "status": "unreachable",
            "message": f"Unreachable: {host} did not answer ({type(exc).__name__}). "
            "Check the network, a proxy or the base URL.",
            "docs": _TEST_DOCS,
        }


@app.get("/admin/direct-providers/presets")
async def list_direct_presets(_: None = Depends(_require_admin)):
    """List available direct-provider presets (no secrets)."""
    from plugboard.direct_providers import list_presets

    return {"presets": list_presets()}


@app.post("/admin/direct-providers/test")
async def test_direct_provider(
    body: DirectProviderTest, _: None = Depends(_require_admin)
):
    """Check a key against a preset or custom endpoint without creating anything."""
    from plugboard.config import check_provider_domain_allowed, ProviderDomainNotAllowed
    from plugboard.direct_providers import get_preset

    base_url = body.base_url.strip()
    pinned = None
    preset = None
    if body.preset_key:
        preset = get_preset(body.preset_key)
        if preset is None:
            raise HTTPException(404, f"Unknown preset: {body.preset_key}")
        base_url = preset.get("base_url", "")
        if preset.get("catalog"):
            pinned = await _require_public_host(base_url)
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "Give a preset_key or an http(s) base_url")
    try:
        check_provider_domain_allowed(base_url)
    except ProviderDomainNotAllowed as exc:
        raise HTTPException(422, str(exc))
    if pinned:
        return await _test_openai_endpoint(
            base_url,
            body.api_key.strip(),
            body.send_test_token,
            pinned=pinned,
            preset=preset,
        )
    return await _test_openai_endpoint(
        base_url, body.api_key.strip(), body.send_test_token, preset=preset
    )


@app.get("/admin/direct-providers/{preset_key}/check")
async def check_direct_preset(preset_key: str, _: None = Depends(_require_admin)):
    """Check if the CLI for an OAuth/subprocess preset is installed and logged in."""
    from plugboard.direct_providers import (
        get_preset,
        is_oauth_preset,
        is_subprocess_preset,
    )

    preset = get_preset(preset_key)
    if preset is None:
        raise HTTPException(404, f"Unknown preset: {preset_key}")
    if not is_oauth_preset(preset):
        return {
            "ok": True,
            "auth_type": "api_key",
            "hint": "This preset uses an API key, not OAuth.",
        }
    # Subprocess presets: verify the CLI binary is on PATH and runnable.
    if is_subprocess_preset(preset):
        from plugboard.cli_subprocess import (
            check_cli_installed,
            probe_cli_version,
        )

        kind = preset["auth_source"].removeprefix("subprocess:")
        installed = check_cli_installed(kind)
        if not installed["ok"]:
            return {
                "ok": False,
                "auth_type": "subprocess",
                "kind": kind,
                "error": installed.get("error"),
            }
        probe = await probe_cli_version(kind)
        return {
            "ok": probe["ok"],
            "auth_type": "subprocess",
            "kind": kind,
            "binary": installed.get("binary"),
            "version": probe.get("version", ""),
            "error": probe.get("error"),
        }
    # CLI-delegation presets (cli:*) — existing loader path
    from plugboard.cli_auth import get_loader

    source = preset["auth_source"].removeprefix("cli:")
    loader = get_loader(source)
    if loader is None:
        raise HTTPException(500, f"No loader for source: {source}")
    status = loader.available()
    status["auth_type"] = "oauth"
    status["source"] = source
    return status


@app.post("/admin/direct-providers/{preset_key}", status_code=201)
async def create_direct_provider(
    preset_key: str,
    body: DirectProviderCreate,
    _: None = Depends(_require_admin),
):
    """Create a provider from a preset. For OAuth presets, api_key is ignored."""
    from plugboard.direct_providers import create_from_preset, get_preset

    preset = get_preset(preset_key)
    if preset is not None and preset.get("catalog"):
        await _require_public_host(preset.get("base_url", ""))

    try:
        record = create_from_preset(preset_key, body.api_key, is_active=body.is_active)
    except ValueError as exc:
        detail = str(exc)
        if "Unknown preset" in detail:
            raise HTTPException(404, detail)
        raise HTTPException(422, detail)
    invalidate_models_cache()
    _audit(
        "provider.create",
        target_type="provider",
        target_id=record["id"],
        details=f"direct:{preset_key}",
    )
    await _webhook(
        "provider.created", {"provider_id": record["id"], "name": record["name"]}
    )
    if record.get("auto_discover") and record.get("is_active", True):
        _schedule_initial_discovery(record["id"])
    return record


_background_tasks: set = set()


def _schedule_initial_discovery(provider_id: str) -> None:
    """Replace a new provider's catalog model list with what its key can use."""

    async def _run() -> None:
        try:
            provider = get_provider(provider_id)
            if provider is None:
                return
            models = await discover_models(provider)
            if models:
                update_provider(provider_id, {"models": models})
                invalidate_models_cache()
        except Exception as exc:  # noqa: BLE001  (background: never crash the app)
            log.warning("Initial discovery for %s failed: %s", provider_id, exc)

    task = _asyncio.get_running_loop().create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# -- Admin: Ollama model management ------------------------------------------


class OllamaPullRequest(BaseModel):
    name: str  # e.g. "llama3:8b", "mistral"


@app.get("/admin/ollama/status")
async def ollama_status(_: None = Depends(_require_admin)):
    """Check Ollama availability and provider registration."""
    from plugboard.ollama import check_available, get_ollama_url

    available = await check_available()
    provider = get_provider("ollama")
    return {
        "available": available,
        "url": get_ollama_url(),
        "provider_id": provider["id"] if provider else None,
        "provider_type": provider.get("provider_type", "cloud") if provider else None,
        "is_active": provider.get("is_active", False) if provider else False,
    }


@app.get("/admin/ollama/models")
async def ollama_list_models(_: None = Depends(_require_admin)):
    """List installed Ollama models."""
    from plugboard.ollama import list_models as _ollama_list

    try:
        models = await _ollama_list()
        return {"models": models, "count": len(models)}
    except Exception as exc:
        raise HTTPException(502, f"Ollama error: {exc}")


@app.post("/admin/ollama/models/pull")
async def ollama_pull_model(body: OllamaPullRequest, _: None = Depends(_require_admin)):
    """Pull (download) an Ollama model. Returns streaming NDJSON progress."""
    import json as _json
    from plugboard.ollama import pull_model

    async def _stream():
        try:
            async for progress in pull_model(body.name):
                yield _json.dumps(progress) + "\n"
            # Sync models after pull
            provider = get_provider("ollama")
            if provider:
                from plugboard.ollama import _sync_models

                await _sync_models(provider)
            yield _json.dumps({"status": "complete", "model": body.name}) + "\n"
        except Exception as exc:
            yield _json.dumps({"status": "error", "error": str(exc)}) + "\n"

    _audit("ollama.pull", target_type="model", target_id=body.name)
    return StreamingResponse(_stream(), media_type="application/x-ndjson")


@app.delete("/admin/ollama/models/{name:path}", status_code=204)
async def ollama_delete_model(name: str, _: None = Depends(_require_admin)):
    """Delete an Ollama model."""
    from plugboard.ollama import delete_model

    try:
        deleted = await delete_model(name)
    except Exception as exc:
        raise HTTPException(502, f"Ollama error: {exc}")
    if not deleted:
        raise HTTPException(404, f"Model '{name}' not found in Ollama")
    # Sync provider models list
    provider = get_provider("ollama")
    if provider:
        from plugboard.ollama import _sync_models

        await _sync_models(provider)
    _audit("ollama.delete", target_type="model", target_id=name)


@app.get("/admin/ollama/models/{name:path}")
async def ollama_model_info(name: str, _: None = Depends(_require_admin)):
    """Get detailed info for an Ollama model."""
    from plugboard.ollama import show_model

    try:
        info = await show_model(name)
        return info
    except RuntimeError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(404, str(exc))
        raise HTTPException(502, str(exc))


# -- Admin: API key management -----------------------------------------------


class KeyCreate(BaseModel):
    client_id: str
    label: str = ""
    # None = let the server apply the production-safe env default
    # (PLUGBOARD_DEFAULT_MONTHLY_LIMIT_USD / PLUGBOARD_DEFAULT_RPM_LIMIT).
    # Explicit 0 / 0.0 means "opt out — truly unlimited".
    monthly_limit_usd: Optional[float] = None
    allowed_models: Optional[List[str]] = None
    allowed_decision_models: Optional[
        List[Literal["jev", "laya", "laya-multilingual"]]
    ] = None
    rpm_limit: Optional[int] = None  # requests per minute
    org_id: str = ""  # organisation (empty = no org)


def _default_rpm_limit() -> int:
    """Production-safe default applied when a key is created without an
    explicit rpm_limit. Operators can raise/lower via env without code change."""
    try:
        return max(0, int(os.environ.get("PLUGBOARD_DEFAULT_RPM_LIMIT", "60")))
    except ValueError:
        return 60


def _default_monthly_limit_usd() -> float:
    """Production-safe default applied when a key is created without an
    explicit monthly_limit_usd. 0 disables the default entirely."""
    try:
        return max(
            0.0, float(os.environ.get("PLUGBOARD_DEFAULT_MONTHLY_LIMIT_USD", "10"))
        )
    except ValueError:
        return 10.0


@app.get("/admin/keys")
async def list_api_keys(_: None = Depends(_require_admin)):
    return [
        {**k, "key_display": mask_key(k.get("key_prefix", ""))} for k in load_keys()
    ]


@app.post("/admin/keys", status_code=201)
async def create_api_key(body: KeyCreate, _: None = Depends(_require_admin)):
    """Creates a new client key. The key is shown in plaintext ONLY in this response."""
    rpm = body.rpm_limit if body.rpm_limit is not None else _default_rpm_limit()
    budget = (
        body.monthly_limit_usd
        if body.monthly_limit_usd is not None
        else _default_monthly_limit_usd()
    )
    rec = create_key(
        client_id=body.client_id,
        label=body.label,
        monthly_limit_usd=budget,
        allowed_models=body.allowed_models,
        rpm_limit=rpm,
        org_id=body.org_id,
        allowed_decision_models=body.allowed_decision_models,
    )
    _audit(
        "key.create",
        target_type="key",
        target_id=rec.get("key_id", ""),
        details=body.client_id,
    )
    await _webhook(
        "key.created", {"key_id": rec.get("key_id"), "client_id": body.client_id}
    )
    return rec


@app.get("/admin/keys/client/{client_id}")
async def list_keys_by_client(client_id: str, _: None = Depends(_require_admin)):
    """List all keys for a specific client_id."""
    keys = get_keys_by_client(client_id)
    return [{**k, "key_display": mask_key(k.get("key_prefix", ""))} for k in keys]


@app.post("/admin/keys/{key_id}/revoke", status_code=200)
async def revoke_api_key(key_id: str, _: None = Depends(_require_admin)):
    if not revoke_key(key_id):
        raise HTTPException(404, f"Key '{key_id}' not found")
    _audit("key.revoke", target_type="key", target_id=key_id)
    await _webhook("key.revoked", {"key_id": key_id})
    return {"key_id": key_id, "is_active": False}


@app.delete("/admin/keys/{key_id}", status_code=204)
async def delete_api_key(key_id: str, _: None = Depends(_require_admin)):
    if not delete_key(key_id):
        raise HTTPException(404, f"Key '{key_id}' not found")
    _audit("key.delete", target_type="key", target_id=key_id)


@app.post("/admin/keys/{key_id}/rotate", status_code=201)
async def rotate_api_key(key_id: str, _: None = Depends(_require_admin)):
    """Rotate a key: revoke the old one, create a new one with same settings."""
    result = rotate_key(key_id)
    if result is None:
        raise HTTPException(404, f"Key '{key_id}' not found")
    _audit("key.rotate", target_type="key", target_id=key_id)
    await _webhook("key.rotated", {"key_id": key_id})
    return result


# -- Admin: Organisations ----------------------------------------------------


@app.get("/admin/orgs")
async def list_orgs(_: None = Depends(_require_admin)):
    """List all organisations with key counts."""
    return get_orgs()


@app.get("/admin/orgs/{org_id}/keys")
async def list_org_keys(org_id: str, _: None = Depends(_require_admin)):
    """List all keys for an organisation."""
    keys = get_keys_by_org(org_id)
    return [{**k, "key_display": mask_key(k.get("key_prefix", ""))} for k in keys]


@app.get("/admin/orgs/{org_id}/usage/{month}")
async def org_usage(org_id: str, month: str, _: None = Depends(_require_admin)):
    """Aggregate usage for all clients in an organisation."""
    keys = get_keys_by_org(org_id)
    client_ids = {k.get("client_id") for k in keys}
    all_entries = []
    for cid in client_ids:
        all_entries.extend(_usage.get_usage_by_client(month, cid))
    return {
        "org_id": org_id,
        "month": month,
        "clients": sorted(client_ids),
        "entries": all_entries,
        "summary": _usage.summarize_usage(all_entries),
    }


# -- Admin: Jobs / Crons -----------------------------------------------------


@app.get("/admin/jobs")
async def list_jobs(_: None = Depends(_require_admin)):
    """Background jobs (price refresh, model refresh, watchdog) and their state."""
    from plugboard.jobs import get_jobs_status

    return get_jobs_status(app.state)


@app.get("/admin/jobs/{job_id}/log")
async def job_log(job_id: str, lines: int = 50, _: None = Depends(_require_admin)):
    """Last result of a background job."""
    from plugboard.jobs import get_job_log

    result = get_job_log(job_id, lines=lines, app_state=app.state)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@app.post("/admin/jobs/{job_id}/run")
async def run_job_endpoint(job_id: str, _: None = Depends(_require_admin)):
    """Start a background job now; it runs on its own and the call returns at once."""
    from plugboard.jobs import run_job

    result = await run_job(job_id, app.state)
    # run_job returns a bare {"error": ...} (no "status") ONLY for genuine
    # not-found cases (unknown job id / missing script) -> 404. Every other
    # outcome (success / error / timeout / continuous) carries a "status" and is
    # returned as 200 so the dashboard can render the real message instead of a
    # generic 404 (which the frontend used to display as "undefined — Output").
    if "status" not in result and "error" in result:
        raise HTTPException(404, result["error"])
    _audit(
        "job.run", target_type="job", target_id=job_id, details=result.get("status", "")
    )
    return result


# -- Admin: Pricing management -----------------------------------------------


class PricingUpsert(BaseModel):
    models: Dict[str, Any]
    note: str = ""
    # None keeps the provider prices already in the grid.
    models_by_provider: Optional[Dict[str, Dict[str, Any]]] = None


class PricingPatch(BaseModel):
    models: Dict[str, Any] = Field(default_factory=dict)
    models_by_provider: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class PricingRevert(BaseModel):
    model: str = Field(min_length=1, max_length=300)
    # Price space (provider) of a models_by_provider entry; empty = vendor price.
    provider: str = Field(default="", max_length=200)


class SuggestionIds(BaseModel):
    ids: List[str] = Field(min_length=1, max_length=500)


_MONTH_RE = re.compile(r"^\d{4}_(0[1-9]|1[0-2])$")


def _check_month(month: str) -> None:
    if not _MONTH_RE.match(month):
        raise HTTPException(422, "month must look like YYYY_MM")


def _load_grid_for_edit(month: str) -> Dict[str, Any]:
    try:
        return _billing.load_pricing(month, strict=True) or {"models": {}}
    except _billing.PricingFileError as exc:
        raise HTTPException(503, str(exc))


def _manual_rows(
    rows: Dict[str, Any], current: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    """Validate admin rows; mark as manual only those that change a price."""
    from plugboard.pricing_merge import MAX_PRICE, same_price

    out: Dict[str, Dict[str, Any]] = {}
    for model, row in rows.items():
        if not isinstance(row, dict):
            raise HTTPException(422, f"{model}: expected {{input_price, output_price}}")
        try:
            inp = float(row.get("input_price"))
            outp = float(row.get("output_price"))
        except (TypeError, ValueError):
            raise HTTPException(
                422, f"{model}: input_price and output_price must be numbers"
            )
        if not (0 <= inp <= MAX_PRICE and 0 <= outp <= MAX_PRICE):
            raise HTTPException(
                422,
                f"{model}: prices must be between 0 and {MAX_PRICE:g} USD per 1M tokens",
            )
        price = {k: v for k, v in row.items() if k not in ("source", "previous")}
        price.update(input_price=inp, output_price=outp)
        old = current.get(model)
        if old is not None and same_price(old, price):
            out[model] = {**old, **price}  # unchanged: keeps its source
        else:
            out[model] = {**price, "source": "manual"}
            # Keep the automatic price it replaces, so a revert can restore it
            # even before the next update fetches the sources again.
            if old is not None and old.get("source") != "manual":
                out[model]["previous"] = {
                    k: old[k]
                    for k in ("input_price", "output_price", "source")
                    if k in old
                }
            elif old is not None and old.get("previous"):
                out[model]["previous"] = old["previous"]
    return out


@app.get("/admin/pricing/llm/status")
async def llm_pricing_status(_: None = Depends(_require_admin)):
    """Prices read by a model: settings, today's spend and why each model has no price."""
    from plugboard import llm_pricing

    return llm_pricing.status()


@app.post("/admin/pricing/llm/run")
async def llm_pricing_run(_: None = Depends(_require_admin)):
    """Run one pass now, ignoring the once-a-day rule (the daily budget still applies)."""
    from plugboard import llm_pricing

    if not llm_pricing.enabled():
        raise HTTPException(409, "LLM pricing is off: set PLUGBOARD_LLM_PRICING=1")
    report = await llm_pricing.run(await llm_pricing.missing_models(), force=True)
    _audit(
        "pricing.llm.run",
        details=f"calls={report.calls} suggested={len(report.suggested)}",
    )
    return report.as_dict()


@app.get("/admin/pricing/suggestions")
async def list_pricing_suggestions(
    status: str = "pending", _: None = Depends(_require_admin)
):
    """Price changes held for review (type drift) and LLM-read prices (type llm)."""
    from plugboard import pricing_suggestions

    return {"items": pricing_suggestions.list_suggestions(status or None)}


async def _suggestion_action(sid: str, action: str) -> Dict[str, Any]:
    from plugboard import pricing_suggestions

    try:
        item = getattr(pricing_suggestions, action)(sid)
    except KeyError:
        raise HTTPException(404, f"Unknown suggestion '{sid}'")
    except pricing_suggestions.SuggestionConflict as exc:
        raise HTTPException(409, str(exc))
    except _billing.PricingFileError as exc:
        raise HTTPException(503, str(exc))
    _audit(
        f"pricing.suggestion.{action}",
        target_type="pricing",
        target_id=item.get("model", sid),
        details=f"{item.get('type')} {sid}",
    )
    if action == "apply" and item.get("current"):
        from plugboard.pricing_scraper import price_change_events

        change = {
            "model": item["model"],
            "source": item.get("source", ""),
            "old": item["current"],
            "new": item["proposed"],
            "pct": item.get("pct"),
            "space": item.get("space", ""),
        }
        price_change_events(item["month"], [change], [], -1).flush_in_background()
    return item


@app.post("/admin/pricing/suggestions/apply")
async def apply_pricing_suggestions(
    body: SuggestionIds, _: None = Depends(_require_admin)
):
    return await _bulk_suggestions(body.ids, "apply")


@app.post("/admin/pricing/suggestions/reject")
async def reject_pricing_suggestions(
    body: SuggestionIds, _: None = Depends(_require_admin)
):
    return await _bulk_suggestions(body.ids, "reject")


async def _bulk_suggestions(ids: List[str], action: str) -> Dict[str, Any]:
    done, errors = [], []
    for sid in ids:
        try:
            done.append(await _suggestion_action(sid, action))
        except HTTPException as exc:
            errors.append({"id": sid, "status": exc.status_code, "detail": exc.detail})
    return {"done": done, "errors": errors}


@app.post("/admin/pricing/suggestions/{sid}/apply")
async def apply_pricing_suggestion(sid: str, _: None = Depends(_require_admin)):
    return await _suggestion_action(sid, "apply")


@app.post("/admin/pricing/suggestions/{sid}/reject")
async def reject_pricing_suggestion(sid: str, _: None = Depends(_require_admin)):
    return await _suggestion_action(sid, "reject")


@app.patch("/admin/pricing/{month}")
async def patch_pricing(
    month: str, body: PricingPatch, _: None = Depends(_require_admin)
):
    """Set the price of some models; changed rows are marked ``source=manual``.

    Other rows are left as they are. A manual price is never replaced by a
    source; ``POST /admin/pricing/{month}/revert`` gives the model back to them.
    """
    _check_month(month)
    changed: List[str] = []
    with _billing.pricing_lock(month):
        grid = _load_grid_for_edit(month)
        models = grid.setdefault("models", {})
        rows = _manual_rows(body.models, models)
        models.update(rows)
        changed += [m for m, r in rows.items() if r.get("source") == "manual"]
        for space, space_rows in body.models_by_provider.items():
            table = grid.setdefault("models_by_provider", {}).setdefault(space, {})
            rows = _manual_rows(space_rows, table)
            table.update(rows)
            changed += [
                f"{space}|{m}" for m, r in rows.items() if r.get("source") == "manual"
            ]
        _billing.save_pricing(month, grid)
    _audit(
        "pricing.patch",
        target_type="pricing",
        target_id=month,
        details=",".join(changed)[:500],
    )
    return _billing.load_pricing(month)


@app.post("/admin/pricing/{month}/revert")
async def revert_pricing(
    month: str, body: PricingRevert, _: None = Depends(_require_admin)
):
    """Drop a manual price and give the model back to the automatic sources.

    The new price comes from the last update's sources, else the automatic
    price the manual edit replaced, else the previous month (see revert_model).
    """
    from plugboard.pricing_scraper import revert_model

    _check_month(month)
    if month != _billing.current_pricing_month():
        raise HTTPException(
            409, "only the current month can be recomputed; past months are frozen"
        )
    try:
        entry = revert_model(month, body.model, body.provider)
    except KeyError:
        raise HTTPException(404, f"No price for '{body.model}' in {month}")
    except _billing.PricingFileError as exc:
        raise HTTPException(503, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    _audit("pricing.revert", target_type="pricing", target_id=body.model, details=month)
    return {"model": body.model, "entry": entry}


@app.get("/admin/pricing/{month}")
async def get_pricing(month: str, _: None = Depends(_require_admin)):
    pricing = _billing.load_pricing(month)
    if not pricing:
        raise HTTPException(404, f"No pricing data for month '{month}'")
    return pricing


@app.post("/admin/pricing/scrape")
async def scrape_pricing(_: None = Depends(_require_admin)):
    """Scrape latest pricing from configured sources and update current month grid."""
    from plugboard.pricing_scraper import update_current_pricing

    # Get all model IDs from providers for discovery
    all_models = []
    try:
        items = await list_models()
        all_models = [m.get("id", "") for m in items if m.get("id")]
    except Exception as exc:
        log.warning("Pricing scrape model discovery failed: %s", exc)

    result = await update_current_pricing(provider_models=all_models)
    _audit(
        "pricing.scrape", details=f"added={result['added']} updated={result['updated']}"
    )
    return result


@app.get("/admin/pricing/scheduler/status")
async def pricing_scheduler_status(_: None = Depends(_require_admin)):
    """Expose PricingScheduler state (CB, failure count, next run) for SREs.

    Manifesto §29.7 — observability is a Build → Run requirement so
    operators can diagnose a stuck scheduler without grepping logs.
    """
    from plugboard import models_dev

    sched = getattr(app.state, "pricing_scheduler", None)
    if sched is None:
        return {
            "running": False,
            "error": "scheduler not initialised",
            "catalog": models_dev.status(),
        }
    return {**sched.status_dict, "catalog": models_dev.status()}


@app.get("/admin/models/scheduler/status")
async def models_scheduler_status(_: None = Depends(_require_admin)):
    sched = getattr(app.state, "model_availability_scheduler", None)
    if sched is None:
        return {"running": False, "error": "scheduler not initialised"}
    return sched.status_dict


@app.get("/admin/auto-update/status")
async def auto_update_status(journal: int = 20, _: None = Depends(_require_admin)):
    """Prices, catalog and model lists in one view, with the latest changes."""
    from plugboard import change_events, models_dev, pricing_suggestions

    pricing = getattr(app.state, "pricing_scheduler", None)
    models = getattr(app.state, "model_availability_scheduler", None)
    return {
        "pricing": pricing.status_dict
        if pricing
        else {"error": "scheduler not initialised"},
        "models": models.status_dict
        if models
        else {"error": "scheduler not initialised"},
        "catalog": models_dev.status(),
        "pending_suggestions": len(pricing_suggestions.list_suggestions("pending")),
        "recent_changes": change_events.read_journal(max(1, min(journal, 200))),
    }


@app.post("/admin/auto-update/run", status_code=202)
async def auto_update_run(_: None = Depends(_require_admin)):
    """Start a price update and a model-list refresh now, in the background.

    409 with ``running_since`` while either one is already running.
    """
    pricing = getattr(app.state, "pricing_scheduler", None)
    models = getattr(app.state, "model_availability_scheduler", None)
    for sched in (pricing, models):
        if sched is not None and sched.running_since is not None:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "an update is already running",
                    "running_since": sched.running_since,
                },
            )

    async def _run() -> None:
        for name, sched in (("pricing", pricing), ("models", models)):
            if sched is None:
                continue
            try:
                await sched.run_now("manual")
            except RuntimeError:
                log.info("auto-update: %s update already running, skipped", name)
            except Exception as exc:  # noqa: BLE001
                log.warning("auto-update: %s update failed: %s", name, exc)

    task = _asyncio.create_task(_run(), name="auto-update-run")
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    _audit("auto_update.run")
    return {"started": True}


@app.get("/admin/coverage/pricing")
async def pricing_coverage(_: None = Depends(_require_admin)):
    """Audit pricing coverage for every exposed model.

    PRICE-1: returns which models have a priced entry, which are free
    (local Ollama), and which are MISSING (billing will charge 0, which
    under-reports usage cost). CI-friendly — an empty `missing` list
    means every caller gets an accurate bill.
    """
    items = await list_models()
    pricing = _billing.load_pricing(_billing.current_pricing_month())
    providers_by_id = {p.get("id"): p for p in load_providers()}
    covered: list = []
    free: list = []
    missing: list = []
    for m in items:
        mid = m.get("id", "")
        real = mid.split(":", 1)[-1] if ":" in mid else mid
        provider = providers_by_id.get(mid.split(":", 1)[0]) if ":" in mid else None
        price = _billing.resolve_model_price(real, pricing, provider=provider)
        if price is None:
            missing.append(mid)
        elif price.get("input_price", 0) == 0 and price.get("output_price", 0) == 0:
            free.append(mid)
        else:
            covered.append(mid)
    return {
        "month": _billing.current_pricing_month(),
        "total": len(items),
        "covered_paid": len(covered),
        "covered_free": len(free),
        "missing": missing,
        "coverage_pct": round(
            ((len(covered) + len(free)) / max(1, len(items))) * 100, 1
        ),
    }


@app.get("/admin/coverage/benchmarks")
async def benchmarks_coverage(_: None = Depends(_require_admin)):
    """Audit benchmark coverage — curated vs derived vs none.

    BENCH-1: "derived" entries ship provider/family/context-window
    guesses so clients always render *something*. Curated entries also
    include MMLU/HumanEval/MATH scores.
    """
    from plugboard.benchmarks import get_model_info

    items = await list_models()
    curated: list = []
    derived: list = []
    none: list = []
    for m in items:
        info = get_model_info(m.get("id", ""))
        if not info:
            none.append(m.get("id", ""))
        elif info.get("info_source") == "benchmark_db":
            curated.append(m.get("id", ""))
        else:
            derived.append(m.get("id", ""))
    return {
        "total": len(items),
        "curated": len(curated),
        "derived": len(derived),
        "none": none,
        "coverage_pct": round(
            ((len(curated) + len(derived)) / max(1, len(items))) * 100, 1
        ),
    }


@app.put("/admin/pricing/{month}", status_code=200)
async def upsert_pricing(
    month: str, body: PricingUpsert, _: None = Depends(_require_admin)
):
    """
    Replace the pricing grid of a month. Rows whose price changed are marked
    ``source=manual``. Without ``models_by_provider`` the provider prices
    already in the grid are kept.
    """
    _check_month(month)
    with _billing.pricing_lock(month):
        existing = _load_grid_for_edit(month)
        pricing: Dict[str, Any] = {
            "models": _manual_rows(body.models, existing.get("models") or {}),
            "note": body.note,
        }
        spaces = existing.get("models_by_provider") or {}
        if body.models_by_provider is not None:
            spaces = {
                space: _manual_rows(rows, spaces.get(space) or {})
                for space, rows in body.models_by_provider.items()
                if rows
            }
        if spaces:
            pricing["models_by_provider"] = spaces
        _billing.save_pricing(month, pricing)
    return _billing.load_pricing(month)


# -- Admin: Usage reporting --------------------------------------------------


@app.get("/admin/usage/{month}")
async def admin_usage_month(
    month: str, page: int = 1, limit: int = 100, _: None = Depends(_require_admin)
):
    entries, total = _usage.get_usage_paginated(month, page, limit)
    return {
        "month": month,
        "page": page,
        "limit": limit,
        "total": total,
        "entries": entries,
        "summary": _usage.summarize_usage_month(
            month
        ),  # summary is always on ALL entries
    }


@app.delete("/admin/usage/{month}", status_code=204)
async def delete_usage_month(month: str, _: None = Depends(_require_admin)):
    if not _usage.delete_usage(month):
        raise HTTPException(404, f"No usage data for month '{month}'")


@app.get("/admin/usage/{month}/export")
async def export_usage(
    month: str, format: str = "json", _: None = Depends(_require_admin)
):
    """Export usage data as CSV or JSON."""
    entries = _usage.get_usage(month)
    if format == "csv":
        import csv
        import io

        output = io.StringIO()
        if entries:
            writer = csv.DictWriter(output, fieldnames=entries[0].keys())
            writer.writeheader()
            writer.writerows(entries)
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=usage_{month}.csv"},
        )
    return {
        "month": month,
        "entries": entries,
        "summary": _usage.summarize_usage(entries),
    }


@app.get("/admin/usage/{month}/key/{key_id}")
async def admin_usage_by_key(
    month: str, key_id: str, _: None = Depends(_require_admin)
):
    entries = _usage.get_usage_by_key(month, key_id)
    return {
        "month": month,
        "key_id": key_id,
        "entries": entries,
        "summary": _usage.summarize_usage(entries),
    }


@app.post("/admin/usage/purge")
async def purge_old_usage(keep_months: int = 6, _: None = Depends(_require_admin)):
    """Delete usage files older than `keep_months` months."""
    deleted = _usage.purge_old_usage(keep_months)
    return {"purged": deleted, "keep_months": keep_months}


@app.post("/admin/usage/migrate")
async def migrate_usage(_: None = Depends(_require_admin)):
    """Import existing usage_*.json files into SQLite (idempotent)."""
    count = _usage.migrate_json_to_sqlite()
    return {"migrated_entries": count}


@app.get("/admin/usage/{month}/{client_id}")
async def admin_usage_client(
    month: str, client_id: str, _: None = Depends(_require_admin)
):
    entries = _usage.get_usage_by_client(month, client_id)
    return {
        "month": month,
        "client_id": client_id,
        "entries": entries,
        "summary": _usage.summarize_usage(entries),
    }


# -- Client API contract (public) --------------------------------------------
# The documented, versioned subset an integrator may rely on (docs/api/).
# It describes public routes only, so it needs no key, unlike /openapi.json.


_client_api_contract_body: Optional[bytes] = None


@app.get("/v1/openapi.json")
async def client_api_contract():
    # Built once per process (about 10 ms): the contract only changes with
    # the code, and this route needs no key.
    global _client_api_contract_body
    if _client_api_contract_body is None:
        from plugboard.api_contract import build_openapi

        _client_api_contract_body = json.dumps(build_openapi()).encode("utf-8")
    return Response(content=_client_api_contract_body, media_type="application/json")


# -- API docs (admin-protected) -----------------------------------------------


@app.get("/openapi.json", include_in_schema=False)
async def openapi_schema(_: None = Depends(_require_admin)):
    return app.openapi()


@app.get("/docs", include_in_schema=False)
async def swagger_ui(request: Request, _: None = Depends(_require_admin)):
    from fastapi.openapi.docs import get_swagger_ui_html

    nonce = request.state.csp_nonce
    resp = get_swagger_ui_html(
        openapi_url="/openapi.json", title="Plugboard — API Docs"
    )
    # FastAPI renders a single inline <script> that calls SwaggerUIBundle({...}).
    # Stamp it with the per-request nonce so CSP can drop 'unsafe-inline'.
    body = resp.body.decode().replace("<script>", f'<script nonce="{nonce}">', 1)
    return Response(content=body, media_type="text/html")


@app.get("/redoc", include_in_schema=False)
async def redoc_ui(_: None = Depends(_require_admin)):
    from fastapi.openapi.docs import get_redoc_html

    # ReDoc's HTML has no inline <script> — only an external CDN script
    # and a <redoc> custom element. No nonce stamping needed.
    return get_redoc_html(openapi_url="/openapi.json", title="Plugboard — ReDoc")


# -- Dashboard (static files, auth-protected) --------------------------------

_STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"
_STATIC_DIR_RESOLVED = _STATIC_DIR.resolve()
_PUBLIC_DASHBOARD_HTML = {"portal.html"}

_LOGIN_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Plugboard — Login</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,sans-serif;background:#f8fafc;color:#0f172a;
display:flex;align-items:center;justify-content:center;height:100vh}
.box{background:#fff;padding:2.5rem;border-radius:18px;width:400px;max-width:92vw;
text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.07),0 8px 28px rgba(0,0,0,.08);
border:1px solid rgba(0,0,0,.08)}
.logo{width:52px;height:52px;border-radius:14px;display:inline-grid;place-items:center;
background:linear-gradient(135deg,#1e40af,#2563eb,#3b82f6);color:#fff;font-weight:800;
font-size:1.15rem;margin-bottom:1rem;box-shadow:0 4px 14px rgba(37,99,235,.3)}
h2{font-size:1.35rem;font-weight:700;margin-bottom:.25rem;letter-spacing:-.02em}
.sub{color:#94a3b8;font-size:.85rem;margin-bottom:1.5rem}
input{width:100%;padding:.55rem .85rem;margin:0 0 .5rem;border:1px solid rgba(0,0,0,.12);
border-radius:8px;font-size:.875rem;font-family:inherit;outline:none;
transition:border-color .18s,box-shadow .18s}
input:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
button{width:100%;padding:.6rem;background:#2563eb;color:#fff;border:none;
border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:600;font-family:inherit;
margin-top:.25rem;transition:background .18s;box-shadow:0 1px 3px rgba(37,99,235,.2)}
button:hover{background:#1d4ed8}
.err{color:#dc2626;font-size:.82rem;min-height:1.2em;margin-top:.25rem}
</style></head><body>
<div class="box">
<div class="logo">E</div>
<h2>Plugboard</h2>
<p class="sub">Enter your admin key to continue</p>
<form id="f">
<input type="password" id="k" placeholder="Admin Key" autofocus>
<input type="text" id="m" placeholder="MFA code (6 digits)" inputmode="numeric" pattern="[0-9]*" maxlength="6" autocomplete="one-time-code" style="display:none">
<div class="err" id="e"></div>
<button type="submit" id="b">Sign in</button>
</form>
<script nonce="__CSP_NONCE__">
var f=document.getElementById('f'),k=document.getElementById('k'),
    m=document.getElementById('m'),e=document.getElementById('e'),
    b=document.getElementById('b');
var mfaNeeded=false;
f.onsubmit=async ev=>{
  ev.preventDefault();
  e.textContent='';
  var body={key:k.value};
  if(mfaNeeded) body.totp_code=m.value.trim();
  const r=await fetch('/auth/admin/login',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)
  });
  if(r.ok){
    var raw=new URLSearchParams(location.search).get('next')||'';
    var safe=(raw && raw.charAt(0)==='/' && raw.charAt(1)!=='/') ? raw : '/dashboard';
    location.href=safe;
    return;
  }
  // Parse JSON error for the MFA-required marker; fall back to a
  // generic message if the body is empty or malformed.
  var payload={};
  try{ payload=await r.json(); }catch(_){}
  if(payload && payload.mfa_required){
    mfaNeeded=true;
    m.style.display='block';
    m.focus();
    b.textContent='Verify MFA';
    e.textContent='Enter the 6-digit code from your authenticator.';
    return;
  }
  e.textContent=(payload && payload.detail) || 'Invalid admin key';
};
</script></div></body></html>"""


class AdminLogin(BaseModel):
    key: str
    # Optional six-digit TOTP code. Required when admin_mfa.is_required().
    totp_code: Optional[str] = None


class AdminMfaSetupRequest(BaseModel):
    """Confirm an enrolment by proving the admin controls the secret."""

    secret: str
    code: str


class AdminMfaVerify(BaseModel):
    code: str


def _is_safe_static_path(candidate: pathlib.Path) -> bool:
    try:
        candidate.relative_to(_STATIC_DIR_RESOLVED)
    except ValueError:
        return False
    return True


def _check_dashboard_auth(request: Request) -> bool:
    """Check if request has valid admin auth via header or cookie."""
    # Auth fully disabled in dev mode (API_KEY and ADMIN_KEY both empty)
    if not _API_KEY and not _ADMIN_KEY:
        return True
    return _admin_request_auth(request) == "ok"


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/dashboard")


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_FLAG_ON = {"1", "true", "yes", "on"}
_FLAG_OFF = {"0", "false", "no", "off"}
# Any of these means the request went through a proxy: the TCP peer is then
# the proxy itself, so a loopback peer proves nothing about the real client.
_PROXY_HEADERS = (
    "forwarded",
    "via",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-port",
    "x-forwarded-server",
    "x-original-forwarded-for",
    "x-real-ip",
    "x-client-ip",
    "x-cluster-client-ip",
    "cf-connecting-ip",
    "true-client-ip",
    "fly-client-ip",
    "fastly-client-ip",
)
# Fetch-metadata values a browser sends when the user opens the dashboard
# itself (address bar, bookmark, or a page of the same origin). A page served
# on another localhost port sends "same-site" and is refused.
_LOCAL_NAVIGATION_SITES = {"none", "same-origin"}
# Refusals that mean someone claimed to be local without being so: audited,
# at most once per peer and reason per window, and for at most
# _REFUSAL_AUDIT_MAX_KEYS peers per window, so they cannot flood the log.
_SUSPICIOUS_REFUSALS = {"proxied", "non_loopback_peer"}
_REFUSAL_AUDIT_WINDOW_S = 600.0
_REFUSAL_AUDIT_MAX_KEYS = 256
_refusal_audit_seen: Dict[tuple, float] = {}
_warned_auto_login_values: set = set()


def _host_header_name(request: Request) -> str:
    """Hostname from the raw Host header, without port or brackets.

    Starlette's ``request.url.hostname`` falls back to the local socket
    address when the Host header is malformed, which would read as loopback.
    """
    raw = request.headers.get("host", "").strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[1:end] if end > 0 else ""
    name, sep, port = raw.rpartition(":")
    if sep and port.isdigit():
        return name
    return raw


def _should_audit_refusal(peer: str, reason: str) -> bool:
    now = time.monotonic()
    key = (peer, reason)
    last = _refusal_audit_seen.get(key)
    if last is not None and now - last < _REFUSAL_AUDIT_WINDOW_S:
        return False
    if len(_refusal_audit_seen) >= _REFUSAL_AUDIT_MAX_KEYS:
        for seen_key, seen_at in list(_refusal_audit_seen.items()):
            if now - seen_at >= _REFUSAL_AUDIT_WINDOW_S:
                del _refusal_audit_seen[seen_key]
        # Still full: many distinct peers within one window. Stop auditing new
        # ones (the log line still records them) instead of forgetting all.
        if len(_refusal_audit_seen) >= _REFUSAL_AUDIT_MAX_KEYS:
            return False
    _refusal_audit_seen[key] = now
    return True


def _is_loopback_address(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def _bind_host() -> str:
    """Interface the server really listens on, as exported by run.py.

    HOST alone is not enough: ``uvicorn --host 0.0.0.0`` ignores it. A server
    started another way counts as non-local unless auto-login is opted in.
    """
    return os.environ.get("PLUGBOARD_BIND_HOST", "")


def _auto_login_mode() -> str:
    """Return "on", "off" or "auto" from PLUGBOARD_LOCAL_AUTO_LOGIN.

    "auto" (unset or empty) allows auto-login only when the server listens on
    a loopback address, i.e. a local install. Unknown values fail closed.
    """
    raw = os.environ.get("PLUGBOARD_LOCAL_AUTO_LOGIN", "").strip().lower()
    if raw in {"", "auto"}:
        return "auto"
    if raw in _FLAG_ON:
        return "on"
    if raw not in _FLAG_OFF and raw not in _warned_auto_login_values:
        _warned_auto_login_values.add(raw)
        log.warning(
            "PLUGBOARD_LOCAL_AUTO_LOGIN=%r is not recognised; auto-login is off",
            raw,
        )
    return "off"


def _auto_login_refusal(request: Request) -> str:
    """Return why local auto-login is refused, or "" when it is allowed.

    Auto-login is for a local install opened in the operator's browser. A
    deployed instance (listening on a public interface, or behind a proxy)
    always asks for a key. The Host header is client-controlled, so it never
    counts on its own.
    """
    mode = _auto_login_mode()
    if mode == "off":
        return "disabled"
    if not _effective_admin_key():
        return "no_admin_key"
    if mode == "auto" and not _is_loopback_address(_bind_host()):
        return "not_local_install"
    if request.query_params.get("logged_out"):
        return "logged_out"
    if any(h in request.headers for h in _PROXY_HEADERS):
        return "proxied"
    peer = request.client.host if request.client else ""
    if not _is_loopback_address(peer):
        return "non_loopback_peer"
    if _host_header_name(request) not in _LOOPBACK_HOSTS:
        return "non_loopback_host"
    site = request.headers.get("sec-fetch-site", "").lower()
    navigation = request.headers.get("sec-fetch-mode", "").lower()
    if site not in _LOCAL_NAVIGATION_SITES or navigation != "navigate":
        return "not_navigation"
    from plugboard import admin_mfa as _mfa

    if _mfa.is_required():
        return "mfa_required"
    return ""


log.info(
    "auth.auto_login mode=%s local_install=%s",
    _auto_login_mode(),
    _is_loopback_address(_bind_host()),
)


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    """Public login page. Auto-login only for a local install opened locally."""
    refusal = _auto_login_refusal(request)
    peer = request.client.host if request.client else ""
    host = _host_header_name(request)
    if refusal:
        log.info("auth.auto_login refused reason=%s peer=%s", refusal, peer)
        if (
            refusal in _SUSPICIOUS_REFUSALS
            and host in _LOOPBACK_HOSTS
            and _should_audit_refusal(peer, refusal)
        ):
            # Unauthenticated path: keep the SQLite write off the event loop.
            await _asyncio.to_thread(
                _audit,
                "auth.auto_login_refused",
                details=f"reason={refusal} host={host}",
                ip=peer,
            )
    else:
        session_token = _create_admin_session_token()
        if session_token:
            _audit(
                "auth.auto_login",
                details=f"localhost auto-login host={host}",
                ip=peer,
            )
            resp = RedirectResponse(url="/dashboard", status_code=302)
            # Auto-login only serves plain-http localhost (refused otherwise),
            # so the cookie cannot carry the Secure flag.
            resp.set_cookie(  # NOSONAR
                _ADMIN_COOKIE_NAME,
                session_token,
                max_age=_ADMIN_SESSION_MAX_AGE,
                httponly=True,
                samesite="strict",
                secure=False,
                path="/",
            )
            return resp
    nonce = request.state.csp_nonce
    html = _LOGIN_HTML_TEMPLATE.replace("__CSP_NONCE__", nonce)
    return Response(content=html, media_type="text/html")


# Every path returns its own Response (204 on success, 401/429 otherwise).
@app.post("/auth/admin/login", include_in_schema=False)
async def admin_login(body: AdminLogin, request: Request):
    from plugboard.ratelimit import (
        check_auth_rate_limit,
        get_lockout_remaining,
        record_auth_failure,
    )

    client_ip = request.client.host if request.client else "unknown"

    # Brute-force protection: check lockout before processing
    if not check_auth_rate_limit(client_ip):
        remaining = get_lockout_remaining(client_ip)
        _audit(
            "auth.login_blocked", details=f"IP locked out: {client_ip}", ip=client_ip
        )
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many failed attempts. Try again later."},
            headers={"Retry-After": str(remaining)},
        )

    token = body.key.strip()
    if not _is_admin_key(token):
        locked = record_auth_failure(client_ip)
        _audit(
            "auth.login_failed",
            details=f"IP: {client_ip}, locked={locked}",
            ip=client_ip,
        )
        log.warning("Failed admin login attempt from %s (locked=%s)", client_ip, locked)
        if locked:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many failed attempts. Try again later."},
                headers={"Retry-After": "900"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
        )

    # TM-001: enforce second factor when MFA is required.
    # is_required() is only true when the operator set the env flag AND
    # a secret is already enrolled, so a fresh deploy can't lock itself out.
    from plugboard import admin_mfa as _mfa

    mfa_checked = _mfa.is_required()
    if mfa_checked:
        code = (body.totp_code or "").strip()
        if not code:
            # Distinct 401 shape so the login UI knows to prompt for the
            # second factor rather than telling the user their key is bad.
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "MFA code required", "mfa_required": True},
            )
        if not _mfa.verify(code):
            locked = record_auth_failure(client_ip)
            _audit(
                "auth.mfa_failed",
                details=f"IP: {client_ip}, locked={locked}",
                ip=client_ip,
            )
            log.warning("Failed admin MFA from %s (locked=%s)", client_ip, locked)
            if locked:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many failed attempts. Try again later."},
                    headers={"Retry-After": "900"},
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid MFA code",
            )
        _audit("auth.mfa_success", details=f"IP: {client_ip}", ip=client_ip)

    session_token = _create_admin_session_token(mfa=mfa_checked)
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
        )

    _audit("auth.login_success", details=f"IP: {client_ip}", ip=client_ip)

    resp = Response(status_code=204)
    resp.set_cookie(
        _ADMIN_COOKIE_NAME,
        session_token,
        max_age=_ADMIN_SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_should_use_secure_cookie(request),
        path="/",
    )
    resp.delete_cookie("llm_admin_key", path="/")
    return resp


@app.post("/auth/admin/logout", include_in_schema=False, status_code=204)
async def admin_logout(request: Request):
    """Revoke this browser's session server-side, then clear its cookie."""
    nonce = _admin_session_nonce(request.cookies.get(_ADMIN_COOKIE_NAME, ""))
    if nonce and admin_sessions.revoke(nonce):
        _audit("auth.logout", details="admin session revoked")
    resp = Response(status_code=204)
    resp.delete_cookie(_ADMIN_COOKIE_NAME, path="/")
    resp.delete_cookie("llm_admin_key", path="/")
    return resp


# ── Admin MFA (TM-001) ──────────────────────────────────────────────────────


@app.get("/admin/mfa/status", include_in_schema=False)
async def admin_mfa_status(_: None = Depends(_require_admin)):
    """Expose MFA configuration so the dashboard can render the right UI."""
    from plugboard import admin_mfa as _mfa

    return {
        "enrolled": _mfa.is_enrolled(),
        "enforced": _mfa.is_required(),
        "flag_set": os.environ.get("PLUGBOARD_ADMIN_MFA_REQUIRED", "").strip() == "1",
    }


@app.post("/admin/mfa/begin", include_in_schema=False)
async def admin_mfa_begin(_: None = Depends(_require_admin)):
    """Step 1 of enrolment: server generates a fresh TOTP secret and the
    otpauth:// provisioning URI. Nothing is persisted yet — the caller must
    confirm with /admin/mfa/setup and a valid code from their authenticator.
    """
    from plugboard import admin_mfa as _mfa

    secret = _mfa.generate_secret()
    return {
        "secret": secret,
        "otpauth_uri": _mfa.provisioning_uri(secret, account="admin"),
    }


@app.post("/admin/mfa/setup", include_in_schema=False, status_code=204)
async def admin_mfa_setup(
    body: AdminMfaSetupRequest,
    request: Request,
    _: None = Depends(_require_admin),
):
    """Step 2 of enrolment: the admin submits the secret from /begin plus a
    code read from their authenticator app. On success the secret is
    Fernet-encrypted and written to disk. Subsequent logins will demand
    TOTP codes once PLUGBOARD_ADMIN_MFA_REQUIRED=1."""
    from plugboard import admin_mfa as _mfa

    # Validate the code matches the *submitted* secret (not any previously
    # stored one — we might be rotating). We don't call _mfa.verify() here
    # because that reads from disk. The code is then recorded as used.
    step = _mfa.setup_code_step(body.secret, (body.code or "").strip())
    if step is None:
        raise HTTPException(400, "TOTP code does not match the secret")
    _mfa.enroll(body.secret, used_step=step)
    _audit(
        "admin.mfa.enrolled",
        target_type="admin",
        target_id="admin",
        ip=request.client.host if request.client else "",
    )


@app.post("/admin/mfa/verify", include_in_schema=False, status_code=204)
async def admin_mfa_verify(body: AdminMfaVerify, _: None = Depends(_require_admin)):
    """Validate a code against the currently enrolled secret (debug helper)."""
    from plugboard import admin_mfa as _mfa

    if not _mfa.verify((body.code or "").strip()):
        raise HTTPException(400, "Invalid TOTP code")


@app.delete("/admin/mfa", include_in_schema=False, status_code=204)
async def admin_mfa_disable(request: Request, _: None = Depends(_require_admin)):
    """Remove the stored TOTP secret. Useful for rotation or to disable MFA."""
    from plugboard import admin_mfa as _mfa

    _mfa.disable()
    _audit(
        "admin.mfa.disabled",
        target_type="admin",
        target_id="admin",
        ip=request.client.host if request.client else "",
    )


@app.get("/portal", include_in_schema=False)
async def portal_redirect():
    return RedirectResponse("/dashboard/portal.html")


@app.get("/dashboard/{path:path}", include_in_schema=False)
async def serve_dashboard(path: str, request: Request):
    """Serve static files — CSS/JS/assets are public, HTML requires admin auth."""
    path = (path or "").lstrip("/")
    if not path or path == "/":
        path = "index.html"

    # CSS, JS, images are non-sensitive — serve without auth
    _public_suffixes = {
        ".css",
        ".js",
        ".png",
        ".svg",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
    }
    suffix = pathlib.Path(path).suffix.lower()
    if suffix not in _public_suffixes and path not in _PUBLIC_DASHBOARD_HTML:
        if not _check_dashboard_auth(request):
            next_url = f"/dashboard/{path}" if path else "/dashboard"
            return RedirectResponse(f"/login?next={next_url}")

    # Serve the file
    file_path = (_STATIC_DIR / path).resolve()
    if not _is_safe_static_path(file_path) or not file_path.is_file():
        raise HTTPException(404, "Not found")
    suffix = file_path.suffix.lower()
    media_types = {
        ".html": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }
    return Response(
        content=file_path.read_bytes(),
        media_type=media_types.get(suffix, "application/octet-stream"),
    )


@app.get("/dashboard", include_in_schema=False)
async def dashboard_root(request: Request):
    """Redirect /dashboard to /dashboard/ or login."""
    if not _check_dashboard_auth(request):
        return RedirectResponse("/login?next=/dashboard")
    file_path = _STATIC_DIR / "index.html"
    if not file_path.is_file():
        raise HTTPException(404, "Dashboard not found")
    return Response(content=file_path.read_bytes(), media_type="text/html")
