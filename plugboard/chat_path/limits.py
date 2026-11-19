"""Spending limit, budget holds, known price, rpm and model access of the chat path.

The monthly spending limit, the hold of a budgeted call's estimated cost
(BUG-52), the refusal of an unpriced model for a budgeted key, the client's
rpm limit and the key's allowed_models checked as chat routes the model.
Moved verbatim from ``plugboard/app.py`` (PLU-95), behaviour unchanged.
``core`` is the ``plugboard.app`` module that registers the checks: the patch
points (``_model_route_guard``, ``resolve_provider_and_model``, ``_usage``,
``_BUDGET_RESERVE_TOKENS``) and the exceptions that stayed there
(``_UsageUnavailable``, ``_ModelNotAllowed``) are read as ``core.<name>`` when
they run.
"""

from __future__ import annotations

import asyncio as _asyncio
import json
import logging
import math
import secrets
from types import ModuleType, SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request

from plugboard import billing as _billing
from plugboard import chat_billing
from plugboard import cli_tools as _cli_tools
from plugboard.apikeys import load_keys
from plugboard.cli_subprocess import ModelNotServedError
from plugboard.ratelimit import check_client_rate_limit
from plugboard.router import chat_routes, disabled_route
from plugboard.tokenizer import count_tokens, estimate_prompt_tokens

log = logging.getLogger("plugboard.app")


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


_UNPRICED_DETAIL = "Model price unavailable for this key"


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


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
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
                spent = core._usage.get_total_cost_by_key(month, key_id)
            else:
                spent = core._usage.get_total_cost_by_client(month, client_id)
            held = core._usage.get_held_cost(month, key_id=key_id, client_id=client_id)
        except Exception as exc:
            log.error("usage cannot be read: budgeted key refused (%s)", exc)
            raise core._UsageUnavailable() from None
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

    def _completion_cap(body: Dict[str, Any]) -> int:
        """Completion tokens one answer may produce: the larger of
        max_completion_tokens and max_tokens (a provider may honour either),
        else _BUDGET_RESERVE_TOKENS."""
        caps = [
            cap
            for cap in (
                _whole_count(body.get(n))
                for n in ("max_completion_tokens", "max_tokens")
            )
            if cap
        ]
        return max(caps) if caps else core._BUDGET_RESERVE_TOKENS

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
                core._usage.hold_budget,
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
            raise core._UsageUnavailable() from None
        if isinstance(got, core._usage.HoldRefused):
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
        cli_tools: bool = False,
    ) -> None:
        """The chat's hold (_take_budget_hold), kept on the request: billing
        settles it, _session_turn_dep gives back one that was not billed.
        With ``cli_tools`` (tools emulated on a CLI route, ADR-039) the cost is
        estimated on the text the CLI gets, the instruction included."""
        request.state.budget_hold = None
        if cli_tools:
            body = _cli_tools.to_text_body(body)

        def keep(hold: Optional[chat_billing.BudgetHold]) -> None:
            request.state.budget_hold = hold

        await _take_budget_hold(client_id, key_record, body, keep)

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
        guard = core._model_route_guard(client_id, key_record)
        if guard is None:
            return
        # A model of a disabled provider is checked as its real owner's (PLU-12):
        # resolve_provider_and_model would name another provider (BUG-44).
        disabled = disabled_route(model_str)
        guard(*(disabled or core.resolve_provider_and_model(model_str)))
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
            record = next(
                (k for k in load_keys() if k.get("client_id") == client_id), None
            )
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
                raise core._ModelNotAllowed(
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
        return _guard_accepts(core._model_route_guard(client_id, key_record), model_str)

    def _guard_accepts(guard, model_str: str) -> bool:
        """Whether a key's guard (None: unrestricted) lets this model through."""
        if guard is None:
            return True
        try:
            guard(*core.resolve_provider_and_model(model_str))
        except (HTTPException, ValueError):
            return False
        return True

    return SimpleNamespace(
        _check_spending_limit=_check_spending_limit,
        _estimate_call_cost=_estimate_call_cost,
        _take_budget_hold=_take_budget_hold,
        _hold_budget=_hold_budget,
        _check_price_known=_check_price_known,
        _check_model_access=_check_model_access,
        _model_route_guard=_model_route_guard,
        _model_is_compatible=_model_is_compatible,
        _guard_accepts=_guard_accepts,
    )
