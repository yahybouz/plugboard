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

Admin routes are declared by `plugboard/admin_routes/` (one module per area, registered in place by `register(app, core)`, PLU-36).

The chat path (`/v1/chat/completions`, `/v1/models`, `/v1/preflight`, `/v2/*`,
`/v1/inflight`, `/usage/me`, `/v1/me`) and its limits, billing and error answers are
declared by `plugboard/chat_path/` (registered in place by `register(app, core)`,
PLU-95).

The authentication core behind the `_resolve_client` and `_require_admin` dependencies
(audit principal and limit scopes, admin key and sessions, cross-site refusal, admin and
personal-key authentication, MFA step-up, local auto-login) lives in
`plugboard/auth_core/` (registered in place by `register(app, core)`, PLU-97); both
dependencies stay here.

Authentication:
  ADMIN_KEY env var (required) -- required for admin endpoints.
  Client API keys stored in data/api_keys.json -- required for /v1/* endpoints.
  API_KEY env var -- legacy fallback if no client keys exist.
  Set API_KEY="" to disable client auth (admin auth remains enforced).
"""

from __future__ import annotations

import asyncio as _asyncio
import ipaddress
import json
import logging
import os
import pathlib
import re
import secrets
import sys
import time
import uuid
from typing import (
    Any,
    Dict,
    Optional,
)

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
)
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware

from plugboard import __version__
from plugboard import admin_people  # noqa: F401  (import order kept, PLU-97)
from plugboard import admin_sessions  # noqa: F401  (import order kept, PLU-97)
from plugboard import audit as _audit_mod
from plugboard import envcompat
from plugboard import netguard
from plugboard import trace_context as _trace
from plugboard import billing as _billing
from plugboard import usage as _usage

# Patch point read through core (PLU-95).
from plugboard.alerts import check_and_alert as _check_and_alert  # noqa: F401
from plugboard.audit import log_action as _audit
from plugboard.apikeys import (
    KEY_PREFIXES,  # ARCH-DEBT-029
    get_key_record,
    load_keys,
)
from plugboard.config import (
    dotenv_permission_warning,
    get_provider,  # noqa: F401  (patch point read through core, PLU-95)
    load_providers,
    update_provider,  # noqa: F401  (patch point read through core, PLU-36)
)
from plugboard import live_requests as _live
from plugboard.web_gate import _web_finish
from plugboard import provider_admission as _provider_admission
from plugboard import request_wait as _request_wait
from plugboard.logging_config import _request_id_var, setup_logging
from plugboard.metrics import get_stats, record_request_wait
from plugboard.ratelimit import (
    check_rate_limit,
    rate_limit_on,
)
from plugboard.decision_api import router as decision_router
from plugboard.embeddings_api import router as embeddings_router
from plugboard.admin_people_api import build_router as build_people_router
from plugboard.admin_routes import (
    auth as _admin_auth,
    direct_providers as _admin_direct_providers,
    jobs as _admin_jobs,
    keys as _admin_keys,
    model_probes as _admin_model_probes,
    monitoring as _admin_monitoring,
    oauth as _admin_oauth,
    ollama as _admin_ollama,
    pricing as _admin_pricing,
    providers as _admin_providers,
    usage as _admin_usage,
)
from plugboard.suivi_api import build_router as build_suivi_router
from plugboard.router import (
    cli_refusal,  # noqa: F401  (patch point read through core, PLU-95)
    discover_models,  # noqa: F401  (patch point read through core, PLU-36)
    forward,  # noqa: F401  (patch point read through core, PLU-95)
    forward_stream,  # noqa: F401  (patch point read through core, PLU-95)
    invalidate_models_cache,  # noqa: F401  (patch point read through core, PLU-36)
    list_models,  # noqa: F401  (patch point read through core, PLU-95)
    resolve_provider_and_model,  # noqa: F401  (patch point read through core, PLU-95)
    served_by as _served_by,  # noqa: F401  (patch point read through core, PLU-95)
)
from plugboard.chat_path import (
    account as _cp_account,
    billing as _cp_billing,
    catalogue as _cp_catalogue,
    completions as _cp_completions,
    limits as _cp_limits,
    responses as _cp_responses,
    streaming as _cp_streaming,
    v2 as _cp_v2,
)
from plugboard.auth_core import (
    admin as _ac_admin,
    auto_login as _ac_auto_login,
    scopes as _ac_scopes,
    sessions as _ac_sessions,
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
        _web_finish(request)  # PLU-158: the web slot, after the last byte
        if slot is not None:
            slot.release()
        if budget is not None:
            # PLU-46: all the request waited, once it ends (a stream after
            # its last byte, a refusal just before its 503 goes out).
            record_request_wait(budget.spent)
        _live.finish(rid)


# -- Client hang-up and SSE events: chat_path/streaming.py ---------------------
_streaming_ns = _cp_streaming.register(app, _THIS_MODULE)
_aclose_quietly = _streaming_ns._aclose_quietly
_MAX_HELD = _cp_streaming._MAX_HELD
_complete_events = _cp_streaming._complete_events
_done_event_at = _cp_streaming._done_event_at
# Read by tests/test_inflight_disconnect.py, PLU-95.
_ClientGone = _cp_streaming._ClientGone
_unless_client_gone = _cp_streaming._unless_client_gone


# ── One turn at a time per session (D-24, PLU-8) ────────────────────────────


async def _session_turn_dep(request: Request):
    """Give the session's turn back once the response is sent, and the
    budget hold of a call that was not billed (BUG-52).

    The chat handler takes the turn (its closure ``_take_session_turn``, in
    ``plugboard/chat_path/completions.py``) only when the call carries a
    session id; a yield dependency (scope "request") exits after
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
        # The caller's W3C trace context (PLU-107): kept only when valid, an
        # invalid header is ignored, never answered with a 4xx nor logged.
        trace_token = _trace.current.set(
            _trace.from_headers(
                request.headers.getlist("traceparent"),
                request.headers.getlist("tracestate"),
            )
        )
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
            _trace.current.reset(trace_token)
            _request_id_var.reset(token)


# -- Audit principal, limit and lockout scopes: auth_core/scopes.py ---------
_ac_scopes_ns = _ac_scopes.register(app, _THIS_MODULE)
_ADMIN_SCOPE = _ac_scopes._ADMIN_SCOPE
_ADMIN_KEY_PRINCIPAL = _ac_scopes._ADMIN_KEY_PRINCIPAL
_person_principal = _ac_scopes._person_principal
_person_scope = _ac_scopes._person_scope
_set_principal = _ac_scopes._set_principal
_client_address = _ac_scopes_ns._client_address
_address_scope = _ac_scopes_ns._address_scope
_key_scope = _ac_scopes_ns._key_scope
_person_bearer_scope = _ac_scopes_ns._person_bearer_scope
_rate_limit_scope = _ac_scopes_ns._rate_limit_scope


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


# -- Admin key, signed sessions, cross-site refusal: auth_core/sessions.py ---
_ac_sessions_ns = _ac_sessions.register(app, _THIS_MODULE)
_STATE_CHANGING_METHODS = _ac_sessions._STATE_CHANGING_METHODS
_CROSS_SITE_REFUSED = _ac_sessions._CROSS_SITE_REFUSED
_origin_is_this_host = _ac_sessions._origin_is_this_host
_effective_admin_key = _ac_sessions_ns._effective_admin_key
_is_admin_key = _ac_sessions_ns._is_admin_key
_create_admin_session_token = _ac_sessions_ns._create_admin_session_token
_admin_session_nonce = _ac_sessions_ns._admin_session_nonce
_admin_session_principal = _ac_sessions_ns._admin_session_principal
_is_admin_session = _ac_sessions_ns._is_admin_session
_is_cross_origin_browser_request = _ac_sessions_ns._is_cross_origin_browser_request
_refuse_cross_site_session = _ac_sessions_ns._refuse_cross_site_session
_should_use_secure_cookie = _ac_sessions_ns._should_use_secure_cookie


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


# -- Admin request auth, person codes, MFA step-up: auth_core/admin.py -------
_ac_admin_ns = _ac_admin.register(app, _THIS_MODULE)
_ADMIN_TOTP_HEADER = _ac_admin._ADMIN_TOTP_HEADER
_person_record_is_sound = _ac_admin._person_record_is_sound
_admin_request_auth = _ac_admin_ns._admin_request_auth
_person_request_auth = _ac_admin_ns._person_request_auth
_prove_person_code = _ac_admin_ns._prove_person_code
_require_mfa_step_up = _ac_admin_ns._require_mfa_step_up


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


# -- Model access check -----------------------------------------------------


class _ModelNotAllowed(HTTPException):
    """403: the key's allowed_models does not cover the model. Answered with
    ``code`` forbidden_model, like the preflight's item error."""


@app.exception_handler(_ModelNotAllowed)
async def _model_not_allowed_handler(request: Request, exc: _ModelNotAllowed):
    return JSONResponse(
        status_code=403, content={"detail": exc.detail, "code": "forbidden_model"}
    )


# -- Spending limit, budget holds, price, rpm, model access: chat_path/limits.py
_limits_ns = _cp_limits.register(app, _THIS_MODULE)
_check_spending_limit = _limits_ns._check_spending_limit
_estimate_call_cost = _limits_ns._estimate_call_cost
_take_budget_hold = _limits_ns._take_budget_hold
_hold_budget = _limits_ns._hold_budget
_check_price_known = _limits_ns._check_price_known
_check_model_access = _limits_ns._check_model_access
_model_route_guard = _limits_ns._model_route_guard
_model_is_compatible = _limits_ns._model_is_compatible
_guard_accepts = _limits_ns._guard_accepts
_spending_scope = _cp_limits._spending_scope


# -- Billing of an answer, spending alert: chat_path/billing.py --------------
_billing_ns = _cp_billing.register(app, _THIS_MODULE)
_bill_streaming = _billing_ns._bill_streaming
_bill_completion = _billing_ns._bill_completion
_bill_web_runs = _billing_ns._bill_web_runs
_take_web_runs = _billing_ns._take_web_runs
_charge_answer = _billing_ns._charge_answer
_billing_target = _billing_ns._billing_target
_send_spending_alert = _billing_ns._send_spending_alert


# -- Answers to the client, error responses: chat_path/responses.py -----------
_responses_ns = _cp_responses.register(app, _THIS_MODULE)
_provider_disabled_response = _responses_ns._provider_disabled_response
_chat_error_response = _responses_ns._chat_error_response
_retry_after_headers = _cp_responses._retry_after_headers
_quota_error_response = _cp_responses._quota_error_response
_providers_busy_response = _cp_responses._providers_busy_response
_cli_error_response = _cp_responses._cli_error_response
_model_not_served_response = _cp_responses._model_not_served_response


# -- Chat request refusals: invalid body (400), anonymous chat (401) ---------


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


# -- POST /v1/chat/completions: chat_path/completions.py ----------------------
_completions_ns = _cp_completions.register(app, _THIS_MODULE)
_record_session_turn = _completions_ns._record_session_turn
chat_completions = _completions_ns.chat_completions


# -- /v1/models, /v1/preflight, model info: chat_path/catalogue.py -----------
_catalogue_ns = _cp_catalogue.register(app, _THIS_MODULE)
models = _catalogue_ns.models
_models_for_key = _catalogue_ns._models_for_key


# -- /v2 aliases: chat_path/v2.py ----------------------------------------------
_v2_ns = _cp_v2.register(app, _THIS_MODULE)
models_v2 = _v2_ns.models_v2


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


# -- /v1/inflight, /admin/inflight, /usage/me, /v1/me: chat_path/account.py --
_cp_account.register(app, _THIS_MODULE)


# -- /metrics ----------------------------------------------------------------


@app.get("/metrics")
async def get_metrics(_: None = Depends(_require_admin)):
    return get_stats(per_model=True)


# -- Bulk internet check state, read by admin_routes/model_probes.py ----------

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


# -- /admin/circuits, /admin/models/check-internet ----------------------------
#    declared by admin_routes/model_probes.py
_model_probes = _admin_model_probes.register(app, _THIS_MODULE)
check_model_internet_access = _model_probes.check_model_internet_access
check_all_models_internet_access = _model_probes.check_all_models_internet_access


# -- Model refresh state, read by admin_routes/monitoring.py ------------------

_MODEL_REFRESH_LOCK = _asyncio.Lock()
_MODEL_REFRESH_COOLDOWN_S: float = envcompat.get_float(
    "PLUGBOARD_REFRESH_COOLDOWN_S", default=60.0
)
_LAST_REFRESH_AT: float = 0.0  # time.monotonic() stamp of last successful refresh


# -- /admin/cache ... /admin/audit, /metrics/prometheus -----------------------
#    declared by admin_routes/monitoring.py
_monitoring = _admin_monitoring.register(app, _THIS_MODULE)
admin_models_refresh = _monitoring.admin_models_refresh


# -- Base URL check shared by /providers and /admin/direct-providers ----------


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


# -- /providers: admin_routes/providers.py ------------------------------------
_providers = _admin_providers.register(app, _THIS_MODULE)
get_providers = _providers.get_providers


# -- /admin/oauth, /oauth/callback: admin_routes/oauth.py ---------------------
_oauth = _admin_oauth.register(app, _THIS_MODULE)
oauth_disconnect = _oauth.oauth_disconnect


# -- Egress guard shared by /providers and /admin/direct-providers ------------


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


# -- /admin/direct-providers: admin_routes/direct_providers.py ----------------
_direct_providers = _admin_direct_providers.register(app, _THIS_MODULE)
_test_openai_endpoint = _direct_providers._test_openai_endpoint
_schedule_initial_discovery = _direct_providers._schedule_initial_discovery


_background_tasks: set = set()


# -- /admin/ollama: admin_routes/ollama.py ------------------------------------
_admin_ollama.register(app, _THIS_MODULE)


# -- /admin/keys, /admin/orgs: admin_routes/keys.py ---------------------------
_keys = _admin_keys.register(app, _THIS_MODULE)
_default_rpm_limit = _keys._default_rpm_limit
_default_monthly_limit_usd = _keys._default_monthly_limit_usd


# -- /admin/jobs: admin_routes/jobs.py ----------------------------------------
_admin_jobs.register(app, _THIS_MODULE)


# -- /admin/pricing, schedulers, auto-update, catalog history, coverage -------
#    declared by admin_routes/pricing.py
_pricing = _admin_pricing.register(app, _THIS_MODULE)
_suggestion_action = _pricing._suggestion_action
admin_catalog_history = _pricing.admin_catalog_history


# -- /admin/usage, /admin/retention: admin_routes/usage.py --------------------
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


# The refusal audit window, its state and the values already warned about stay
# here (patched by the tests, one copy per import of app.py):
# auth_core/auto_login.py reads them through core (PLU-97).
_REFUSAL_AUDIT_WINDOW_S = 600.0
_REFUSAL_AUDIT_MAX_KEYS = 256
_refusal_audit_seen: Dict[tuple, float] = {}
_warned_auto_login_values: set = set()


# -- Local auto-login: auth_core/auto_login.py ---------------------------------
_ac_auto_login_ns = _ac_auto_login.register(app, _THIS_MODULE)
_LOOPBACK_HOSTS = _ac_auto_login._LOOPBACK_HOSTS
_FLAG_ON = _ac_auto_login._FLAG_ON
_FLAG_OFF = _ac_auto_login._FLAG_OFF
_PROXY_HEADERS = _ac_auto_login._PROXY_HEADERS
_LOCAL_NAVIGATION_SITES = _ac_auto_login._LOCAL_NAVIGATION_SITES
_SUSPICIOUS_REFUSALS = _ac_auto_login._SUSPICIOUS_REFUSALS
_host_header_name = _ac_auto_login._host_header_name
_is_loopback_address = _ac_auto_login._is_loopback_address
_bind_host = _ac_auto_login._bind_host
_local_login_proof = _ac_auto_login._local_login_proof
_should_audit_refusal = _ac_auto_login_ns._should_audit_refusal
_auto_login_mode = _ac_auto_login_ns._auto_login_mode
_auto_login_refusal = _ac_auto_login_ns._auto_login_refusal
_issue_local_login_token = _ac_auto_login_ns._issue_local_login_token


log.info(
    "auth.auto_login mode=%s local_install=%s",
    _auto_login_mode(),
    _is_loopback_address(_bind_host()),
)


# -- /login, /auth/admin/*, /admin/mfa: admin_routes/auth.py ------------------
_auth = _admin_auth.register(app, _THIS_MODULE)
_LOGIN_HTML_TEMPLATE = _auth._LOGIN_HTML_TEMPLATE


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
