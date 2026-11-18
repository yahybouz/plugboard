"""
Plugboard -- self-hosted LLM gateway: one API for every provider.

Endpoints:
  POST /v1/chat/completions    -- proxy to configured provider (streaming supported)
  POST /v1/embeddings          -- embeddings from a local engine (client key)
  GET  /v1/models              -- list all available models
  GET  /health                 -- liveness check
  GET  /health/ready           -- readiness: 200 when able to serve, else 503
                                   (its checks for a valid key only, PLU-75)
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
import functools
import json
import math
import logging
import os
import pathlib
import re
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    TypeVar,
)

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware

from plugboard import __version__
from plugboard import admin_people
from plugboard import admin_sessions
from plugboard import audit as _audit_mod
from plugboard import envcompat
from plugboard import netguard
from plugboard import billing as _billing
from plugboard import usage as _usage
from plugboard.alerts import check_and_alert as _check_and_alert
from plugboard.audit import log_action as _audit
from plugboard.apikeys import (
    KEY_PREFIXES,  # ARCH-DEBT-029
    get_key_record,
    get_keys_by_client,
    load_keys,
    mask_key,
)
from plugboard.config import (
    dotenv_permission_warning,
    get_provider,
    load_providers,
    update_provider,  # noqa: F401  (patch point read through core, PLU-36)
)
from plugboard import live_requests as _live
from plugboard import provider_admission as _provider_admission
from plugboard import request_wait as _request_wait
from plugboard.logging_config import _request_id_var, setup_logging
from plugboard import chat_billing
from plugboard.storage import to_thread_to_the_end as _to_thread_to_the_end
from plugboard.metrics import get_stats, record_busy_refusal, record_request_wait
from plugboard.ratelimit import (
    check_client_rate_limit,
    check_rate_limit,
    rate_limit_on,
)
from plugboard.decision_api import router as decision_router
from plugboard.embeddings_api import router as embeddings_router
from plugboard.admin_people_api import build_router as build_people_router
from plugboard.admin_routes import model_probes as _admin_model_probes
from plugboard.admin_routes import monitoring as _admin_monitoring
from plugboard.admin_routes import providers as _admin_providers
from plugboard.admin_routes import oauth as _admin_oauth
from plugboard.admin_routes import direct_providers as _admin_direct_providers
from plugboard.admin_routes import ollama as _admin_ollama
from plugboard.admin_routes import keys as _admin_keys
from plugboard.admin_routes import jobs as _admin_jobs
from plugboard.admin_routes import pricing as _admin_pricing
from plugboard.admin_routes import usage as _admin_usage
from plugboard.suivi_api import build_router as build_suivi_router
from plugboard.legacy_connectors import (
    REMOVED_CONNECTOR_ERROR,
    require_supported_model,
    require_supported_provider,
)
from plugboard.decisions import is_decision_model
from plugboard.api_contract import CHAT_ROLES, MAX_OUTPUT_TOKENS_CAP
from plugboard.cli_auth import AuthError
from plugboard.cli_limits import UNSUPPORTED_ON_CLI
from plugboard.cli_subprocess import CliSubprocessError, ModelNotServedError
from plugboard.router import (
    ProviderDisabledError,
    ProviderHTTPError,
    ProviderNetworkError,
    ProvidersBusyError,
    ProviderQuotaError,
    ProviderTimeoutError,
    UnsupportedOnCliError,
    chat_routes,
    cli_refusal,
    disabled_route,
    discover_models,  # noqa: F401  (patch point read through core, PLU-36)
    forward,
    forward_stream,
    invalidate_models_cache,  # noqa: F401  (patch point read through core, PLU-36)
    is_unknown_model,
    is_unserved_cli_model,
    list_models,
    resolve_provider_and_model,
    served_by as _served_by,
    usage_reported as _usage_reported,
)
from plugboard.tokenizer import (
    count_tokens,
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
    0, envcompat.get_int("PLUGBOARD_CACHE_RESPONSE_FLOOR_MS", default=0)
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
#   the warning fires once at boot and `/health/ready` reports
#   `checks.auth: "dev"` with a 200.
# * Any other context (no DEV flag) = treat as production: warning is
#   promoted to CRITICAL, repeated periodically by the lifespan, and
#   `/health/ready` answers 503 with `checks.auth: "fail"` so load
#   balancers / deployment gates can refuse traffic to the instance.
#   The public `/health` never shows the auth posture (PLU-31), and
#   `/health/ready` shows its `checks` to a valid key only (PLU-75).
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
    # The documentation writes values to fill in as `<...>`
    # (`ADMIN_KEY=<openssl rand -hex 24>`): copied as is, that public text
    # passes the length check.
    stripped = (admin_key or "").strip()
    if not dev_mode and stripped.startswith("<") and stripped.endswith(">"):
        raise RuntimeError(
            "ADMIN_KEY is a placeholder written as <...> in the documentation: "
            "set a random value (openssl rand -hex 24) and restart."
        )


_require_admin_key_outside_dev(_ADMIN_KEY, _DEV_MODE)
_ADMIN_SESSION_MAX_AGE = envcompat.get_int("ADMIN_SESSION_MAX_AGE", default=28800)  # 8h
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


def trusted_proxy_warning(value: str) -> Optional[str]:
    """Why FORWARDED_ALLOW_IPS is unsafe or partly ignored, or None (BUG-56).

    uvicorn takes the client IP from X-Forwarded-For only for a peer listed
    there: ``*`` or a ``/0`` network lists every peer, so any client can
    choose its IP; an entry that is neither an address nor a network is
    silently ignored by uvicorn.
    """
    import ipaddress

    entries = [part.strip() for part in value.split(",") if part.strip()]
    trust_all, ignored = [], []
    for entry in entries:
        if entry == "*":
            trust_all.append(entry)
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            ignored.append(entry)
            continue
        if network.prefixlen == 0:
            trust_all.append(entry)
    if trust_all:
        return (
            f"FORWARDED_ALLOW_IPS trusts every peer ({', '.join(trust_all)}): any "
            "client can set its IP with X-Forwarded-For and so dodge the per-IP "
            "rate limit and the admin-login lockout. List only the reverse "
            "proxy's address."
        )
    if ignored:
        return (
            f"FORWARDED_ALLOW_IPS has entries that are neither an address nor a "
            f"network ({', '.join(ignored)}): uvicorn ignores them, so a proxy "
            "meant there is not trusted and its clients share one rate limit "
            "and one admin-login lockout."
        )
    return None


def _warn_trusted_proxies() -> None:
    message = trusted_proxy_warning(os.environ.get("FORWARDED_ALLOW_IPS", ""))
    if message:
        log.warning(message)


_warn_trusted_proxies()


def _check_stored_secrets() -> None:
    """Say at boot which stored secrets ENCRYPTION_KEY cannot decrypt (BUG-57).

    Never stops the boot: the providers concerned are turned off (and
    audited and alerted by load_providers), the rest keeps working.
    """
    from plugboard import config as _config
    from plugboard.secrets_rekey import unreadable_secrets

    report = unreadable_secrets(_config.PROVIDERS_FILE.parent, _config._ENCRYPTION_KEY)
    damaged = report.damaged
    if damaged:
        # Not a key problem: changing the key would not help.
        log.critical(
            "Stored secrets cannot be read, whatever the key: %s. Restore these "
            "files from a backup.",
            "; ".join(damaged),
        )
    unreadable = [where for where in report.unreadable if where not in damaged]
    if unreadable:
        log.critical(
            "ENCRYPTION_KEY cannot decrypt %d stored secret(s): %s. Restore the "
            "key that encrypted them (ENCRYPTION_KEY or data/.encryption_key) and "
            "restart, or enter these secrets again. Until then the providers "
            "concerned are turned off and none of these secrets is sent.",
            len(unreadable),
            "; ".join(unreadable),
        )
        load_providers()  # reports each provider turned off (audit, alert)


@asynccontextmanager
async def _lifespan(application: FastAPI):  # noqa: ARG001
    """Start background services on startup, stop on shutdown."""
    _issue_local_login_token()
    try:
        _check_stored_secrets()
    except Exception as exc:  # the check must never stop the boot
        log.error("Stored secrets check failed: %s", exc)
    # Auto-start provider watchdog
    from plugboard.provider_watchdog import start_watchdog, stop_watchdog

    start_watchdog()
    log.info("Provider watchdog auto-started")
    # Auto-register Ollama if detected
    from plugboard.ollama import bootstrap_ollama

    ollama_prov = await bootstrap_ollama()
    if ollama_prov:
        log.info(
            "Ollama auto-registered: %s (%s) active=%s",
            ollama_prov["id"],
            ollama_prov.get("base_url"),
            ollama_prov.get("is_active", True),
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

    # Load the tiktoken encoding (read from its cache, or downloaded) now,
    # off the event loop: the first token count of a request would block it.
    from plugboard.tokenizer import warm_up_in_background

    warm_up_in_background()

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

    # Maintenance jobs — availability snapshots and the daily retention pass.
    from plugboard.maintenance import availability_job, retention_job

    _maintenance_jobs = [availability_job(), retention_job()]
    app.state.availability_job, app.state.retention_job = _maintenance_jobs
    for _job in _maintenance_jobs:
        await _job.start()
        log.info(
            "PeriodicJob %s started (disabled=%s, interval_s=%s, daily_hour_utc=%s)",
            _job.name,
            _job.disabled,
            _job.interval_s,
            _job.daily_hour_utc,
        )

    # Boot-time internet probe — auto-refresh internet_access flags so the
    # playground dropdown displays correct 🌐/💻 icons without admin click.
    # Runs in background after schedulers are up so boot isn't blocked.
    # It sends a chat request to every model: off by default (BUG-45),
    # opt-in via PLUGBOARD_INTERNET_CHECK_ON_BOOT=1.
    # An accept-list: now that the check is opt-in, "off" or "False" must
    # not turn it on.
    _internet_boot_enabled = os.environ.get(
        "PLUGBOARD_INTERNET_CHECK_ON_BOOT", ""
    ).strip().lower() in ("1", "true", "yes", "on")

    async def _internet_boot_probe():
        # Small delay so warm-up + ollama sync settle first, and so a quick
        # restart loop doesn't immediately burn quota.
        await _asyncio.sleep(5.0)
        try:
            log.info("internet_check_on_boot: starting bulk probe (persist=True)")
            # Local Ollama models have no web access, and probing them makes
            # the host load and run each installed model in turn.
            summary = await check_all_models_internet_access(
                {"persist": True, "skip_local": True}
            )
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
            "internet_check_on_boot: off (PLUGBOARD_INTERNET_CHECK_ON_BOOT=1 turns it on)"
        )

    yield

    for _job in _maintenance_jobs:
        await _job.stop()
    await _model_availability_scheduler.stop()
    await _pricing_scheduler.stop()
    # The watchdog first: it can raise a provider_down alert. Then the
    # webhooks still being delivered (changes, alerts) share one grace period.
    stop_watchdog()
    log.info("Provider watchdog stopped")
    from plugboard import alerts as _alerts
    from plugboard import change_events

    try:
        await _asyncio.wait_for(
            _asyncio.gather(change_events.drain(), _alerts.drain()), timeout=10
        )
    except _asyncio.TimeoutError:
        log.warning("Shutdown: some change or alert webhooks were not delivered")
    if _nag_task:
        _nag_task.cancel()
    for task in _boot_tasks:
        task.cancel()
    # Close the shared HTTP pool so pooled sockets don't leak at shutdown.
    from plugboard.http_pool import close_session

    await close_session()
    log.info("HTTP pool closed")


_MONTH_RE = re.compile(r"^\d{4}_(0[1-9]|1[0-2])$")
_MONTH_DASH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def _month_param(month: str) -> str:
    """``{month}`` path parameter: ``YYYY_MM`` (storage form) or ``YYYY-MM``.

    ``2026-11`` used to answer an empty month (usage) or a 404 (pricing)
    because only ``2026_11`` exists in the store. Anything else is a 422.
    """
    m = _MONTH_DASH_RE.match(month or "")
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    if not _MONTH_RE.match(month or ""):
        raise HTTPException(422, "month must look like YYYY_MM or YYYY-MM")
    return month


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
app.include_router(embeddings_router)

# The module object handed to plugboard.admin_routes.*.register (PLU-36): each
# import of this file binds its own admin handlers to itself.
_THIS_MODULE = sys.modules[__name__]


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


_MAX_BODY_BYTES = envcompat.get_int(
    "MAX_BODY_BYTES", default=10 * 1024 * 1024
)  # 10 MB default


# ── Inflight cap on /v*/chat/completions (TM-003) ───────────────────────────
#
# A burst of concurrent chat requests — each one possibly fanning out to a
# subprocess CLI and eating RAM — can DoS the instance before any per-IP or
# per-key limit kicks in. PLUGBOARD_MAX_INFLIGHT_CHAT (default 8, 0 = off)
# enforces a hard global cap: once saturated, additional requests wait up
# to PLUGBOARD_INFLIGHT_WAIT_S (default 10 s) for a slot and then get 503 +
# Retry-After. With one chat and no wait, a second application got 503 at
# once, before the provider queue could hold it (D-32, PLU-14).


_INFLIGHT_CHAT_MAX = envcompat.get_int(
    "PLUGBOARD_MAX_INFLIGHT_CHAT", default=8, minimum=0
)
_INFLIGHT_WAIT_S = envcompat.get_float(
    "PLUGBOARD_INFLIGHT_WAIT_S", default=10.0, minimum=0.0
)
_INFLIGHT_RETRY_AFTER_S = 5
_inflight_chat_sem: Optional["_request_wait.ChatCap"] = None
_inflight_chat_active = 0  # observability counter; only accurate when sem is used


def _get_inflight_sem() -> Optional["_request_wait.ChatCap"]:
    """The global cap, created on first use (``request_wait.ChatCap``: first
    come first served, a request holding a provider's slot first, PLU-48)."""
    global _inflight_chat_sem
    if _INFLIGHT_CHAT_MAX <= 0:
        return None
    if _inflight_chat_sem is None:
        _inflight_chat_sem = _request_wait.ChatCap(_INFLIGHT_CHAT_MAX)
    return _inflight_chat_sem


class _BusyRefusal(HTTPException):
    """A 503 that tells a program why with ``code``, and when to retry.

    An HTTPException, so the handlers that let an HTTPException raised by the
    router through do the same with these (a slot taken back after a
    provider's wait, BUG-77); their handler adds ``code`` and ``retry_after``.
    """

    code: str  # set by each subclass

    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=503,
            detail=detail,
            headers={"Retry-After": str(_INFLIGHT_RETRY_AFTER_S)},
        )


class _ChatOverloaded(_BusyRefusal):
    """503 ``server_busy``: the global chat cap stayed full for the whole wait."""

    code = "server_busy"

    def __init__(self) -> None:
        super().__init__(
            "Server overloaded: the limit of concurrent chat requests is "
            f"reached, retry in {_INFLIGHT_RETRY_AFTER_S} s"
        )


class _SessionBusy(_BusyRefusal):
    """503 ``session_busy``: the session's previous turn was still being
    answered when the request's wait ended (PLU-45)."""

    code = "session_busy"

    def __init__(self) -> None:
        super().__init__(
            "The previous turn of this session is still being answered: "
            f"retry in {_INFLIGHT_RETRY_AFTER_S} s"
        )


@app.exception_handler(_BusyRefusal)
async def _busy_refusal_handler(request: Request, exc: _BusyRefusal):
    content: Dict[str, Any] = {"detail": exc.detail, "code": exc.code}
    headers = _retry_after_headers(content, _INFLIGHT_RETRY_AFTER_S)
    return JSONResponse(status_code=503, content=content, headers=headers)


