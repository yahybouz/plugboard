"""The model list, preflight and model info routes.

GET /v1/models, POST /v1/preflight, GET /v1/models/benchmarks and
GET /v1/models/{model_id}/info, with the catalogue as the calling key sees it
and the recent-failure filter of a client key's list. Moved verbatim from
``plugboard/app.py`` (PLU-95), behaviour unchanged. ``core`` is the
``plugboard.app`` module that registers the routes: the patch points
(``list_models``, ``load_providers``, ``forward``,
``resolve_provider_and_model``, ``_get_request_key_record``,
``_model_route_guard``, ``_check_price_known``) and the names that stay in
``app.py`` are read as ``core.<name>`` when they run.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
import time
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from plugboard import chat_billing
from plugboard.chat_path.billing import _keep_hold
from plugboard.chat_path.limits import _check_client_rpm
from plugboard.chat_path.streaming import (
    _ClientGone,
    _client_gone_response,
    _unless_client_gone,
)
from plugboard.cli_subprocess import ModelNotServedError
from plugboard.decisions import is_decision_model
from plugboard.router import (
    ProviderDisabledError,
    ProvidersBusyError,
    chat_routes,
    disabled_route,
    is_unknown_model,
    is_unserved_cli_model,
)

log = logging.getLogger("plugboard.app")


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


class _PreflightItem(BaseModel):
    provider: str
    model: str


_PREFLIGHT_MAX_ITEMS = 10


class _PreflightRequest(BaseModel):
    items: List[_PreflightItem] = Field(max_length=_PREFLIGHT_MAX_ITEMS)


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    async def _models_for_key(
        request: Request, client_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        """The catalogue as the calling key sees it, for every model list route.

        Chat's allowed_models rule decides: a limited key only gets the models it
        may call; the admin and unlimited keys get every model. A client key also
        loses a model whose last probe failed within 36 hours. Entries are copies,
        so the shared models cache never carries one caller's flag.
        """
        key_record = core._get_request_key_record(request, client_id=client_id)
        # One guard for the whole list: its registry snapshot is read once.
        guard = core._model_route_guard(client_id or "", key_record)
        # A client key is offered only what can be called now: a model whose last
        # probe failed recently is left out. The admin keeps the full view.
        callable_only = bool(client_id) and client_id != "admin"
        return [
            dict(item, account_compatible=True)
            for item in await core.list_models()
            if core._guard_accepts(guard, item.get("id", ""))
            and not (callable_only and _not_callable_now(item))
        ]

    @app.get("/v1/models")
    async def models(
        request: Request, client_id: Optional[str] = Depends(core._resolve_client)
    ):
        from plugboard.legacy_connectors import is_removed_catalogue_model
        from plugboard.model_identity import catalogue_native_model

        items = await _models_for_key(request, client_id)
        provider_ids = tuple(p["id"] for p in core.load_providers())
        items = [
            item
            for item in items
            if not is_decision_model(
                catalogue_native_model(item, provider_ids=provider_ids),
                effective_name=True,
            )
            and not is_removed_catalogue_model(item)
        ]
        return {"object": "list", "data": items}

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
                lambda: core._check_spending_limit(client_id, key_record),
            ),
            (
                "forbidden_model",
                keyed,
                lambda: core._check_model_access(
                    client_id, model_str, key_record=key_record
                ),
            ),
            ("provider_disabled", True, _enabled),
            ("model_not_found", True, _known),
            (
                "price_unavailable",
                keyed,
                lambda: core._check_price_known(client_id, model_str, key_record),
            ),
            ("rate_limited", keyed, lambda: _check_client_rpm(client_id, key_record)),
        )
        for code, applies, check in checks:
            if not applies:
                continue
            try:
                check()
            except core._UsageUnavailable:
                raise  # the whole batch: 503 usage_unavailable (D-37)
            except HTTPException:
                return code
        return None

    @app.post(
        "/v1/preflight",
        # The key first (PLU-15): see _inflight_chat_dep.
        dependencies=[Depends(core._resolve_client), Depends(core._inflight_chat_dep)],
    )
    async def preflight(
        request: Request,
        body: _PreflightRequest,
        client_id: Optional[str] = Depends(core._resolve_client),
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
        key_record = core._get_request_key_record(request, client_id=client_id)
        key_id = getattr(request.state, "key_id", "")
        route_guard = core._model_route_guard(client_id, key_record)
        route_options = {}
        if route_guard is not None:
            route_options["route_guard"] = route_guard
        results: List[Dict[str, Any]] = []
        for item in body.items:
            t0 = time.perf_counter()
            model_str = f"{item.provider}:{item.model}"
            error: Optional[str] = None
            try:
                core.resolve_provider_and_model(model_str)
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
                    await core._take_budget_hold(
                        client_id,
                        key_record,
                        ping_body,
                        lambda h: holds.__setitem__(0, h),
                    )
                except core._UsageUnavailable:
                    raise
                except HTTPException:
                    error = "budget_exceeded"
            if error is None:
                hold = holds[0]
                try:
                    # BUG-77: a ping that waited for a full provider gave the
                    # global slot back; the router takes it back before a call.
                    result = await _unless_client_gone(
                        request, core.forward(ping_body, timeout=10.0, **route_options)
                    )
                except _ClientGone:
                    if hold is not None:
                        await _asyncio.to_thread(hold.release)
                    # Nobody waits for the rest of the batch: stop here.
                    return _client_gone_response(model_str)
                except core._ChatOverloaded:
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
                            core._bill_completion(
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
        request: Request, client_id: Optional[str] = Depends(core._resolve_client)
    ):
        """List all models enriched with benchmarks, capabilities, and metadata."""
        from plugboard.benchmarks import enrich_models_list

        items = await _models_for_key(request, client_id)
        return {"object": "list", "data": enrich_models_list(items)}

    @app.get("/v1/models/{model_id:path}/info")
    async def model_info(
        model_id: str,
        request: Request,
        client_id: Optional[str] = Depends(core._resolve_client),
    ):
        """Get detailed info for a specific model: benchmarks, capabilities, best_for."""
        from plugboard.benchmarks import derived_model_info, get_model_info

        if client_id and client_id != "admin":
            # A client key reads the routes chat would use for this spelling
            # (PLU-60), whatever the spelling: the admin keeps the full view.
            key_record = core._get_request_key_record(request, client_id=client_id)
            guard = core._model_route_guard(client_id, key_record)
            disabled = disabled_route(model_id)
            try:
                # A model the key may not call is refused as chat refuses it:
                # 403 forbidden_model, same detail, no profile. Chat checks the
                # first route, then every route it would try, in order.
                if guard is not None:
                    guard(*(disabled or core.resolve_provider_and_model(model_id)))
                routes = [disabled] if disabled else chat_routes(model_id, guard)
            except (ValueError, ModelNotServedError):
                # No route chat would use (no active provider, a decision model,
                # a name the CLI routes refuse): the unknown-model answer, never
                # the curated profile.
                return derived_model_info(model_id)
            # So does a model the client's catalogue leaves out (_models_for_key):
            # every route chat would use was probed and failed recently.
            failing = set()
            for item in await core.list_models():
                if _not_callable_now(item):
                    try:
                        provider, native = core.resolve_provider_and_model(
                            item.get("id", "")
                        )
                    except ValueError:
                        continue
                    failing.add((provider["id"], native))
            if routes and all((p["id"], m) in failing for p, m in routes):
                return derived_model_info(model_id)
        info = get_model_info(model_id)
        if not info:
            raise HTTPException(404, f"No benchmark data for model '{model_id}'")
        return info

    return SimpleNamespace(
        models=models,
        _models_for_key=_models_for_key,
    )