class _ChatSlot:
    """A request's slot under the global cap (``request_wait.ChatSlot``).

    The request gives it back while it waits for a provider or for its
    session's turn (BUG-77), so requests waiting for a full provider no
    longer keep out the requests for the other ones, and takes it back right
    before a provider is called (``request_wait.slot_back``, PLU-51), ahead
    of the requests that hold no scarce provider's slot (PLU-48).
    """

    __slots__ = ("sem", "rid", "held", "wait_s")

    def __init__(self, sem: Any, rid: str) -> None:
        self.sem = sem
        self.rid = rid
        self.held = False
        self.wait_s = _INFLIGHT_WAIT_S

    async def take(self, wait_s: float, first: bool = False) -> None:
        global _inflight_chat_active
        # A plain asyncio semaphore (a test's) has no front of the line.
        if first and isinstance(self.sem, _request_wait.ChatCap):
            acquire, waits = self.sem.acquire_first, self.sem.would_wait(True)
        else:
            acquire, waits = self.sem.acquire, self.sem.locked()
        if waits:
            if wait_s <= 0:
                raise _ChatOverloaded()
            _live.update(self.rid, phase="waiting_slot")
            try:
                await _asyncio.wait_for(acquire(), timeout=wait_s)
            except _asyncio.TimeoutError:
                raise _ChatOverloaded() from None
            _live.update(self.rid, phase="running")
        else:
            await acquire()  # free: taken at once, without suspending
        self.held = True
        _inflight_chat_active += 1

    def release(self) -> None:
        global _inflight_chat_active
        if self.held:
            self.held = False
            _inflight_chat_active -= 1
            self.sem.release()


async def _inflight_chat_dep(request: Request):
    """FastAPI dependency enforcing the global inflight cap.

    Uses the yield-style contract: code before `yield` runs before the
    handler, code after `yield` runs after the response body is fully
    delivered (including streaming chunks). That means a StreamingResponse
    keeps its slot for the entire stream duration — critical for DoS
    protection, since streaming is where the real resource cost lives.

    PLU-15: every route lists ``Depends(_resolve_client)`` before this one,
    so a call without a valid key gets its 401 without taking or waiting for
    a slot. The request's wait budget (``request_wait``) starts here: the
    waits for a slot, for the session's turn and for a provider share it.
    BUG-77: the slot is given back while the request waits for a provider or
    for its session's turn, and taken back right before a provider is called
    (``_ChatSlot``, PLU-51).
    """
    # PLU-13: every chat call is listed in /v1/inflight, cap or not.
    rid = _request_id_var.get()
    _live.begin(rid, request.url.path, request.headers.get(_live.SURFACE_HEADER))
    slot: Optional[_ChatSlot] = None
    budget: Optional[_request_wait.RequestWait] = None
    try:
        sem = _get_inflight_sem()
        slot = _ChatSlot(sem, rid) if sem is not None else None
        budget = _request_wait.start(
            max(_INFLIGHT_WAIT_S, _provider_admission.WAIT_S), slot=slot
        )
        if slot is not None:
            with _request_wait.waiting(_INFLIGHT_WAIT_S) as wait_s:
                await slot.take(wait_s)
        yield
    finally:
        if slot is not None:
            slot.release()
        if budget is not None:
            # PLU-46: all the request waited, once it ends (a stream after
            # its last byte, a refusal just before its 503 goes out).
            record_request_wait(budget.spent)
        _live.finish(rid)


# ── Client hang-up while the upstream works (BUG-30) ────────────────────────
#
# The inflight slot is given back only once the response is sent, and nothing
# else listens to the client before the response starts: an abandoned call
# kept its slot, its CLI and its HTTP request until the upstream answered (up
# to 900 s for a CLI), then was billed and cached for nobody.

_CLIENT_CLOSED_REQUEST = 499  # nginx's code for "client closed request"
_T = TypeVar("_T")


class _ClientGone(Exception):
    """The caller hung up before the upstream answered."""


async def _unless_client_gone(request: Request, awaitable: Awaitable[_T]) -> _T:
    """Await upstream work, and cancel it as soon as the caller hangs up.

    The work runs in the calling task on purpose: ``forward()`` sets the
    router's context variables that billing reads afterwards. A watcher task
    cancels this task on ``http.disconnect``; the cancellation kills a CLI's
    process group (cli_subprocess) and closes an HTTP request, then becomes
    ``_ClientGone``. Only valid once the request body has been read.
    """
    task = _asyncio.current_task()
    if task is None:  # pragma: no cover - always called from a request task
        return await awaitable
    hung_up = False
    finished = False

    async def _watch() -> None:
        nonlocal hung_up
        try:
            message = await request.receive()
        except Exception as exc:  # the server cannot tell: keep the old way
            log.debug("Client watch stopped: %s", exc)
            return
        # After the body, an ASGI server only ever sends http.disconnect.
        # Once the work is over, a hang-up changes nothing: BaseHTTPMiddleware's
        # receive (an anyio task group) can swallow watcher.cancel() and still
        # return the disconnect, which must not cancel the finished request.
        if message.get("type") == "http.disconnect" and not finished:
            hung_up = True
            task.cancel()

    watcher = _asyncio.ensure_future(_watch())
    try:
        result = await awaitable
    except (_asyncio.CancelledError, Exception):
        # Withdraw our own cancellation request (the work may also have turned
        # it into an error); any other cancellation keeps propagating.
        if hung_up and task.uncancel() == 0:
            raise _ClientGone() from None
        raise
    finally:
        finished = True  # before the first await: see _watch
        watcher.cancel()
        await _asyncio.wait([watcher])
    if hung_up:
        # The work swallowed the cancellation: the caller is gone all the same.
        task.uncancel()
        raise _ClientGone()
    return result


_EVENT_ENDS = (b"\n\n", b"\r\n\r\n")


_DONE = b"data: [DONE]"
# Held without an event end past this, a provider's stream goes out as it
# comes (all but its last bytes): a stream that never ends its events must
# not be buffered whole.
_MAX_HELD = 1 << 16


def _done_event_at(buf: bytearray) -> int:
    """Where the `data: [DONE]` event starts in ``buf``, -1 when there is
    none. Only a whole event counts: at the start of ``buf`` (which starts
    at an event's start) or right after an event end, followed by a line
    end or nothing yet; `data: [DONE]` quoted inside an event's JSON does
    not."""
    end = len(buf)
    while True:
        at = buf.rfind(_DONE, 0, end)
        if at == -1:
            return -1
        after = buf[at + len(_DONE) : at + len(_DONE) + 1]
        starts_event = at == 0 or any(buf.endswith(sep, 0, at) for sep in _EVENT_ENDS)
        if starts_event and after in (b"", b"\n", b"\r"):
            return at
        end = at


def _complete_events(buf: bytearray) -> bytes:
    """Take from ``buf`` the complete SSE events that may go out now: up to
    the last event end, but never the `data: [DONE]` event nor what follows
    it. What is left stays in ``buf``."""
    done = _done_event_at(buf)
    limit = len(buf) if done == -1 else done
    end = 0
    for sep in _EVENT_ENDS:
        at = buf.rfind(sep, 0, limit)
        if at != -1:
            end = max(end, at + len(sep))
    if not end and done == -1 and len(buf) > _MAX_HELD:
        end = len(buf) - 64  # no event end in sight: let it flow
    if not end:
        return b""
    out = bytes(buf[:end])
    del buf[:end]
    return out


def _stream_cut_event() -> bytes:
    """The SSE event that ends a stream the provider broke off after its
    first chunk (PLU-26), in OpenAI's streamed error shape."""
    event = {
        "error": {
            "message": (
                "The provider stopped the answer before its end: what came "
                "before this event is all there is."
            ),
            "type": "server_error",
            "code": "stream_interrupted",
        }
    }
    return f"data: {json.dumps(event)}\n\n".encode("utf-8")


def _client_gone_response(model: str) -> Response:
    """Answer nobody reads: it only lets the inflight slot go at once."""
    log.info(
        "Client hung up before the answer: upstream stopped, nothing billed | model=%s",
        model,
    )
    return Response(status_code=_CLIENT_CLOSED_REQUEST)


# ── One turn at a time per session (D-24, PLU-8) ────────────────────────────


async def _session_turn_dep(request: Request):
    """Give the session's turn back once the response is sent, and the
    budget hold of a call that was not billed (BUG-52).

    The handler takes the turn (``_take_session_turn``) only when the call
    carries a session id; a yield dependency (scope "request") exits after
    the last byte, streams included, and on any error the handler raises.
    """
    try:
        yield
    finally:
        # The hold first: the session's next turn must not see it.
        hold = getattr(request.state, "budget_hold", None)
        if hold is not None and not hold.done:
            await _asyncio.to_thread(hold.release)
        release = getattr(request.state, "session_turn_release", None)
        if release is not None:
            release()


async def _take_session_turn(request: Request, client_id: str, session_id: str) -> None:
    """Wait for the previous turn of this session to be answered and recorded.

    The release is stored as soon as the turn is obtained, so a hang-up that
    comes later (``_ClientGone``) cannot lose it. The wait takes what is left
    of the request's wait budget (PLU-15): then 503 ``session_busy`` with
    Retry-After (PLU-45), out of the queue. A free turn is taken at once,
    even with no budget left. While the turn runs elsewhere, the request
    gives its global slot back (BUG-77).
    """
    from plugboard import session_turns

    if not session_turns.busy(client_id, session_id):
        request.state.session_turn_release = await session_turns.acquire(
            client_id, session_id
        )
        return
    # BUG-77: no global slot held while the turn runs elsewhere; it is taken
    # back right before a provider is called (PLU-51).
    _request_wait.release_slot()
    try:
        with _request_wait.waiting() as timeout:
            request.state.session_turn_release = await session_turns.acquire(
                client_id, session_id, timeout=timeout
            )
    except session_turns.QueueFull:
        log.info("session turn busy: %d turns already wait", session_turns.MAX_WAITING)
        raise _SessionBusy() from None
    except TimeoutError:
        log.info("session turn busy: the request's wait ended")
        raise _SessionBusy() from None


def _record_session_turn(
    client_id: Optional[str],
    session_id: Optional[str],
    messages: List[dict],
    reply: str,
) -> None:
    """PERF-1: record the exchange so the next call with the same session_id
    sees the updated transcript. Non-blocking: never bubbles errors."""
    if not (session_id and client_id):
        return
    try:
        from plugboard.session_store import record_exchange

        record_exchange(client_id, session_id, messages, reply)
    except Exception as exc:
        log.warning("session record error (non-fatal): %s", exc)


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
        # This request's own principal holder (PB-AUTH-01): the admin
        # authentication fills it, every audit entry of the request reads it.
        # A streamed body still sees it after this function returns.
        principal_token = _audit_mod.begin_request()
        try:
            try:
                response = await call_next(request)
            except Exception:
                _live.set_status(rid, 500)
                raise
            response.headers["X-Request-ID"] = rid
            _live.set_status(rid, response.status_code)
            return response
        finally:
            _audit_mod.end_request(principal_token)
            _request_id_var.reset(token)


_ADMIN_SCOPE = "key:admin"
_ADMIN_KEY_PRINCIPAL = "admin-key"


def _person_principal(person: Dict[str, Any]) -> str:
    """The audit principal of a personal key: ``person:<id>``."""
    return "person:" + person["id"]


def _person_scope(person_id: str) -> str:
    """Lockout counter of one person's codes: ``key:admin:<id>``."""
    return f"{_ADMIN_SCOPE}:{person_id}"


def _set_principal(request: Request, principal: str, name: str = "") -> None:
    """Name the admin credential of this request, for the audit and the routes."""
    request.state.admin_principal = principal
    request.state.admin_principal_name = name
    _audit_mod.set_principal(principal, name)


def _client_address(request: Request) -> str:
    """The client address: uvicorn's ``request.client.host``.

    uvicorn replaces the peer with the right-most ``X-Forwarded-For`` entry
    that is not a trusted proxy, and only when the peer is listed in
    ``FORWARDED_ALLOW_IPS`` (``uvicorn/middleware/proxy_headers.py``).
    Plugboard reads no header for it.
    """
    return request.client.host if request.client else "unknown"


def _address_scope(request: Request) -> str:
    """Counter of the client address, for the limit and the key lockout."""
    return "ip:" + _client_address(request)


def _key_scope(request: Request) -> str:
    """The valid key a request carries, as ``key:<key_id>``, else "".

    ``key:admin`` for the admin key or an admin session, ``key:<key_id>`` for
    a client key the auth dependency accepts, "" otherwise (no key, a wrong,
    forged or revoked key). Never raises and logs no refusal (BUG-119). A
    session opened with a personal key is ``key:admin:<id>``, that person's
    own scope; a bare personal key is not counted here (it shows no checks
    without its code), see ``_person_bearer_scope``.

    Same checks as ``_resolve_client``, in its order, with no file read in
    the common case: the admin key is a constant-time comparison; a client
    key is looked up in the key cache (one ``stat``, the file is read only
    when it changed); a session cookie is checked against its signature
    first, so only a correctly signed one is looked up in the session store.
    The admin key needs no TOTP here: callers only count or show generic
    states with it, never act as the admin.
    """
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if _is_admin_key(token):
        return _ADMIN_SCOPE
    if token.startswith(KEY_PREFIXES):
        # A client key wins over the session cookie, as in _resolve_client.
        rec = get_key_record(token, log_refused=False)
        key_id = str(rec.get("key_id") or "") if rec else ""
        return f"key:{key_id}" if key_id else ""
    session = request.cookies.get(_ADMIN_COOKIE_NAME, "")
    if not _admin_session_nonce(session):
        return ""
    found = _admin_session_principal(session)
    if found is None:
        return ""
    principal = found[0]
    if principal == _ADMIN_KEY_PRINCIPAL:
        return _ADMIN_SCOPE
    # The id needs no check here: the principal names a person that
    # ``admin_people.get`` found, and ``get`` refuses any id outside
    # ``ID_PATTERN`` (that is where the id guard lives).
    return _person_scope(principal.removeprefix("person:"))


def _person_bearer_scope(request: Request) -> str:
    """``key:admin:<id>`` for a live personal key in the bearer, else "".

    Its code is not checked: the scope only counts the request in that
    person's bucket of the global limit. A revoked or unknown ``pba_`` key,
    an unreadable store or a hand-edited record is "" (the address counts
    it). Never raises and logs no refusal (BUG-119).
    """
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if (
        not token.startswith(admin_people.KEY_PREFIX)
        or not _effective_admin_key()
        or _is_admin_key(token)
    ):
        return ""
    person = admin_people.find_by_key(token)
    person_id = person.get("id") if person else None
    if not isinstance(person_id, str) or not admin_people.ID_PATTERN.fullmatch(
        person_id
    ):
        return ""
    return _person_scope(person_id)


def _rate_limit_scope(request: Request) -> str:
    """Whose counter a request takes from the global limit (BUG-119).

    The valid key it carries (``_key_scope``), the client address otherwise:
    a wrong, forged or revoked key never leaves the address counter. Behind a
    proxy every client has the proxy's address unless it is trusted, so the
    address counter only holds the calls without a valid key. A live personal
    key, or a session it opened, counts in that person's own bucket
    (``key:admin:<id>``).
    """
    return (
        _key_scope(request) or _person_bearer_scope(request) or _address_scope(request)
    )


class _RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if rate_limit_on() and not check_rate_limit(_rate_limit_scope(request)):
            return JSONResponse(
                status_code=429, content={"detail": "Rate limit exceeded"}
            )
        return await call_next(request)


_KEYLESS_REFUSAL = (
    "API_KEY and ADMIN_KEY are both empty: every route is open, so this server "
    "only answers on a loopback interface. Run `make init` (or set random "
    "ADMIN_KEY and API_KEY values), or listen on 127.0.0.1."
)
_keyless_refusal_logged = False


def _arrived_on_loopback(scope) -> bool:
    """True when the connection arrived on a loopback interface.

    ``scope["server"]`` is the local address of the connection (uvicorn: the
    socket's sockname); proxy headers, the Host header and the peer never
    change it. A Unix socket or an unknown address is not loopback.
    """
    server = scope.get("server")
    if not server:
        return False
    try:
        address = ipaddress.ip_address(str(server[0]))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


class _KeylessLoopbackOnlyMiddleware:
    """Keyless mode (API_KEY and ADMIN_KEY empty) answers on loopback only.

    Every route is open then. run.py refuses to listen elsewhere; this covers
    a server started another way, e.g. ``uvicorn plugboard.app:app --host
    0.0.0.0``.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] in ("http", "websocket")
            and not _API_KEY
            and not _ADMIN_KEY
            and not _arrived_on_loopback(scope)
        ):
            global _keyless_refusal_logged
            if not _keyless_refusal_logged:
                _keyless_refusal_logged = True
                log.critical(
                    "%s Refused a request on %s.", _KEYLESS_REFUSAL, scope.get("server")
                )
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            refusal = JSONResponse(
                status_code=403, content={"detail": _KEYLESS_REFUSAL}
            )
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)


# Added first = innermost: the refusal still gets the security headers.
app.add_middleware(_KeylessLoopbackOnlyMiddleware)
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


def _create_admin_session_token(
    mfa: bool = False, person: Optional[Dict[str, Any]] = None
) -> str:
    """Sign a new session and register it server-side (revocable).

    ``mfa`` records that a TOTP code was checked to open it. A session opened
    with a personal key (``person``) always had its code checked, and keeps
    the person's id and ``generation``: it dies with the key (PB-AUTH-01).
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

    if person is None:
        admin_sessions.register(
            nonce,
            issued_at,
            mfa=mfa,
            max_age=_ADMIN_SESSION_MAX_AGE,
            mfa_generation=_mfa.generation(),
        )
    else:
        admin_sessions.register(
            nonce,
            issued_at,
            mfa=True,
            max_age=_ADMIN_SESSION_MAX_AGE,
            mfa_generation=_mfa.generation(),
            person_id=person["id"],
            person_generation=person["generation"],
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


def _admin_session_principal(token: str) -> Optional[Tuple[str, str]]:
    """The ``(principal, name)`` of a valid admin session, else ``None``.

    Valid: signed, unexpired, still registered, opened under the current MFA
    generation, and opened with TOTP when MFA is required (a session from
    before enforcement does not count). ``("admin-key", "")`` for a session
    of ``ADMIN_KEY``. A session opened with a personal key also needs its
    person to be in the store, not revoked, and of the generation the
    session was opened under (PB-AUTH-01): an unreadable store refuses it.
    """
    nonce = _admin_session_nonce(token)
    if not nonce:
        return None
    record = admin_sessions.lookup(nonce)
    if record is None:
        return None
    from plugboard import admin_mfa as _mfa

    if record.get("mfa_generation") != _mfa.generation():
        return None
    if not (record.get("mfa") or not _mfa.is_required()):
        return None
    if "person_id" not in record:
        return (_ADMIN_KEY_PRINCIPAL, "")
    person = admin_people.get(record["person_id"])
    if person is None or person.get("revoked_at"):
        return None
    generation = person.get("generation")
    if not isinstance(generation, str) or not generation:
        return None
    if generation != record.get("person_generation"):
        return None
    return (_person_principal(person), str(person.get("name", "")))


def _is_admin_session(token: str) -> bool:
    """A valid admin session (see ``_admin_session_principal``)."""
    return _admin_session_principal(token) is not None


# PB-SEC-06: SameSite=Strict keeps the session cookie off cross-site requests
# only. A page served on another port of the same host is same-site, so the
# browser still attaches the cookie to the forms and fetches that page sends.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CROSS_SITE_REFUSED = "Cross-site request refused"


def _origin_is_this_host(origin: str, request: Request) -> bool:
    """``Origin`` names the host and port the request was sent to.

    The scheme is not compared: behind a TLS proxy the app sees plain HTTP.
    """
    from urllib.parse import urlsplit

    try:
        sent_from = urlsplit(origin)
        sent_to = urlsplit("//" + request.headers.get("host", ""))
        default_port = {"http": 80, "https": 443}.get(sent_from.scheme)
        return bool(sent_from.hostname) and (
            sent_from.hostname == sent_to.hostname
            and (sent_from.port or default_port) == (sent_to.port or default_port)
        )
    except ValueError:
        return False


def _is_cross_origin_browser_request(request: Request) -> bool:
    """The browser says another origin sent this request.

    ``Sec-Fetch-Site`` decides when present (anything but ``same-origin``);
    otherwise ``Origin`` when present. A page cannot set either header; a
    request without both does not come from a browser page.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site.strip().lower() != "same-origin"
    origin = request.headers.get("origin")
    if origin is not None:
        return not _origin_is_this_host(origin, request)
    return False


def _refuse_cross_site_session(request: Request) -> None:
    """403 for a state-changing request that the dashboard session cookie
    authenticates but another origin sent (CSRF).

    The admin key in ``Authorization`` carries its own authority: such a
    request is not refused, nor any request the cookie does not authenticate.
    """
    if request.method not in _STATE_CHANGING_METHODS:
        return
    if not _is_cross_origin_browser_request(request):
        return
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if _is_admin_key(token):
        return
    if not _is_admin_session(request.cookies.get(_ADMIN_COOKIE_NAME, "")):
        return
    log.warning(
        "auth rejected reason=cross_site_session method=%s path=%s",
        request.method,
        request.url.path,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=_CROSS_SITE_REFUSED
    )


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
            _refuse_cross_site_session(request)
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
    "locked" (too many wrong codes for the admin key, from any address:
    a code is only checked with the admin key; BUG-119)."""
    session = _admin_session_principal(request.cookies.get(_ADMIN_COOKIE_NAME, ""))
    if session is not None:
        _set_principal(request, *session)
        return "ok"
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token.startswith(admin_people.KEY_PREFIX) and not _is_admin_key(token):
        return _person_request_auth(request, token)
    if not _is_admin_key(token):
        return "denied"
    from plugboard import admin_mfa as _mfa

    # The principal is set only once the request is proven: a refused one
    # names nobody (its own audit entries name the admin key explicitly).
    if not _mfa.is_required():
        _set_principal(request, _ADMIN_KEY_PRINCIPAL)
        return "ok"
    code = request.headers.get(_ADMIN_TOTP_HEADER, "").strip()
    if not code:
        return "mfa_required"
    from plugboard.ratelimit import check_auth_rate_limit, record_auth_failure

    client_ip = _client_address(request)
    if not check_auth_rate_limit(_ADMIN_SCOPE):
        return "locked"
    if _mfa.verify(code):
        # The code is single-use: remember it was proven in this request, so
        # an MFA change made with it (step-up) does not ask for it again.
        request.state.admin_mfa_verified = True
        _set_principal(request, _ADMIN_KEY_PRINCIPAL)
        return "ok"
    locked = record_auth_failure(_ADMIN_SCOPE)
    _audit(
        "auth.mfa_failed",
        details=f"header IP: {client_ip}, locked={locked}",
        ip=client_ip,
        principal=_ADMIN_KEY_PRINCIPAL,
    )
    return "mfa_required"


def _person_request_auth(request: Request, token: str) -> str:
    """Admin authentication by a personal key (``pba_``), with the same
    outcomes as ``_admin_request_auth``. A code of that person's own secret
    is always required, even where global admin MFA is not; wrong codes lock
    that person only (``key:admin:<id>``). An unknown or revoked key, or an
    unreadable people store, is "denied" and counts nothing. The admin key's MFA proof
    (``admin_mfa_verified``) is never set here. Without ``ADMIN_KEY`` a
    personal key opens nothing; a fault of the store (unreadable, not
    writable, malformed record) refuses without counting a wrong code."""
    if not _effective_admin_key():
        return "denied"
    person = admin_people.find_by_key(token)
    if person is None or not _person_record_is_sound(person):
        return "denied"
    code = request.headers.get(_ADMIN_TOTP_HEADER, "").strip()
    if not code:
        return "mfa_required"
    outcome = _prove_person_code(request, person, code, via="header")
    if outcome == "locked":
        return "locked"
    if outcome == "unavailable":
        return "denied"
    if outcome != "ok":
        return "mfa_required"
    _set_principal(request, _person_principal(person), str(person.get("name", "")))
    return "ok"


def _person_record_is_sound(person: Dict[str, Any]) -> bool:
    """The record's id has the store's shape (a hand-edited one is refused)."""
    person_id = person.get("id")
    if isinstance(person_id, str) and admin_people.ID_PATTERN.fullmatch(person_id):
        return True
    log.error("auth rejected reason=person_record_malformed")
    return False


def _prove_person_code(
    request: Request, person: Dict[str, Any], code: str, *, via: str
) -> str:
    """Check a code of that person's own secret, on the TOTP header
    (``via="header"``) or at login (``via="login"``).

    "ok" when accepted (the first accepted code confirms the key, audited);
    "locked" when that person's scope (``key:admin:<id>``) is locked, nothing
    checked; "refused" for a wrong code, counted on that scope only and
    audited, or "refused_locked" when that count locks the scope;
    "unavailable" for a fault of the store, nothing counted. Every entry
    names the person explicitly: it may be written before the request is
    proven.
    """
    from plugboard.ratelimit import check_auth_rate_limit, record_auth_failure

    person_id = person["id"]
    principal = _person_principal(person)
    name = str(person.get("name", ""))
    scope = _person_scope(person_id)
    client_ip = _client_address(request)
    if not check_auth_rate_limit(scope):
        return "locked"
    outcome = admin_people.check_code(person_id, code)
    if outcome == "unavailable":
        log.error("auth rejected reason=person_store_unavailable")
        return "unavailable"
    if outcome == "refused":
        locked = record_auth_failure(scope)
        where = "header IP" if via == "header" else "IP"
        _audit(
            "auth.person.mfa_failed",
            target_type="admin_person",
            target_id=person_id,
            details=f"{where}: {client_ip}, locked={locked}",
            ip=client_ip,
            principal=principal,
            principal_name=name,
        )
        return "refused_locked" if locked else "refused"
    if outcome == "confirmed":
        _audit(
            "admin.person.confirmed",
            target_type="admin_person",
            target_id=person_id,
            details=f"via={via}",
            ip=client_ip,
            principal=principal,
            principal_name=name,
        )
    return "ok"


def _require_mfa_step_up(request: Request, code: Optional[str] = None) -> None:
    """Ask for a current TOTP code before the second factor is changed.

    Once a secret is enrolled, rotating or removing it needs a code from the
    current secret, even from an open admin session (an auto-login session
    included): holding a session must not be enough to drop the factor.
    The code comes from ``code`` (request body) or the TOTP header; a code
    already verified by this request's authentication counts. Wrong codes
    lock the admin key (``key:admin``), whatever the address (BUG-119).
    """
    from plugboard import admin_mfa as _mfa

    if not _mfa.is_enrolled():
        return
    if getattr(request.state, "admin_mfa_verified", False):
        return
    code = (code or request.headers.get(_ADMIN_TOTP_HEADER, "")).strip()
    client_ip = _client_address(request)
    if not code:
        _audit("admin.mfa.step_up_missing", details=f"IP: {client_ip}", ip=client_ip)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"A current MFA code is required (body or {_ADMIN_TOTP_HEADER})",
        )
    from plugboard.ratelimit import check_auth_rate_limit, record_auth_failure

    if not check_auth_rate_limit(_ADMIN_SCOPE):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts",
        )
    if _mfa.verify(code):
        return
    locked = record_auth_failure(_ADMIN_SCOPE)
    _audit(
        "admin.mfa.step_up_failed",
        details=f"IP: {client_ip}, locked={locked}",
        ip=client_ip,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Invalid MFA code"
    )


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
        _refuse_cross_site_session(request)
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
    """Raise 429 when the caller's monthly spend, with the holds of its calls
    in progress (BUG-52), reaches its limit; 503 usage_unavailable when the
    usage cannot be read. Returns the recorded spend (holds not included)."""
    limit, key_id = _spending_scope(client_id, key_record)
    if limit <= 0:
        return 0.0  # unlimited
    month = _billing.current_pricing_month()
    try:
        if key_id:
            spent = _usage.get_total_cost_by_key(month, key_id)
        else:
            spent = _usage.get_total_cost_by_client(month, client_id)
        held = _usage.get_held_cost(month, key_id=key_id, client_id=client_id)
    except Exception as exc:
        log.error("usage cannot be read: budgeted key refused (%s)", exc)
        raise _UsageUnavailable() from None
    scope = f"key '{key_id}'" if key_id else f"client '{client_id}'"
    if spent >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly spending limit exceeded for {scope} (${spent:.4f} >= ${limit:.4f})",
        )
    if spent + held >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Monthly spending limit reached for {scope} with the calls in "
                f"progress (${spent:.4f} spent + ${held:.4f} held >= ${limit:.4f})"
            ),
        )
    return spent


# Completion tokens a budgeted call is assumed to produce when it sets
# neither max_tokens nor max_completion_tokens (BUG-52).
_BUDGET_RESERVE_TOKENS = envcompat.get_int(
    "PLUGBOARD_BUDGET_RESERVE_TOKENS", default=1024, minimum=1
)
_USAGE_RETRY_AFTER_S = 30


class _UsageUnavailable(HTTPException):
    """503 ``usage_unavailable``: usage cannot be read or written, so a key
    with a monthly budget is refused before any provider call (D-37); the
    other keys are served. An HTTPException, so the chat handlers let it
    through; its own handler adds ``code`` and ``retry_after``."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail=(
                "Usage cannot be recorded right now, so a key with a monthly "
                f"budget is refused: retry in {_USAGE_RETRY_AFTER_S} s"
            ),
            headers={"Retry-After": str(_USAGE_RETRY_AFTER_S)},
        )


@app.exception_handler(_UsageUnavailable)
async def _usage_unavailable_handler(request: Request, exc: _UsageUnavailable):
    content: Dict[str, Any] = {"detail": exc.detail, "code": "usage_unavailable"}
    headers = _retry_after_headers(content, _USAGE_RETRY_AFTER_S)
    return JSONResponse(status_code=503, content=content, headers=headers)


# Prompt tokens counted for each image of a budgeted call: about the most
# a provider bills for one (a large image at high detail).
_BUDGET_IMAGE_TOKENS = 1600


def _whole_count(value: Any) -> Optional[int]:
    """A positive count as a provider would read it (5, 5.0, "5"), rounded
    up; None for anything else (a boolean, 0, a negative, text)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return math.ceil(value)
    return None


def _completion_cap(body: Dict[str, Any]) -> int:
    """Completion tokens one answer may produce: the larger of
    max_completion_tokens and max_tokens (a provider may honour either),
    else _BUDGET_RESERVE_TOKENS."""
    caps = [
        cap
        for cap in (
            _whole_count(body.get(n)) for n in ("max_completion_tokens", "max_tokens")
        )
        if cap
    ]
    return max(caps) if caps else _BUDGET_RESERVE_TOKENS


def _answers_asked(body: Dict[str, Any]) -> int:
    """Answers the call asks for: n, or best_of when larger (each is billed)."""
    return max(
        1, _whole_count(body.get("n")) or 1, _whole_count(body.get("best_of")) or 1
    )


def _prompt_estimate(body: Dict[str, Any]) -> int:
    """Prompt tokens: the messages' text, the tool definitions and the tool
    calls sent back, and _BUDGET_IMAGE_TOKENS per image part."""
    messages = body.get("messages") or []
    tokens = estimate_prompt_tokens(messages)
    extra: List[Any] = [body.get(k) for k in ("tools", "functions") if body.get(k)]
    images = 0
    for msg in messages if isinstance(messages, list) else []:
        if not isinstance(msg, dict):
            continue
        if msg.get("tool_calls"):
            extra.append(msg["tool_calls"])
        content = msg.get("content")
        if isinstance(content, list):
            images += sum(
                1
                for part in content
                if isinstance(part, dict) and part.get("type") != "text"
            )
    if extra:
        tokens += count_tokens(json.dumps(extra, default=str))
    return tokens + images * _BUDGET_IMAGE_TOKENS


def _estimate_call_cost(body: Dict[str, Any]) -> float:
    """What this call should cost at most: its prompt, and its completion cap
    for each answer it asks for, priced on the dearest route it may take.

    An estimate, not a ceiling: a provider that ignores max_tokens (a CLI
    route) or a call without max_tokens that answers longer than
    _BUDGET_RESERVE_TOKENS costs more; the overrun is logged when billed.
    """
    from plugboard.router import _resolve_all_providers

    try:
        routes = _resolve_all_providers(body.get("model", ""))
    except ValueError:
        return 0.0  # forward() stops on the same routing error, before any call
    prompt = _prompt_estimate(body)
    completion = _completion_cap(body) * _answers_asked(body)
    return max(
        (chat_billing.price(p, m, prompt, completion).cost_usd for p, m in routes),
        default=0.0,
    )


async def _take_budget_hold(
    client_id: Optional[str],
    key_record: Optional[Dict[str, Any]],
    body: Dict[str, Any],
    keep: Callable[[Optional[chat_billing.BudgetHold]], None],
) -> Optional[chat_billing.BudgetHold]:
    """Hold the call's estimated cost on the key's monthly budget (BUG-52).

    Keys without a budget, and the admin, hold nothing. The check and the
    hold are one transaction of the usage database, so simultaneous calls
    cannot pass the budget together: a call that does not fit is refused
    (429) and calls nothing. When the hold cannot be written the key is
    refused (503 usage_unavailable, D-37): nothing is served uncounted.
    ``keep`` gets the hold before it is written, so a request cancelled
    meanwhile still gives it back; then None when it was not taken.
    """
    if not client_id or client_id == "admin":
        return None
    limit, key_id = _spending_scope(client_id, key_record)
    if limit <= 0:
        return None
    cost = _estimate_call_cost(body)
    hold = chat_billing.BudgetHold(secrets.token_hex(8), cost)
    keep(hold)
    try:
        got = await _asyncio.to_thread(
            _usage.hold_budget,
            client_id=client_id,
            key_id=key_id,
            month=_billing.current_pricing_month(),
            limit=limit,
            cost=cost,
            hold_id=hold.id,
            model=str(body.get("model", "")),
        )
    except Exception as exc:
        hold.done = True
        keep(None)
        log.error("usage cannot be written: budgeted key refused (%s)", exc)
        raise _UsageUnavailable() from None
    if isinstance(got, _usage.HoldRefused):
        hold.done = True
        keep(None)
        scope = f"key '{key_id}'" if key_id else f"client '{client_id}'"
        raise HTTPException(
            status_code=429,
            detail=(
                f"Monthly spending limit would be exceeded for {scope}: "
                f"${got.spent:.4f} spent, ${got.held:.4f} held for calls in "
                f"progress, about ${cost:.4f} for this call, limit ${limit:.4f}"
            ),
        )
    return hold


async def _hold_budget(
    request: Request,
    client_id: Optional[str],
    key_record: Optional[Dict[str, Any]],
    body: Dict[str, Any],
) -> None:
    """The chat's hold (_take_budget_hold), kept on the request: billing
    settles it, _session_turn_dep gives back one that was not billed."""
    request.state.budget_hold = None

    def keep(hold: Optional[chat_billing.BudgetHold]) -> None:
        request.state.budget_hold = hold

    await _take_budget_hold(client_id, key_record, body, keep)


def _keep_hold(hold: Optional[chat_billing.BudgetHold]) -> None:
    """A served call whose billing failed keeps its hold: counted at its
    estimate until it lapses and is recorded, never given back."""
    if hold is not None and not hold.done:
        hold.done = True
        hold.settling = False
        log.error("budget hold %s kept: the served call was not billed", hold.id)


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


async def _aclose_quietly(agen: Any) -> None:
    """Close an upstream async generator, even inside a cancelled task.

    Starlette cancels the response task when the client disconnects, and
    keeps cancelling it on every await. The close runs in its own task, so
    the upstream generator still releases its HTTP connection; if we are
    cancelled meanwhile, the cancellation propagates and the close finishes
    on its own.
    """
    aclose = getattr(agen, "aclose", None)
    if aclose is None:
        return
    task = _asyncio.ensure_future(aclose())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    # Retrieve the result so an error in a close nobody awaits is not
    # reported as "exception never retrieved".
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    try:
        await _asyncio.shield(task)
    except _asyncio.CancelledError:
        raise
    except Exception as exc:
        log.debug("Upstream stream close error (ignored): %s", exc)


def _bill_streaming(
    client_id: str,
    body: dict,
    raw_sse: bytes,
    key_id: str = "",
    hold: Optional[chat_billing.BudgetHold] = None,
    reported: bool = False,
) -> Dict[str, Any]:
    """Record usage from accumulated SSE chunks and return the billing dict.

    ``reported``: the stream's usage holds counts a CLI reported itself
    (router.usage_reported()); a 0 among them is then a count, not a gap.

    Returns a dict with keys {prompt_tokens, completion_tokens, total_tokens,
    cost_usd, model, upstream_usage_present, text}; ``text`` (the answer,
    recorded in the session) is never sent to the client. The caller (streaming
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

    # Prefer real upstream counts if present, fall back to estimates. A zero
    # is estimated too, unless the CLI reported it (as for a whole answer,
    # chat_billing.counts): a live CLI stream ends with the counts that
    # _stream_route already settled.
    def _kept(value: Any) -> bool:
        if reported and isinstance(value, int) and not isinstance(value, bool):
            return value >= 0
        return bool(value)

    prompt_tokens = upstream_usage.get("prompt_tokens")
    if not _kept(prompt_tokens):
        prompt_tokens = estimate_prompt_tokens(body.get("messages", []))
    completion_tokens = upstream_usage.get("completion_tokens")
    if not _kept(completion_tokens):
        completion_tokens = count_tokens(completion_text) if completion_text else 0

    provider, real_model = _billing_target(body)
    charge = chat_billing.price(provider, real_model, prompt_tokens, completion_tokens)
    chat_billing.record(client_id, key_id, charge, hold=hold)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": charge.total_tokens,
        "cost_usd": round(charge.cost_usd, 6),
        "pricing_month": charge.pricing_month,
        "model": real_model,
        "upstream_usage_present": bool(upstream_usage),
        "text": completion_text,
    }


def _bill_completion(
    client_id: str,
    key_id: str,
    body: Dict[str, Any],
    result: Dict[str, Any],
    hold: Optional[chat_billing.BudgetHold] = None,
) -> Dict[str, Any]:
    """Record the usage of a whole answer; return the billing dict.

    A zero count is estimated, unless the CLI reported it (a reported 0 is a
    count, see router.usage_reported()).
    """
    provider, real_model = _billing_target(body)
    charge = chat_billing.charge_completion(
        body, result, provider, real_model, reported=_usage_reported()
    )
    chat_billing.record(client_id, key_id, charge, hold=hold)
    return charge.billing_info()


async def _charge_answer(
    request: Request,
    client_id: Optional[str],
    key_record: Optional[Dict[str, Any]],
    spending_total: float,
    body: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Bill a whole answer (a non-streamed chat, or one a CLI route answered
    for a stream) and check the spending alert; {} when nothing was billed.
    A billing error does not fail the answer: it is logged. The usage write
    runs off the event loop (PLU-27)."""
    if not client_id:
        return {}
    hold = getattr(request.state, "budget_hold", None)
    if hold is not None:
        hold.settling = True  # never given back while it is billed
    try:
        billing_info = await _asyncio.to_thread(
            _bill_completion,
            client_id,
            getattr(request.state, "key_id", ""),
            body,
            result,
            hold=getattr(request.state, "budget_hold", None),
        )
    except Exception as exc:
        log.warning("Billing error (non-fatal): %s", exc)
        _keep_hold(getattr(request.state, "budget_hold", None))
        return {}
    if client_id != "admin":
        try:
            _send_spending_alert(
                client_id,
                billing_info["pricing_month"],
                key_record=key_record,
                current_total_usd=spending_total + billing_info["cost_usd"],
            )
        except Exception as exc:
            log.warning("Spending alert error (non-fatal): %s", exc)
    return billing_info


def _billing_target(body: Dict[str, Any]) -> tuple:
    """(provider, real_model) to bill: the one that served the call.

    forward() may fall back to another provider; router.served_by() records
    what actually answered.
    """
    served = _served_by()
    if served is not None:
        return served
    return resolve_provider_and_model(body.get("model", ""))


# -- Model access check -----------------------------------------------------


class _ModelNotAllowed(HTTPException):
    """403: the key's allowed_models does not cover the model. Answered with
    ``code`` forbidden_model, like the preflight's item error."""


@app.exception_handler(_ModelNotAllowed)
async def _model_not_allowed_handler(request: Request, exc: _ModelNotAllowed):
    return JSONResponse(
        status_code=403, content={"detail": exc.detail, "code": "forbidden_model"}
    )


def _check_model_access(
    client_id: str,
    model_str: str,
    key_record: Optional[Dict[str, Any]] = None,
) -> None:
    """Raise 403 unless the key may use the model as chat would route it.

    Chat's own rule, as on the info page since PLU-60 (BUG-129): the first
    route the model resolves to, then every route chat would try, in order
    (router.chat_routes): refused when the key may use none of them before
    the first it refuses. A name no route serves is left to the checks that
    follow (provider_disabled, model_not_found).
    """
    guard = _model_route_guard(client_id, key_record)
    if guard is None:
        return
    # A model of a disabled provider is checked as its real owner's (PLU-12):
    # resolve_provider_and_model would name another provider (BUG-44).
    disabled = disabled_route(model_str)
    guard(*(disabled or resolve_provider_and_model(model_str)))
    if disabled is not None:
        return
    try:
        chat_routes(model_str, guard)
    except (ValueError, ModelNotServedError):
        return


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
    registry_ids: Optional[tuple] = None

    def guard(provider, native):
        from plugboard import router as _router
        from plugboard.model_identity import model_access_matches

        nonlocal registry_ids
        if registry_ids is None:
            # Every registered ID, inactive included: the namespace /v1/models
            # publishes in. Read once per guard, through the router's registry.
            registry_ids = tuple(p["id"] for p in _router.load_providers())
        if not model_access_matches(allowed, provider["id"], native, registry_ids):
            raise _ModelNotAllowed(
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
    return _guard_accepts(_model_route_guard(client_id, key_record), model_str)


def _guard_accepts(guard, model_str: str) -> bool:
    """Whether a key's guard (None: unrestricted) lets this model through."""
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
        # Kept until sent and drained at shutdown (PLU-27).
        from plugboard.alerts import in_background

        in_background(_check_and_alert(client_id, total, limit))
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


def _quota_detail(provider: str, retry_after: Optional[int]) -> str:
    """Reason given to a client key: composed here, never the upstream text."""
    when = (
        f"retry after {retry_after} seconds"
        if retry_after is not None
        else "retry later"
    )
    return f"The quota or rate limit of the {provider} provider is exhausted: {when}."


def _retry_after_headers(
    content: Dict[str, Any], retry_after: Optional[int]
) -> Dict[str, str]:
    """Add ``retry_after`` to an error body; return the Retry-After header."""
    if retry_after is None:
        return {}
    content["retry_after"] = retry_after
    return {"Retry-After": str(retry_after)}


def _quota_error_response(
    exc: ProviderQuotaError, client_id: Optional[str]
) -> JSONResponse:
    """429 when the last HTTP provider tried answered 429.

    A non-streamed request has already been offered to the other candidate
    providers (the last one tried decides); a streamed one to an HTTP
    provider has no fallback. The upstream body stays with the admin; a
    client key gets the provider, the reason and the provider's Retry-After
    delay when it gave one.
    """
    log.warning(
        "provider quota exhausted: provider=%s retry_after=%s: %s",
        exc.provider_id,
        exc.retry_after,
        exc,
    )
    if client_id == "admin":
        detail = str(exc)
    else:
        detail = _quota_detail(exc.provider_id, exc.retry_after)
    content: Dict[str, Any] = {"detail": detail, "code": "quota_exhausted"}
    headers = _retry_after_headers(content, exc.retry_after)
    return JSONResponse(status_code=429, content=content, headers=headers)


def _providers_busy_response(
    exc: ProvidersBusyError, client_id: Optional[str]
) -> JSONResponse:
    """503 ``providers_busy`` when every candidate provider stayed at its
    concurrency cap for the whole wait (D-23).

    Nothing was called. Only the admin gets the ids of the busy providers.
    """
    log.warning("providers busy: %s", ", ".join(exc.provider_ids))
    record_busy_refusal((exc.provider_ids or [""])[0], exc.model)  # PLU-16
    detail = (
        "Every provider that serves this model is busy: "
        f"retry in {exc.retry_after} seconds."
    )
    if client_id == "admin":
        detail += f" Busy providers: {', '.join(exc.provider_ids)}."
    content: Dict[str, Any] = {"detail": detail, "code": "providers_busy"}
    headers = _retry_after_headers(content, exc.retry_after)
    return JSONResponse(status_code=503, content=content, headers=headers)


def _provider_disabled_response(
    provider_id: str, provider: Optional[Dict[str, Any]] = None
) -> JSONResponse:
    """503 ``provider_disabled``: the model belongs to a provider the admin
    turned off (PLU-12). Nothing was called, no other provider stands in,
    and no ``Retry-After``: nobody knows when it comes back.

    A provider turned off because its stored key cannot be decrypted
    (BUG-57) keeps the same code, with a detail and hint that say so.
    """
    log.info("provider disabled: %s", provider_id)
    if provider is None:
        provider = get_provider(provider_id) or {}
        if not provider:
            from plugboard import ollama

            if provider_id == ollama.PROVIDER_ID and ollama.disabled_by_config():
                provider = {"disabled_by_config": True}  # never registered
    if provider.get("disabled_by_config"):
        detail = (
            f"Provider '{provider_id}' is turned off by the server "
            "configuration (OLLAMA_ENABLED=false); nothing was called."
        )
        hint = (
            "Pick another model from GET /v1/models, or ask the administrator "
            "to set OLLAMA_ENABLED to true or auto and restart the gateway."
        )
    elif provider.get("secret_unreadable"):
        detail = (
            f"Provider '{provider_id}' is turned off: its stored key cannot be "
            "decrypted with the current ENCRYPTION_KEY; nothing was called."
        )
        hint = (
            "Pick another model from GET /v1/models, or ask the administrator "
            "to restore the ENCRYPTION_KEY that encrypted it, or to enter the "
            "provider's key again."
        )
    else:
        detail = (
            f"Provider '{provider_id}' is turned off by the "
            "administrator; nothing was called."
        )
        hint = (
            "Pick another model from GET /v1/models, or ask the "
            "administrator to turn the provider back on."
        )
    return JSONResponse(
        status_code=503,
        content={"detail": detail, "code": "provider_disabled", "hint": hint},
    )


def _unsupported_on_cli_response(exc: UnsupportedOnCliError) -> JSONResponse:
    """400 ``unsupported_on_cli``: every route of the model is a CLI
    subscription, and a CLI cannot serve the fields ``exc.unsupported``
    names (tools, a structured response format, an image...). Nothing was
    called. The body holds no secret: a client key and the admin get it alike.
    """
    log.info(
        "unsupported on cli: provider=%s fields=%s",
        exc.provider_id,
        ",".join(exc.unsupported),
    )
    return JSONResponse(
        status_code=400,
        content={
            "detail": f"Model route '{exc.provider_id}' is a CLI subscription: "
            f"it cannot serve {', '.join(exc.unsupported)}. Nothing was called.",
            "code": UNSUPPORTED_ON_CLI,
            "unsupported": list(exc.unsupported),
            "hint": "Send plain text messages, or pick a model served by an API "
            "or Ollama provider from GET /v1/models.",
        },
    )


# A whole answer asked as a stream is replayed by chat_billing (PLU-28).
_replay_completion_as_sse = chat_billing.replay_as_sse


def _chat_error_response(
    exc: Exception, client_id: Optional[str], phase: str
) -> JSONResponse:
    """The answer to a chat whose provider call raised ``exc`` (every chat
    path: plain, stream, a CLI route asked to stream).

    Typed errors keep their own answers. Any other text can name a host, a
    port, a path, an account or a CLI hint (PLU-28): only the admin gets it.
    A client key gets a sentence written here and an ``error_ref`` that
    finds the text in the gateway log. A timeout is a 503 with Retry-After
    10, a failed provider authentication keeps its status, the rest is 502.
    """
    if isinstance(exc, ProvidersBusyError):
        return _providers_busy_response(exc, client_id)
    if isinstance(exc, ProviderDisabledError):
        return _provider_disabled_response(exc.provider_id)
    if isinstance(exc, UnsupportedOnCliError):
        return _unsupported_on_cli_response(exc)
    if isinstance(exc, ModelNotServedError):
        return _model_not_served_response()  # nothing was started (BUG-120)
    # Before the timeout test: a CLI message quotes its stderr.
    if isinstance(exc, CliSubprocessError):
        return _cli_error_response(exc, client_id)
    if isinstance(exc, ProviderQuotaError):
        return _quota_error_response(exc, client_id)
    # Before the timeout test: the provider's body can say "timeout".
    if isinstance(exc, (ProviderHTTPError, ProviderNetworkError)):
        return _provider_http_error_response(exc, client_id)
    ref = secrets.token_hex(4)
    err_str = str(exc)
    log.error("%s error ref=%s (%s): %s", phase, ref, type(exc).__name__, err_str)
    headers: Dict[str, str] = {}
    content: Dict[str, Any] = {}
    if isinstance(exc, ProviderTimeoutError) or "timeout" in err_str.lower():
        status_code = 503
        who = getattr(exc, "provider_id", "")
        detail = f"The {who} provider" if who else "The provider"
        detail += " did not answer in time"
        content["retry_after"] = 10
        content["code"] = "provider_timeout"
        headers["Retry-After"] = "10"
    elif isinstance(exc, AuthError):
        status_code = exc.status_code
        detail = "The provider's credentials could not be used"
    else:
        status_code = 502
        detail = "The provider call failed"
    if client_id == "admin":
        content["detail"] = err_str
    else:
        content["detail"] = f"{detail} (reference {ref} in the gateway log)."
    content["error_ref"] = ref
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _provider_http_error_response(
    exc: ProviderHTTPError | ProviderNetworkError, client_id: Optional[str]
) -> JSONResponse:
    """400 when an HTTP provider rejected the request, 502 for its other errors.

    The provider rejected the request when its answer says so (413, an error
    saying the context is too long in any format, or an error type naming the
    request: ``exc.request_rejected``); the status alone does not, a provider
    also answers 400 for its own key. A
    provider that could not be reached gives a 502. The provider's body can
    name its account or organisation, and a network error its host and port:
    only the admin gets them. A client key gets a reason written here and a
    reference to the server log entry, never the upstream text.
    """
    ref = secrets.token_hex(4)
    log.warning(
        "http provider error ref=%s provider=%s status=%s: %s",
        ref,
        exc.provider_id,
        getattr(exc, "status", "network"),
        exc,
    )
    rejected = getattr(exc, "request_rejected", False)
    if client_id == "admin":
        detail = str(exc)
    elif rejected:
        detail = (
            f"The {exc.provider_id} provider rejected the request: check the "
            "length of the messages against the model's context and the "
            f"parameters (reference {ref} in the gateway log)."
        )
    else:
        detail = (
            f"The {exc.provider_id} provider failed "
            f"(reference {ref} in the gateway log)."
        )
    return JSONResponse(
        status_code=400 if rejected else 502,
        content={"detail": detail, "error_ref": ref},
    )


def _cli_error_response(
    exc: CliSubprocessError, client_id: Optional[str]
) -> JSONResponse:
    """502 for a failed CLI provider, 429 when it failed on its quota, 400
    (``context_length_exceeded``) when it found the prompt too long, 400
    (``model_not_supported_by_subscription``) when its subscription does not
    serve the model (BUG-78), 503 (``provider_sandbox_unavailable``) when
    agy's cage could not start, so agy was not run (BUG-86).

    ``exc.quota`` and ``exc.request_too_long`` were decided by the CLI wrapper
    from the CLI's own error and stderr, never from the model's output. The
    CLI's message, stderr, pid and exit code can carry local paths, account
    names or tokens: only the admin (dashboard Playground, admin key) gets
    them. A client key gets a reference to the server log entry.
    """
    quota = exc.quota
    too_long = exc.request_too_long and not quota
    unsupported = getattr(exc, "model_unsupported", False) and not quota
    timed_out = getattr(exc, "timed_out", False) and not quota
    sandbox = getattr(exc, "sandbox_failed", False) and not quota
    ref = secrets.token_hex(4)
    log.warning(
        "cli provider error ref=%s provider=%s model=%s exit=%s pid=%s: %s | stderr=%s",
        ref,
        exc.provider_name,
        exc.model_id,
        exc.exit_code,
        exc.pid,
        exc,
        exc.stderr,
    )
    if client_id == "admin":
        content = {
            "detail": str(exc),
            "exit_code": exc.exit_code,
            "stderr": exc.stderr,
            "pid": exc.pid,
            "provider_name": exc.provider_name,
            "model_id": exc.model_id,
            "error_ref": ref,
        }
    else:
        name = exc.provider_name or "CLI"
        if sandbox:
            detail = (
                f"The {name} provider is unavailable: its sandbox (bubblewrap) "
                f"could not start, so {name} was not run "
                f"(reference {ref} in the gateway log)."
            )
        elif quota:
            detail = _quota_detail(name, exc.retry_after)
        elif too_long:
            detail = (
                f"The {name} provider rejected the request: the messages are too "
                f"long for the model's context (reference {ref} in the gateway log)."
            )
        elif timed_out:
            detail = (
                f"The {name} provider did not answer in time "
                f"(reference {ref} in the gateway log)."
            )
        elif unsupported:
            detail = (
                f"The {name} provider's subscription does not support the model "
                f"{exc.model_id!r}: pick another model from GET /v1/models "
                f"(reference {ref} in the gateway log)."
            )
        else:
            detail = f"The {name} provider failed (reference {ref} in the gateway log)."
        content = {
            "detail": detail,
            "error_ref": ref,
            "provider_name": exc.provider_name,
            "model_id": exc.model_id,
        }
    if sandbox:
        # No Retry-After: a missing or refused sandbox does not come back by
        # itself.
        content["code"] = "provider_sandbox_unavailable"
        return JSONResponse(status_code=503, content=content)
    if too_long:
        content["code"] = "context_length_exceeded"
        return JSONResponse(status_code=400, content=content)
    if unsupported:
        content["code"] = "model_not_supported_by_subscription"
        content["hint"] = MODEL_NOT_SUPPORTED_HINT
        return JSONResponse(status_code=400, content=content)
    if timed_out:
        content["code"] = "provider_timeout"
        content["retry_after"] = 10
        return JSONResponse(
            status_code=503, content=content, headers={"Retry-After": "10"}
        )
    if not quota:
        return JSONResponse(status_code=502, content=content)
    content["code"] = "quota_exhausted"
    headers = _retry_after_headers(content, exc.retry_after)
    return JSONResponse(status_code=429, content=content, headers=headers)


MODEL_NOT_SUPPORTED_HINT = (
    "This route's subscription (a CLI signed in with an account plan) does not "
    "serve this model: GET /v1/models lists the ones it does, or use a route "
    "with an API key."
)

MODEL_NOT_FOUND_HINT = (
    "GET /v1/models lists the model IDs this gateway serves: "
    "send one as written there, <provider-id>:<model>."
)


def _model_not_found_response(model: str) -> JSONResponse:
    """404 for a model ID no provider knows (router.is_unknown_model).

    ``code`` is stable for programs, ``hint`` says what to do. The ID is
    echoed, shortened, so a typo or a stray space shows.
    """
    shown = model if len(model) <= 100 else model[:100] + "..."
    return JSONResponse(
        status_code=404,
        content={
            "detail": f"Model '{shown}' not found: no provider ID prefixes it "
            "and no provider lists it.",
            "code": "model_not_found",
            "hint": MODEL_NOT_FOUND_HINT,
        },
    )


def _model_not_served_response() -> JSONResponse:
    """404 for a model ID a CLI route does not serve (BUG-120).

    The same ``code`` and ``hint`` as ``_model_not_found_response``. The
    detail is generic: it never quotes the ID (it may hold line breaks or
    options), nor a CLI, its output or a pid, since nothing was started.
    """
    return JSONResponse(
        status_code=404,
        content={
            "detail": "Model not found: the provider this model ID selects "
            "does not serve it; nothing was called.",
            "code": "model_not_found",
            "hint": MODEL_NOT_FOUND_HINT,
        },
    )


class _InvalidChatRequest(HTTPException):
    """400 ``invalid_request``: a chat body no provider can answer (messages,
    roles, output caps). Raised before the budget hold and any provider call;
    its handler adds ``code``."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=400, detail=detail)


@app.exception_handler(_InvalidChatRequest)
async def _invalid_chat_request_handler(request: Request, exc: _InvalidChatRequest):
    return JSONResponse(
        status_code=400, content={"detail": exc.detail, "code": "invalid_request"}
    )


def _require_output_caps(body: Dict[str, Any]) -> None:
    """400 for a max_tokens / max_completion_tokens no provider can honour
    (BUG-121): absent or null passes; otherwise a whole number (5 or 5.0, not
    a boolean nor text) from 1 to MAX_OUTPUT_TOKENS_CAP. A negative cap used
    to reach the provider, a huge one to stop at the budget hold as a 429."""
    for name in ("max_tokens", "max_completion_tokens"):
        value = body.get(name)
        if value is None:
            continue
        whole = isinstance(value, int) or (
            isinstance(value, float) and value.is_integer()
        )
        if (
            isinstance(value, bool)
            or not whole
            or not 1 <= value <= MAX_OUTPUT_TOKENS_CAP
        ):
            raise _InvalidChatRequest(
                f"{name} must be an integer from 1 to {MAX_OUTPUT_TOKENS_CAP}"
            )


def _shown_role(role: str) -> str:
    """A refused role as the 400 quotes it: 40 characters at most, anything
    unprintable (a line break, a control character) as ?."""
    shown = "".join(c if c.isprintable() else "?" for c in role[:40])
    return shown + ("..." if len(role) > 40 else "")


def _require_chat_messages(
    body: Dict[str, Any], *, history_may_follow: bool = False
) -> None:
    """400 ``invalid_request`` for a messages field, or an output cap, no
    provider can answer.

    Without it, an empty or malformed list reached the provider and came back
    as a 502 carrying the upstream parser error (or the CLI's "Empty prompt");
    an unknown role was answered as a user turn (BUG-121).
    With ``history_may_follow`` (a session turn, before its history is merged)
    an absent or empty list passes: the stored history may fill it, and the
    full check runs again after the merge. The output caps are checked first,
    in both cases.
    """
    _require_output_caps(body)
    if history_may_follow and body.get("messages", []) == []:
        return
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _InvalidChatRequest("messages must be a non-empty array")
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise _InvalidChatRequest(f"messages[{i}] must be an object")
        role = msg.get("role")
        if not isinstance(role, str) or not role.strip():
            raise _InvalidChatRequest(f"messages[{i}].role must be a non-empty string")
        if role not in CHAT_ROLES:
            raise _InvalidChatRequest(
                f"messages[{i}].role '{_shown_role(role)}' is not supported: use "
                "system, developer, user, assistant, tool or function"
            )
        content = msg.get("content")
        if content is not None and not isinstance(content, (str, list)):
            raise _InvalidChatRequest(
                f"messages[{i}].content must be a string or an array of parts"
            )


def _live_note(
    model: str, client_id: Optional[str], disabled: Optional[tuple] = None
) -> None:
    """Tell /v1/inflight which model, provider and caller this request is.

    ``disabled`` is the request's router.disabled_route answer, read once by
    the caller: a model of a disabled provider is shown as its owner's.
    """
    provider_id = None
    try:
        route = disabled or resolve_provider_and_model(model)
        provider_id = route[0].get("id")
    except Exception:  # unknown model: the admission checks answer it
        pass
    _live.update(client_id=client_id, model=model[:120] or None, provider=provider_id)


async def _refuse_anonymous_chat(
    client_id: Optional[str] = Depends(_resolve_client),
) -> None:
    """SEC-001's 401 for a chat without a client in strict tenant mode, before
    the chat slot (PLU-15): with an empty API_KEY, _resolve_client lets an
    anonymous call through, and it would otherwise wait for a slot."""
    if not client_id and _strict_tenant_mode():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: missing client_id.",
        )


@app.post(
    "/v1/chat/completions",
    # The key first (PLU-15): see _inflight_chat_dep.
    dependencies=[
        Depends(_resolve_client),
        Depends(_refuse_anonymous_chat),
        Depends(_inflight_chat_dep),
        Depends(_session_turn_dep),
    ],
)
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
    try:
        body = await request.json()
    except (ValueError, RecursionError):
        # BUG-48: a body that is not JSON (or not UTF-8, or nested past the
        # parser's depth) is the client's error, not a 500.
        raise HTTPException(400, "Request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    if not isinstance(body.get("model", ""), str):
        raise HTTPException(400, "model must be a string")
    # Read once per request (PLU-12): /v1/inflight and the admission share it.
    disabled = disabled_route(body.get("model", ""))
    _live_note(body.get("model", ""), client_id, disabled)
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
    if isinstance(session_id, (list, dict)):
        # BUG-48: an unhashable session id made the session turn fail (500).
        raise HTTPException(400, "session_id must be a string")
    in_session = bool(session_id and client_id)
    _require_chat_messages(body, history_may_follow=in_session)

    # Issue #5: check spending limit before processing request
    if client_id and client_id != "admin":
        spending_total = await _asyncio.to_thread(
            _check_spending_limit, client_id, key_record=key_record
        )

    # Check model access (per-key allowed_models). A model of a disabled
    # provider is checked as its owner's, then refused (PLU-12).
    route_guard = _model_route_guard(client_id, key_record)
    route_options = {}
    if route_guard is not None:
        route_guard(*(disabled or resolve_provider_and_model(body.get("model", ""))))
        route_options["route_guard"] = route_guard

    # After the key's 403, before the 404 and the 402; before the cache too,
    # so no stored answer comes out while the provider is off.
    if disabled is not None:
        return _provider_disabled_response(disabled[0]["id"], disabled[0])

    # After the key's 403, so a limited key cannot probe which IDs exist;
    # before the 402, which would blame a price for a model that is not there.
    if is_unknown_model(body.get("model", "")):
        return _model_not_found_response(body["model"])
    # BUG-120: a CLI route is never given a name outside its catalogue, nor
    # one that reads as an option, whoever calls (the admin key included).
    # Same place as the 404 above: after the key's 403 and the 503 of a
    # turned-off provider, before the 402, the rpm count, the session turn,
    # the cache and any provider; each of those refuses too, nothing starts.
    if is_unserved_cli_model(body.get("model", "")):
        return _model_not_served_response()

    # After the 404, before the 402 and the rpm count, the session turn and
    # the cache: a request only CLI routes serve, asking for what a CLI cannot
    # (tools, structured output, images), is refused with nothing counted.
    refusal = cli_refusal(body)
    if refusal is not None:
        return _unsupported_on_cli_response(refusal)

    # A budget cannot hold a call whose price is unknown (402, nothing called)
    if client_id and client_id != "admin":
        _check_price_known(client_id, body.get("model", ""), key_record=key_record)

    # Per-client rate limit (rpm_limit on key record)
    if client_id and client_id != "admin":
        _check_client_rpm(client_id, key_record=key_record)

    if in_session:
        from plugboard.session_store import merge_history_into_body

        # D-24: the previous turn of this session is answered and recorded
        # first, so this one sees it; the turn comes back after the response.
        # Taken after every refusal above: a request that is refused does not
        # wait for the turn its session is running.
        try:
            await _unless_client_gone(
                request, _take_session_turn(request, client_id, session_id)
            )
        except _ClientGone:
            return _client_gone_response(body.get("model", "?"))
        body = merge_history_into_body(client_id, session_id, body)
        _require_chat_messages(body)

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
        from plugboard.cache import compute_cache_key, get_cached, key_params

        _cache_key = compute_cache_key(
            body.get("model", ""),
            body.get("messages", []),
            client_id=client_id or "",
            **key_params(body),
        )
        cached = await _asyncio.to_thread(
            get_cached, _cache_key, client_id=client_id or ""
        )
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
                        params=key_params(body),
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
            return StreamingResponse(
                _replay_completion_as_sse(
                    cached,
                    completion_id=cached.get("id", "chatcmpl-cache"),
                    created=cached.get("created", int(time.time())),
                    model=cached.get("model", body.get("model", "")),
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Cache": "HIT",
                },
            )

    # BUG-52: a budgeted key holds this call's estimated cost before any
    # provider is called (429 when it does not fit, 503 when usage cannot be
    # written); the hold is settled when the call is billed, else released
    # once the response is sent (_session_turn_dep).
    await _hold_budget(request, client_id, key_record, body)

    if body.get("stream") is True:
        # Fast path: subprocess providers execute synchronously (CLI returns
        # the full text), so we can call forward() to populate the non-stream
        # cache AND replay the result as SSE. Future identical requests will
        # hit the cache instantly (see the "Cache HIT + stream=true" branch
        # above) — saving ~10s per repeat query.
        try:
            _provider_for_stream, _ = resolve_provider_and_model(body.get("model", ""))
            _stream_auth = str(_provider_for_stream.get("auth_source", ""))
            # A CLI that writes its answer as it goes (claude, agy) is streamed
            # live by forward_stream, the first word at once (INC-2): the
            # answer is not cached by this path. A CLI that cannot is run whole
            # here so repeats hit the cache.
            from plugboard import cli_subprocess as _cli

            _stream_bypass = _stream_auth.startswith("subprocess:") and not (
                _cli.can_stream(_stream_auth.removeprefix("subprocess:"))
            )
        except Exception:
            _stream_bypass = False

        if _stream_bypass:
            try:
                result = await _unless_client_gone(
                    request, forward({**body, "stream": False}, **route_options)
                )
            except _ClientGone:
                return _client_gone_response(body.get("model", "?"))
            except HTTPException:
                raise
            except Exception as exc:
                return _chat_error_response(exc, client_id, "subprocess stream")

            # Billed as a plain chat; the replay ends with what was billed.
            reported_usage = dict(result.get("usage") or {})
            _billing_for_cache = await _charge_answer(
                request, client_id, key_record, spending_total, body, result
            )
            if _billing_for_cache:
                result["billing"] = _billing_for_cache
                result["usage"] = chat_billing.usage_block(
                    _billing_for_cache, reported_usage if _usage_reported() else None
                )

            # Write to cache (same cache the non-stream branch uses, so future
            # requests — stream or not — hit instantly).
            if _use_cache and _cache_key:
                try:
                    from plugboard.cache import set_cached

                    _cost = _billing_for_cache.get("cost_usd", 0)
                    await _asyncio.to_thread(
                        set_cached,
                        _cache_key,
                        body.get("model", ""),
                        _cache_key[:16],
                        result,
                        cost_usd=_cost,
                        client_id=client_id or "",
                    )
                    try:
                        from plugboard import semantic_cache as _sem
                        from plugboard.cache import key_params as _cache_key_params

                        await _sem.store_async(
                            body.get("model", ""),
                            body.get("messages", []),
                            result,
                            cost_usd=_cost,
                            client_id=client_id or "",
                            params=_cache_key_params(body),
                        )
                    except Exception as exc:
                        log.warning("Semantic cache write error (non-fatal): %s", exc)
                except Exception as exc:
                    log.warning("Cache write error (non-fatal): %s", exc)

            # Replay as SSE: the answer may come from the provider that served
            # the request in the CLI's place, tool calls included.
            content = (result.get("choices") or [{}])[0].get("message", {}).get(
                "content", ""
            ) or ""
            _record_session_turn(
                client_id, session_id, list(body.get("messages") or []), content
            )
            return StreamingResponse(
                _replay_completion_as_sse(
                    result,
                    completion_id=result.get("id", "chatcmpl-subproc"),
                    created=result.get("created", int(time.time())),
                    model=result.get("model", body.get("model", "")),
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-Cache": "MISS",
                },
            )

        try:
            gen = forward_stream(body, **route_options)
            first = await _unless_client_gone(request, gen.__anext__())
        except _ClientGone:
            await _aclose_quietly(gen)
            return _client_gone_response(body.get("model", "?"))
        except StopAsyncIteration:
            return StreamingResponse(
                iter([]),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        except HTTPException:
            raise
        except Exception as exc:
            return _chat_error_response(exc, client_id, "stream")

        _key_id = getattr(request.state, "key_id", "")
        _hold = getattr(request.state, "budget_hold", None)

        async def _stream_and_bill(first_chunk: bytes, rest, cid, req_body, kid):
            """Yield chunks in real-time and append a final usage chunk.

            OpenAI's `stream_options.include_usage` is honoured by OpenAI
            and GitHub Models but NOT by Ollama or most other providers.
            Without this injection, the Playground
            never sees tokens/cost for those upstreams.

            We must preserve the real-time streaming UX — so each complete
            event goes out as soon as it is in; only an event cut by a read,
            and `[DONE]` with what follows, are held back (``tail_buf``), so
            the usage chunk can be spliced in before `[DONE]` (OpenAI
            clients stop reading there) and a cut stream ends between two
            events (PLU-26).
            """
            import json as _json_mod

            accumulated = bytearray(first_chunk)
            # The first chunk is already consumed by the caller's probe; a
            # short answer can arrive whole in this one read.
            tail_buf = bytearray(first_chunk)
            ready = _complete_events(tail_buf)
            if ready:
                yield ready
            completed = False
            cut: Optional[Exception] = None
            try:
                async for chunk in rest:
                    accumulated.extend(chunk)
                    tail_buf.extend(chunk)
                    ready = _complete_events(tail_buf)
                    if ready:
                        yield ready
                completed = True
            except Exception as exc:
                if _done_event_at(tail_buf) != -1:
                    # The provider had finished: a late read error is moot.
                    completed = True
                else:
                    # The provider broke off after the first chunk (PLU-26):
                    # no other provider can take over a stream already begun.
                    cut = exc
                    log.warning("Stream cut by the provider: %s", exc)
            finally:
                if not completed:
                    # Client gone (cancellation or generator close) or upstream
                    # read error: the provider has already generated these
                    # tokens, so record them before letting the error through.
                    if cid:
                        try:
                            # On the loop: a cancelled request cannot await
                            # here (the rare path; PLU-27 moves the others).
                            _bill_streaming(
                                cid,
                                req_body,
                                bytes(accumulated),
                                key_id=kid,
                                hold=_hold,
                                # Counts only when the usage event came before
                                # the cut; without it this changes nothing.
                                reported=_usage_reported(),
                            )
                            log.info(
                                "Stream interrupted: billed the partial answer "
                                "(%d bytes)",
                                len(accumulated),
                            )
                        except Exception as exc:
                            log.warning("Stream billing error (non-fatal): %s", exc)
                            _keep_hold(_hold)
                    await _aclose_quietly(rest)
            if cut is not None:
                # What came, in whole events, then a readable end: an error
                # event and [DONE], never the provider's own text (it may name
                # its address). An event the cut broke in two is dropped.
                yield _stream_cut_event()
                yield b"data: [DONE]\n\n"
                return
            # At this point `tail_buf` holds what was held back (an event cut
            # by a read, `[DONE]` and what follows) and has NOT been yielded.

            bill = None
            cancelled: Optional[BaseException] = None
            if cid:
                if _hold is not None:
                    _hold.settling = True  # never given back while it is billed
                try:
                    # Off the loop, waited for to the end (PLU-27): a client
                    # that hangs up now cannot leave the hold given back and
                    # the session's turn unrecorded before the usage is written.
                    bill, cancelled = await _to_thread_to_the_end(
                        functools.partial(
                            _bill_streaming,
                            cid,
                            req_body,
                            bytes(accumulated),
                            key_id=kid,
                            hold=_hold,
                            # Read here, in the task that relayed the stream:
                            # the route set it when the CLI's answer ended.
                            reported=_usage_reported(),
                        )
                    )
                except Exception as exc:
                    log.warning("Stream billing error (non-fatal): %s", exc)
                    _keep_hold(_hold)
            if bill is not None and cid and cid != "admin":
                # As a whole answer is: the spending alert follows its billing
                # (a CLI stream used to be billed through forward() and had it).
                try:
                    _send_spending_alert(
                        cid,
                        bill["pricing_month"],
                        key_record=key_record,
                        current_total_usd=spending_total + bill["cost_usd"],
                    )
                except Exception as exc:
                    log.warning("Spending alert error (non-fatal): %s", exc)
            if bill is not None:
                # Complete stream only: an interrupted one left above.
                _record_session_turn(
                    cid, session_id, list(req_body.get("messages") or []), bill["text"]
                )
            if cancelled is not None:
                raise cancelled

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

            done_idx = _done_event_at(tail_buf)
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
        result = await _unless_client_gone(request, forward(body, **route_options))
    except _ClientGone:
        return _client_gone_response(body.get("model", "?"))
    except HTTPException:
        raise
    except Exception as exc:
        return _chat_error_response(exc, client_id, "forward")
    elapsed = time.perf_counter() - t0
    log.info("<- OK | model=%s | %.2fs", body.get("model", "?"), elapsed)

    # -- Billing --------------------------------------------------------------
    billing_info = await _charge_answer(
        request, client_id, key_record, spending_total, body, result
    )

    # Inject billing into response + headers
    if billing_info:
        result["billing"] = billing_info

    if session_id and client_id:
        assistant_text = ""
        try:
            choice = (result.get("choices") or [{}])[0]
            assistant_text = (choice.get("message") or {}).get("content") or ""
        except (AttributeError, IndexError, TypeError):
            pass
        _record_session_turn(
            client_id, session_id, list(body.get("messages", []) or []), assistant_text
        )

    # Cache the response
    if _use_cache and _cache_key:
        try:
            from plugboard.cache import set_cached

            cost = billing_info.get("cost_usd", 0) if billing_info else 0
            await _asyncio.to_thread(
                set_cached,
                _cache_key,
                body.get("model", ""),
                _cache_key[:16],
                result,
                cost_usd=cost,
                client_id=client_id or "",
            )
            try:
                from plugboard import semantic_cache as _sem
                from plugboard.cache import key_params as _cache_key_params

                await _sem.store_async(
                    body.get("model", ""),
                    body.get("messages", []),
                    result,
                    cost_usd=cost,
                    client_id=client_id or "",
                    params=_cache_key_params(body),
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


# A probe or call failure hides a model only while it is fresh: probes are
# periodic (nightly), and a stale failure says nothing about the model today.
_PROBE_FAILURE_MAX_AGE_S = 36 * 3600


def _probe_age(item: Dict[str, Any]) -> Optional[float]:
    """Seconds since the entry's last connectivity probe, or None."""
    probed = item.get("last_probed_at")
    if not isinstance(probed, str):
        return None
    try:
        when = datetime.fromisoformat(probed.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds()


def _not_callable_now(item: Dict[str, Any]) -> bool:
    """A catalogue entry whose last observation, a probe or the route's real
    calls (PLU-55, read by ``call_outcome.last_call``: failures up to the
    breaker's threshold, the last neither a timeout nor a quota), is a recent
    failure.

    The newer of the two decides: a successful call after a failed probe
    brings the model back, and so does a successful probe after a failed
    call. A model never probed nor called, or failed long ago, stays listed.
    Quarantine is not used: it is keyed by the bare model name, so one
    provider's refusal would hide the model at every other provider.
    """
    from plugboard.call_outcome import last_call
    from plugboard.model_identity import catalogue_native_model

    observations = []  # (age in seconds, succeeded)
    probe_age = _probe_age(item)
    if probe_age is not None:
        observations.append((probe_age, item.get("connected") is not False))
    call = last_call(item.get("owned_by") or "", catalogue_native_model(item))
    if call is not None:
        observations.append(call)
    if not observations:
        return False
    age, succeeded = min(observations, key=lambda seen: seen[0])
    return not succeeded and age <= _PROBE_FAILURE_MAX_AGE_S


async def _models_for_key(
    request: Request, client_id: Optional[str]
) -> List[Dict[str, Any]]:
    """The catalogue as the calling key sees it, for every model list route.

    Chat's allowed_models rule decides: a limited key only gets the models it
    may call; the admin and unlimited keys get every model. A client key also
    loses a model whose last probe failed within 36 hours. Entries are copies,
    so the shared models cache never carries one caller's flag.
    """
    key_record = _get_request_key_record(request, client_id=client_id)
    # One guard for the whole list: its registry snapshot is read once.
    guard = _model_route_guard(client_id or "", key_record)
    # A client key is offered only what can be called now: a model whose last
    # probe failed recently is left out. The admin keeps the full view.
    callable_only = bool(client_id) and client_id != "admin"
    return [
        dict(item, account_compatible=True)
        for item in await list_models()
        if _guard_accepts(guard, item.get("id", ""))
        and not (callable_only and _not_callable_now(item))
    ]


@app.get("/v1/models")
async def models(request: Request, client_id: Optional[str] = Depends(_resolve_client)):
    from plugboard.legacy_connectors import is_removed_catalogue_model
    from plugboard.model_identity import catalogue_native_model

    items = await _models_for_key(request, client_id)
    provider_ids = tuple(p["id"] for p in load_providers())
    items = [
        item
        for item in items
        if not is_decision_model(
            catalogue_native_model(item, provider_ids=provider_ids), effective_name=True
        )
        and not is_removed_catalogue_model(item)
    ]
    return {"object": "list", "data": items}


class _PreflightItem(BaseModel):
    provider: str
    model: str


_PREFLIGHT_MAX_ITEMS = 10


class _PreflightRequest(BaseModel):
    items: List[_PreflightItem] = Field(max_length=_PREFLIGHT_MAX_ITEMS)


def _preflight_refusal(
    client_id: Optional[str], model_str: str, key_record: Optional[Dict[str, Any]]
) -> Optional[str]:
    """Closed error code of the first chat admission check the item fails.

    The chat route's checks, in its order. The key's own checks apply to a
    client key only; ``provider_disabled`` (router.disabled_route, PLU-12) and
    ``model_not_found`` (router.is_unknown_model) apply to every caller, after
    the key's 403 so a limited key cannot probe which IDs exist, and before
    the 402 and the rpm count.
    """
    keyed = bool(client_id) and client_id != "admin"

    def _enabled() -> None:
        if disabled_route(model_str) is not None:
            raise HTTPException(503, "provider_disabled")

    def _known() -> None:
        if is_unknown_model(model_str) or is_unserved_cli_model(model_str):
            raise HTTPException(404, "model_not_found")

    checks = (
        (
            "budget_exceeded",
            keyed,
            lambda: _check_spending_limit(client_id, key_record),
        ),
        (
            "forbidden_model",
            keyed,
            lambda: _check_model_access(client_id, model_str, key_record=key_record),
        ),
        ("provider_disabled", True, _enabled),
        ("model_not_found", True, _known),
        (
            "price_unavailable",
            keyed,
            lambda: _check_price_known(client_id, model_str, key_record),
        ),
        ("rate_limited", keyed, lambda: _check_client_rpm(client_id, key_record)),
    )
    for code, applies, check in checks:
        if not applies:
            continue
        try:
            check()
        except _UsageUnavailable:
            raise  # the whole batch: 503 usage_unavailable (D-37)
        except HTTPException:
            return code
    return None


@app.post(
    "/v1/preflight",
    # The key first (PLU-15): see _inflight_chat_dep.
    dependencies=[Depends(_resolve_client), Depends(_inflight_chat_dep)],
)
async def preflight(
    request: Request,
    body: _PreflightRequest,
    client_id: Optional[str] = Depends(_resolve_client),
):
    """Smoke-test a batch of provider+model pairs before a long benchmark.

    Each ping goes through the chat admission checks (allowed_models, budget,
    a provider that is not turned off, a model some provider knows, known
    price, rpm) and is billed like a chat call. The batch holds one slot of
    the global cap, given back while a ping waits for a full provider and
    taken back right before a provider is called (BUG-77, PLU-51): a pair it
    cannot ping for want of a slot answers ``server_busy``. Errors are closed codes, never the
    provider's text.
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
            # With no provider on, a model of a disabled one does not
            # resolve: _preflight_refusal names it provider_disabled.
            if disabled_route(model_str) is None:
                error = "unknown_model"
        if error is None:
            error = _preflight_refusal(client_id, model_str, key_record)
        if error is None:
            # Minimal ping to verify the pipe is open
            ping_body = {
                "model": model_str,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
            # BUG-52: a budgeted key's ping holds its cost, as a chat does
            # (429 -> budget_exceeded; usage that cannot be written -> 503).
            holds: List[Optional[chat_billing.BudgetHold]] = [None]
            try:
                await _take_budget_hold(
                    client_id, key_record, ping_body, lambda h: holds.__setitem__(0, h)
                )
            except _UsageUnavailable:
                raise
            except HTTPException:
                error = "budget_exceeded"
        if error is None:
            hold = holds[0]
            try:
                # BUG-77: a ping that waited for a full provider gave the
                # global slot back; the router takes it back before a call.
                result = await _unless_client_gone(
                    request, forward(ping_body, timeout=10.0, **route_options)
                )
            except _ClientGone:
                if hold is not None:
                    await _asyncio.to_thread(hold.release)
                # Nobody waits for the rest of the batch: stop here.
                return _client_gone_response(model_str)
            except _ChatOverloaded:
                # The global cap stayed full: this pair is not pinged (or its
                # provider's slot went back), the results so far are kept.
                error = "server_busy"
            except HTTPException:
                error = "forbidden_model"
            except ProvidersBusyError as exc:
                # Full, not failed: nothing was called (D-23).
                log.info("preflight busy | model=%s | %s", model_str, exc)
                error = "providers_busy"
            except ProviderDisabledError:
                # Turned off between the checks and the ping: nothing called.
                error = "provider_disabled"
            except Exception as exc:
                log.warning("preflight error | model=%s | %s", model_str, exc)
                error = "upstream_error"
            else:
                if client_id:
                    try:
                        _bill_completion(
                            client_id, key_id, ping_body, result, hold=hold
                        )
                    except Exception as exc:
                        log.warning("Preflight billing error (non-fatal): %s", exc)
                        _keep_hold(hold)
            if hold is not None and not hold.done:
                await _asyncio.to_thread(hold.release)  # not billed: given back
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
async def models_with_benchmarks(
    request: Request, client_id: Optional[str] = Depends(_resolve_client)
):
    """List all models enriched with benchmarks, capabilities, and metadata."""
    from plugboard.benchmarks import enrich_models_list

    items = await _models_for_key(request, client_id)
    return {"object": "list", "data": enrich_models_list(items)}


@app.get("/v1/models/{model_id:path}/info")
async def model_info(
    model_id: str,
    request: Request,
    client_id: Optional[str] = Depends(_resolve_client),
):
    """Get detailed info for a specific model: benchmarks, capabilities, best_for."""
    from plugboard.benchmarks import derived_model_info, get_model_info

    if client_id and client_id != "admin":
        # A client key reads the routes chat would use for this spelling
        # (PLU-60), whatever the spelling: the admin keeps the full view.
        key_record = _get_request_key_record(request, client_id=client_id)
        guard = _model_route_guard(client_id, key_record)
        disabled = disabled_route(model_id)
        try:
            # A model the key may not call is refused as chat refuses it:
            # 403 forbidden_model, same detail, no profile. Chat checks the
            # first route, then every route it would try, in order.
            if guard is not None:
                guard(*(disabled or resolve_provider_and_model(model_id)))
            routes = [disabled] if disabled else chat_routes(model_id, guard)
        except (ValueError, ModelNotServedError):
            # No route chat would use (no active provider, a decision model,
            # a name the CLI routes refuse): the unknown-model answer, never
            # the curated profile.
            return derived_model_info(model_id)
        # So does a model the client's catalogue leaves out (_models_for_key):
        # every route chat would use was probed and failed recently.
        failing = set()
        for item in await list_models():
            if _not_callable_now(item):
                try:
                    provider, native = resolve_provider_and_model(item.get("id", ""))
                except ValueError:
                    continue
                failing.add((provider["id"], native))
        if routes and all((p["id"], m) in failing for p, m in routes):
            return derived_model_info(model_id)
    info = get_model_info(model_id)
    if not info:
        raise HTTPException(404, f"No benchmark data for model '{model_id}'")
    return info


# -- /v2/ aliases (API versioning) -------------------------------------------
# v2 endpoints are identical to v1 for now. The version header allows
# clients to opt-in to future breaking changes.


@app.post(
    "/v2/chat/completions",
    # The key first (PLU-15): see _inflight_chat_dep.
    dependencies=[
        Depends(_resolve_client),
        Depends(_refuse_anonymous_chat),
        Depends(_inflight_chat_dep),
        Depends(_session_turn_dep),
    ],
)
async def chat_completions_v2(
    request: Request, client_id: Optional[str] = Depends(_resolve_client)
):
    resp = await chat_completions(request, client_id)
    resp.headers["X-API-Version"] = "2"
    return resp


@app.get("/v2/models")
async def models_v2(
    request: Request, client_id: Optional[str] = Depends(_resolve_client)
):
    items = await _models_for_key(request, client_id)
    return {"object": "list", "data": items, "api_version": "2"}


@app.get("/v2/models/benchmarks")
async def models_benchmarks_v2(
    request: Request, client_id: Optional[str] = Depends(_resolve_client)
):
    from plugboard.benchmarks import enrich_models_list

    items = await _models_for_key(request, client_id)
    return {"object": "list", "data": enrich_models_list(items), "api_version": "2"}


# -- /health -----------------------------------------------------------------


@app.get("/health")
async def health():
    """Liveness (PLU-31): always 200 while the process answers. Public, so it
    shows the status and the version only and reads no file; the provider
    list is on the admin `GET /providers`, the auth posture on
    `GET /health/ready`."""
    return {"ok": True, "version": __version__}


@app.get("/health/ready")
async def health_ready(request: Request):
    """Readiness (PLU-31): 200 when this instance can serve requests, 503
    otherwise. Public: a load balancer reads the status code with no key.

    The body is ``ready`` and ``version``; ``checks`` (generic states: which
    check failed, the client auth posture) is added for a valid key only
    (PLU-75): an active client key, the admin key (no TOTP: a read of
    generic states) or an admin session, a person's session included; a
    bare personal key (``pba_``), even with its code, gets no checks
    (PB-AUTH-01). A wrong, forged or revoked key is
    not refused here: it gets the same body as no key. With both API_KEY and
    ADMIN_KEY empty every route is open (loopback only), so are the checks.
    """
    from plugboard import readiness

    result = await readiness.evaluate(
        request.app.state, api_key=_API_KEY, dev_mode=_DEV_MODE
    )
    body = {"ready": result["ready"], "version": __version__}
    if (not _API_KEY and not _ADMIN_KEY) or _key_scope(request):
        body["checks"] = result["checks"]
    return JSONResponse(body, status_code=200 if result["ready"] else 503)


@app.get("/health/detailed")
async def health_detailed(_: None = Depends(_require_admin)):
    """Detailed health check — probes every active provider.

    Uses the same probe as the dashboard Ping (health_probe.probe_provider):
    CLI providers are checked through their binary, local servers through
    the URL they really serve, and the provider's credentials are sent.
    """
    import asyncio

    from plugboard.health_probe import probe_provider

    active = [p for p in load_providers() if p.get("is_active", True)]
    probes = await asyncio.gather(*(probe_provider(p) for p in active))
    results = [{"provider": p["id"], **r} for p, r in zip(active, probes)]
    all_ok = all(r.get("ok") for r in results)
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


@app.get("/v1/inflight")
async def inflight_me(client_id: Optional[str] = Depends(_resolve_client)):
    """Chat calls in flight and the last results (PLU-13).

    The admin sees every call with its provider; a client key sees its own
    calls only, without routing details.
    """
    if client_id is None:
        raise HTTPException(401, "Authentication required")
    return _live.snapshot(client_id=client_id, admin=client_id == "admin")


@app.get("/admin/inflight", dependencies=[Depends(_require_admin)])
async def inflight_admin():
    """Every chat call in flight and the last results, with routing details."""
    return _live.snapshot(admin=True)


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
    return get_stats(per_model=True)


# Concurrency limit for the bulk internet check. Each probe is a real LLM
# call; running 30+ in parallel would saturate provider rate limits. Tunable
# via env. Cooldown is shared with /admin/models/refresh to prevent admins
# from spamming both endpoints.
_BULK_INTERNET_CONCURRENCY = max(
    1, envcompat.get_int("PLUGBOARD_BULK_INTERNET_CONCURRENCY", default=3)
)
_BULK_INTERNET_PER_MODEL_TIMEOUT_S = max(
    5.0,
    envcompat.get_float("PLUGBOARD_BULK_INTERNET_PER_MODEL_TIMEOUT_S", default=25.0),
)
_BULK_INTERNET_LOCK = _asyncio.Lock()


# -- /admin/circuits, /admin/models/check-internet: admin_routes/model_probes.py
_model_probes = _admin_model_probes.register(app, _THIS_MODULE)
check_model_internet_access = _model_probes.check_model_internet_access
check_all_models_internet_access = _model_probes.check_all_models_internet_access


# -- /admin/models/status and /admin/models/refresh --------------------------

_MODEL_REFRESH_LOCK = _asyncio.Lock()
_MODEL_REFRESH_COOLDOWN_S: float = envcompat.get_float(
    "PLUGBOARD_REFRESH_COOLDOWN_S", default=60.0
)
_LAST_REFRESH_AT: float = 0.0  # time.monotonic() stamp of last successful refresh


# -- /admin/cache ... /admin/audit, /metrics/prometheus: admin_routes/monitoring.py
_monitoring = _admin_monitoring.register(app, _THIS_MODULE)
admin_models_refresh = _monitoring.admin_models_refresh


# -- Provider management -----------------------------------------------------


def _require_parsable_base_url(base_url: str, label: str) -> None:
    """Refuse (422) an address that cannot be parsed, as the guard does.

    An unclosed IPv6 bracket, a bracketed name or a bad port (urlsplit,
    .port), or an empty or over-long label in an ASCII host (the IDNA step of
    getaddrinfo, which aiohttp lets through): a 500 on the connection test
    (BUG-63), and a provider saved that no request could reach (BUG-66). A
    non-ASCII host is encoded by aiohttp, which reports it.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url)
        parts.port  # parsed on read
        host = parts.hostname or ""
        if host.isascii():
            host.encode("idna")
    except ValueError as exc:
        raise HTTPException(422, f"{label} refused: not a valid address ({exc})")


# -- /providers: admin_routes/providers.py
_providers = _admin_providers.register(app, _THIS_MODULE)
get_providers = _providers.get_providers


# -- /admin/oauth, /oauth/callback: admin_routes/oauth.py
_oauth = _admin_oauth.register(app, _THIS_MODULE)
oauth_disconnect = _oauth.oauth_disconnect


# -- Egress guard shared by /providers and /admin/direct-providers


async def _require_public_host(base_url: str, *, label: str = "Catalog preset") -> list:
    """Refuse a base URL whose host resolves to a non-public address.

    Catalog base URLs come from a third-party feed; an admin key must not be
    sent to an internal address because that feed says so. ``label`` names
    what is refused in the error message.
    """
    from urllib.parse import urlsplit

    from plugboard.llm_pricing import UnsafeURL, _resolve_public

    try:
        parts = urlsplit(base_url)
        # The checked addresses are returned so the caller connects to them
        # and nothing else (no second lookup that DNS rebinding could change).
        return await _resolve_public(parts.hostname or "", parts.port or 443)
    except UnsafeURL as exc:
        if str(exc).startswith("cannot resolve"):
            raise HTTPException(503, f"{label} host unreachable: {exc}")
        raise HTTPException(422, f"{label} refused: {exc}")
    except ValueError as exc:
        # A host the IDNA codec rejects (UnicodeError from the lookup), a bad
        # port or an unclosed IPv6 bracket: refused, not a 500 (BUG-62).
        raise HTTPException(422, f"{label} refused: not a valid address ({exc})")


async def _check_guard_on_edit(
    provider_id: str, before: Dict[str, Any], updates: Dict[str, Any]
) -> None:
    """Apply the egress guard rules to ``PUT /providers/{id}`` (D-19).

    An imposed guard cannot be turned off (422) and is never written. When
    the provider is protected after the edit, a changed ``base_url`` (compared
    without a trailing ``/``) or a guard just ticked must point to a public
    http(s) address. A ``base_url`` sent back unchanged triggers no lookup:
    the edit form always sends it. ``updates`` is changed in place.
    """
    forced = netguard.guard_is_forced(before)
    if forced and "egress_guard" in updates:
        if updates["egress_guard"] is False:
            raise HTTPException(
                422,
                "The egress guard is always on for catalog and built-in cloud presets",
            )
        del updates["egress_guard"]
    protected_after = forced or updates.get(
        "egress_guard", before.get("egress_guard") is True
    )
    if not protected_after:
        return
    current_url = before.get("base_url") or ""
    new_url = updates.get("base_url", current_url)
    url_changed = new_url.rstrip("/") != current_url.rstrip("/")
    turned_on = not netguard.is_protected(before)
    if url_changed or turned_on:
        await _require_guard_allows(provider_id, new_url)


async def _require_guard_allows(
    provider_id: str, base_url: str, *, actor: str = "admin"
) -> None:
    """Refuse to guard a provider whose base_url is not a public http(s) URL.

    The egress guard would refuse every connection of such a provider, so it
    is refused up front (D-18 c, D-19). Every refusal is audited as
    ``provider.egress.rejected`` (by ``actor``) before it is raised.
    """
    try:
        if not base_url.lower().startswith(("http://", "https://")):
            raise HTTPException(422, "The egress guard needs an http(s) base_url")
        await _require_public_host(base_url, label="Provider base_url")
    except HTTPException as exc:
        _audit(
            "provider.egress.rejected",
            actor=actor,
            target_type="provider",
            target_id=provider_id,
            details=json.dumps({"base_url": base_url, "reason": exc.detail}),
        )
        raise


# -- /admin/direct-providers: admin_routes/direct_providers.py
_direct_providers = _admin_direct_providers.register(app, _THIS_MODULE)
_test_openai_endpoint = _direct_providers._test_openai_endpoint
_schedule_initial_discovery = _direct_providers._schedule_initial_discovery


_background_tasks: set = set()


# -- /admin/ollama: admin_routes/ollama.py
_admin_ollama.register(app, _THIS_MODULE)


# -- /admin/keys, /admin/orgs: admin_routes/keys.py
_keys = _admin_keys.register(app, _THIS_MODULE)
_default_rpm_limit = _keys._default_rpm_limit
_default_monthly_limit_usd = _keys._default_monthly_limit_usd


# -- /admin/jobs: admin_routes/jobs.py
_admin_jobs.register(app, _THIS_MODULE)


# -- /admin/pricing, schedulers, auto-update, catalog history, coverage:
#    admin_routes/pricing.py
_pricing = _admin_pricing.register(app, _THIS_MODULE)
_suggestion_action = _pricing._suggestion_action
admin_catalog_history = _pricing.admin_catalog_history


# -- /admin/usage, /admin/retention: admin_routes/usage.py
_admin_usage.register(app, _THIS_MODULE)


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
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,sans-serif;background:#f8fafc;color:#0f172a;
display:flex;align-items:center;justify-content:center;min-height:100vh;min-height:100dvh;padding:1rem}
.box{background:#fff;padding:clamp(1.25rem,6vw,2.5rem);border-radius:18px;width:400px;max-width:100%;margin:auto;flex-shrink:0;
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
.err{color:#dc2626;font-size:.82rem;min-height:1.2em;margin-top:.25rem;overflow-wrap:anywhere}
@media(max-width:600px),(any-pointer:coarse){input{font-size:1rem}input,button{min-height:44px}}
</style></head><body>
<div class="box">
<div class="logo">E</div>
<h2>Plugboard</h2>
<p class="sub">Enter your admin key or your personal key</p>
<form id="f">
<input type="password" id="k" placeholder="Admin or personal key" autofocus>
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
  e.textContent=(payload && payload.detail) || 'Invalid key or code';
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
    # Code from the secret being replaced; required once one is enrolled.
    current_code: Optional[str] = None


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
_SUSPICIOUS_REFUSALS = {"proxied", "non_loopback_peer", "bad_token"}
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


def _local_login_proof(request: Request) -> tuple:
    """("device" | "token", "") when the browser shows a secret only the
    operator holds, else ("", refusal reason).

    The loopback and header checks prove nothing against a same-machine
    container, VM or forwarder; this does: a remembered-browser cookie, or
    the single-use token from data/.login_token (opened by ``make dev``).
    """
    from plugboard import local_login

    if local_login.is_remembered(request.cookies.get(local_login.DEVICE_COOKIE, "")):
        return "device", ""
    token = request.query_params.get("token", "")
    if not token:
        return "", "no_local_secret"
    if local_login.consume_token(token):
        return "token", ""
    return "", "bad_token"


def _issue_local_login_token() -> None:
    """At startup, write a fresh login token when auto-login can be used."""
    mode = _auto_login_mode()
    if mode == "off" or not _effective_admin_key():
        return
    if mode == "auto" and not _is_loopback_address(_bind_host()):
        return
    from plugboard import local_login

    try:
        local_login.issue_token()
    except OSError as exc:
        log.warning("auth.auto_login cannot write the login token: %s", exc)
        return
    log.info(
        "auth.auto_login sign in once with the link `make dev` opens, or "
        "/login?token=<content of %s>; the browser is then remembered",
        local_login.token_path(),
    )


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    """Public login page. Auto-login only for a local install opened locally
    by a browser holding the operator's secret (login token or remembered)."""
    refusal = _auto_login_refusal(request)
    proof = ""
    if not refusal:
        proof, refusal = _local_login_proof(request)
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
                details=f"localhost auto-login host={host} via={proof}",
                ip=peer,
                principal=_ADMIN_KEY_PRINCIPAL,
            )
            resp = RedirectResponse(
                url=_safe_next(request.query_params.get("next", "")), status_code=302
            )
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
            if proof == "token":
                from plugboard import local_login

                # Remember this browser so the next visits need no token.
                resp.set_cookie(  # NOSONAR
                    local_login.DEVICE_COOKIE,
                    local_login.remember_device(),
                    max_age=local_login.DEVICE_MAX_AGE,
                    httponly=True,
                    samesite="strict",
                    secure=False,
                    path="/login",
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

    client_ip = _client_address(request)
    address = _address_scope(request)
    token = body.key.strip()
    is_admin_key = _is_admin_key(token)
    # A live personal key (PB-AUTH-01); without ADMIN_KEY it opens nothing
    # and counts as a wrong key.
    personal = (
        not is_admin_key
        and token.startswith(admin_people.KEY_PREFIX)
        and bool(_effective_admin_key())
    )
    person = admin_people.find_by_key(token) if personal else None
    known = is_admin_key or person is not None

    # Brute-force protection (BUG-119): wrong keys lock their address, but
    # the address lockout never refuses a valid key (the admin key or a live
    # personal key); behind a proxy anyone could otherwise lock them out.
    # Checked before the store refusal below, so a locked address gets no
    # free guess while the people store is unreadable (PB-AUTH-01).
    if not known and not check_auth_rate_limit(address):
        remaining = get_lockout_remaining(address)
        _audit(
            "auth.login_blocked", details=f"IP locked out: {client_ip}", ip=client_ip
        )
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many failed attempts. Try again later."},
            headers={"Retry-After": str(remaining)},
        )

    if personal and person is None and not admin_people.is_readable():
        # A fault of the store, not a wrong key: refuse and count nothing,
        # as on the TOTP header.
        log.error("auth rejected reason=person_store_unavailable")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
        )

    if not known:
        locked = record_auth_failure(address)
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

    if person is not None:
        return _person_login(request, body, person)

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
        # Wrong codes need the admin key: they lock the admin key, from any
        # address (BUG-119).
        if not check_auth_rate_limit(_ADMIN_SCOPE):
            remaining = get_lockout_remaining(_ADMIN_SCOPE)
            _audit(
                "auth.login_blocked",
                details=f"admin key locked out (MFA), IP: {client_ip}",
                ip=client_ip,
                principal=_ADMIN_KEY_PRINCIPAL,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many failed attempts. Try again later."},
                headers={"Retry-After": str(remaining)},
            )
        if not _mfa.verify(code):
            locked = record_auth_failure(_ADMIN_SCOPE)
            _audit(
                "auth.mfa_failed",
                details=f"IP: {client_ip}, locked={locked}",
                ip=client_ip,
                principal=_ADMIN_KEY_PRINCIPAL,
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
        _audit(
            "auth.mfa_success",
            details=f"IP: {client_ip}",
            ip=client_ip,
            principal=_ADMIN_KEY_PRINCIPAL,
        )

    session_token = _create_admin_session_token(mfa=mfa_checked)
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
        )

    _set_principal(request, _ADMIN_KEY_PRINCIPAL)
    _audit("auth.login_success", details=f"IP: {client_ip}", ip=client_ip)
    return _session_response(request, session_token)


def _person_login(
    request: Request, body: AdminLogin, person: Dict[str, Any]
) -> Response:
    """Open a session with a live personal key and its own code (PB-AUTH-01).

    The code is always required, and checked like on the TOTP header
    (``_prove_person_code``): wrong codes lock that person only. Every entry
    names the person.
    """
    from plugboard.ratelimit import get_lockout_remaining

    client_ip = _client_address(request)
    generation = person.get("generation")
    sound = _person_record_is_sound(person)
    if sound and not (isinstance(generation, str) and generation):
        # A session without a generation could never be checked again.
        log.error("auth rejected reason=person_record_malformed")
        sound = False
    if not sound:  # a hand-edited record opens nothing; nothing is counted
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
        )
    person_id = person["id"]
    principal = _person_principal(person)
    name = str(person.get("name", ""))
    code = (body.totp_code or "").strip()
    if not code:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "MFA code required", "mfa_required": True},
        )
    outcome = _prove_person_code(request, person, code, via="login")
    if outcome == "locked":
        remaining = get_lockout_remaining(_person_scope(person_id))
        _audit(
            "auth.login_blocked",
            target_type="admin_person",
            target_id=person_id,
            details=f"personal key locked out (MFA), IP: {client_ip}",
            ip=client_ip,
            principal=principal,
            principal_name=name,
        )
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many failed attempts. Try again later."},
            headers={"Retry-After": str(remaining)},
        )
    if outcome == "refused_locked":
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many failed attempts. Try again later."},
            headers={"Retry-After": "900"},
        )
    if outcome == "refused":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid MFA code"
        )
    if outcome != "ok":  # the store failed: refuse, count nothing
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
        )
    session_token = _create_admin_session_token(person=person)
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
        )
    _set_principal(request, principal, name)
    _audit(
        "auth.login_success",
        details=f"IP: {client_ip}",
        ip=client_ip,
        principal=principal,
        principal_name=name,
    )
    return _session_response(request, session_token)


def _session_response(request: Request, session_token: str) -> Response:
    """204 that sets the admin session cookie (and drops the legacy one)."""
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


def _session_record_author(nonce: str) -> Tuple[str, str]:
    """``(principal, name)`` of a session no longer valid, from its record.

    A logout still names who opened the session once that person is revoked
    (or the people store is unreadable: no name then), or once an MFA change
    refused an ``ADMIN_KEY`` session. ``("", "")`` without a record or with a
    hand-edited ``person_id``.
    """
    record = admin_sessions.lookup(nonce) if nonce else None
    if record is None:
        return ("", "")
    if "person_id" not in record:
        return (_ADMIN_KEY_PRINCIPAL, "")
    person_id = record.get("person_id")
    if not isinstance(person_id, str) or not admin_people.ID_PATTERN.fullmatch(
        person_id
    ):
        return ("", "")
    person = admin_people.get(person_id)
    return ("person:" + person_id, str(person.get("name", "")) if person else "")


@app.post("/auth/admin/logout", include_in_schema=False, status_code=204)
async def admin_logout(request: Request):
    """Revoke this browser's session server-side, then clear its cookie."""
    session = request.cookies.get(_ADMIN_COOKIE_NAME, "")
    nonce = _admin_session_nonce(session)
    # The author is read before the revocation: the entry names who left.
    found = _admin_session_principal(session) if nonce else None
    principal, name = found or _session_record_author(nonce)
    if nonce and admin_sessions.revoke(nonce):
        _audit(
            "auth.logout",
            details="admin session revoked",
            principal=principal,
            principal_name=name,
        )
    resp = Response(status_code=204)
    resp.delete_cookie(_ADMIN_COOKIE_NAME, path="/")
    resp.delete_cookie("llm_admin_key", path="/")
    return resp


@app.post("/auth/admin/logout-all", include_in_schema=False, status_code=204)
async def admin_logout_all(request: Request, _: None = Depends(_require_admin)):
    """Revoke every admin session (this one included), then clear its cookie.

    Browsers remembered for local auto-login are forgotten first: otherwise
    their next visit to /login would open a new session at once.
    """
    from plugboard import local_login

    try:
        local_login.forget_devices()
        admin_sessions.revoke_all()
    except OSError as exc:
        log.error("Admin sessions not revoked: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Sessions could not be revoked: the session store is not writable.",
        ) from exc
    _audit("auth.logout_all", details="every admin session revoked")
    resp = Response(status_code=204)
    resp.delete_cookie(_ADMIN_COOKIE_NAME, path="/")
    resp.delete_cookie("llm_admin_key", path="/")
    return resp


# ── Admin MFA (TM-001) ──────────────────────────────────────────────────────


def _refuse_person_principal(request: Request) -> None:
    """The second factor of ADMIN_KEY is managed with ADMIN_KEY only.

    A person (personal key or person-bound session) is refused before any
    code is read, so a person's codes never count on ``key:admin`` nor
    reach the audit of ADMIN_KEY's factor (PB-AUTH-01). The refusal is
    audited once, under the person's own principal and name.
    """
    principal = getattr(request.state, "admin_principal", _ADMIN_KEY_PRINCIPAL)
    if principal != _ADMIN_KEY_PRINCIPAL:
        _audit(
            "admin.mfa.person_refused",
            target_type="admin",
            target_id="admin",
            details=f"{request.method} {request.url.path}",
            ip=_client_address(request),
            principal=principal,
            principal_name=getattr(request.state, "admin_principal_name", ""),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The second factor of ADMIN_KEY is managed with ADMIN_KEY",
        )


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
async def admin_mfa_begin(request: Request, _: None = Depends(_require_admin)):
    """Step 1 of enrolment: server generates a fresh TOTP secret and the
    otpauth:// provisioning URI. Nothing is persisted yet — the caller must
    confirm with /admin/mfa/setup and a valid code from their authenticator.
    """
    from plugboard import admin_mfa as _mfa

    _refuse_person_principal(request)

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
    TOTP codes once PLUGBOARD_ADMIN_MFA_REQUIRED=1. Replacing an enrolled
    secret also needs a current code from it (``current_code``)."""
    from plugboard import admin_mfa as _mfa

    _refuse_person_principal(request)
    _require_mfa_step_up(request, body.current_code)
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
async def admin_mfa_verify(
    body: AdminMfaVerify, request: Request, _: None = Depends(_require_admin)
):
    """Validate a code against the currently enrolled secret (debug helper)."""
    from plugboard import admin_mfa as _mfa

    _refuse_person_principal(request)

    if not _mfa.verify((body.code or "").strip()):
        raise HTTPException(400, "Invalid TOTP code")


@app.delete("/admin/mfa", include_in_schema=False, status_code=204)
async def admin_mfa_disable(request: Request, _: None = Depends(_require_admin)):
    """Remove the stored TOTP secret. Useful for rotation or to disable MFA.

    Needs a current code in the X-Plugboard-Admin-TOTP header while a secret
    is enrolled (lost authenticator: scripts/admin_mfa_setup.py --rotate on
    the host)."""
    from plugboard import admin_mfa as _mfa

    _refuse_person_principal(request)
    _require_mfa_step_up(request)
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


def _safe_next(raw: str) -> str:
    """A local path to go back to after sign-in, else ``/dashboard``.

    Same rule as the login page script: one leading slash only, so
    ``//host`` and ``/\\host`` (read as other origins) are refused.
    """
    if (
        raw.startswith("/")
        and not raw.startswith(("//", "/\\"))
        and len(raw) <= 512
        and raw.isprintable()
    ):
        return raw
    return "/dashboard"


# -- /suivi: Kanban of Plugboard tickets (admin only) -------------------------

app.include_router(build_suivi_router(_require_admin))
app.include_router(
    build_people_router(_require_admin, lambda: bool(_effective_admin_key()))
)


@app.get("/suivi", include_in_schema=False)
async def suivi_page(request: Request):
    if not _check_dashboard_auth(request):
        return RedirectResponse("/login?next=/suivi")
    file_path = _STATIC_DIR / "suivi.html"
    if not file_path.is_file():
        raise HTTPException(404, "Not found")
    return Response(content=file_path.read_bytes(), media_type="text/html")


@app.get("/dashboard", include_in_schema=False)
async def dashboard_root(request: Request):
    """Redirect /dashboard to /dashboard/ or login."""
    if not _check_dashboard_auth(request):
        return RedirectResponse("/login?next=/dashboard")
    file_path = _STATIC_DIR / "index.html"
    if not file_path.is_file():
        raise HTTPException(404, "Dashboard not found")
    return Response(content=file_path.read_bytes(), media_type="text/html")
