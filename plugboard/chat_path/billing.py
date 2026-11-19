"""Billing of a chat answer and the spending alert of the chat path.

The usage of a streamed answer (from its SSE chunks) and of a whole answer,
priced and recorded through ``chat_billing``, the provider and model that
served the call, the hold kept when a served call was not billed, and the
fire-and-forget spending alert. Moved verbatim from ``plugboard/app.py``
(PLU-95), behaviour unchanged. ``core`` is the ``plugboard.app`` module that
registers the billing: the patch points (``_served_by``,
``resolve_provider_and_model``, ``_usage``, ``_check_and_alert``,
``_send_spending_alert``) are read as ``core.<name>`` when they run.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request

from plugboard import chat_billing
from plugboard.apikeys import load_keys
from plugboard.router import usage_reported as _usage_reported
from plugboard.tokenizer import count_tokens, estimate_prompt_tokens

log = logging.getLogger("plugboard.app")


def _keep_hold(hold: Optional[chat_billing.BudgetHold]) -> None:
    """A served call whose billing failed keeps its hold: counted at its
    estimate until it lapses and is recorded, never given back."""
    if hold is not None and not hold.done:
        hold.done = True
        hold.settling = False
        log.error("budget hold %s kept: the served call was not billed", hold.id)


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
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
        charge = chat_billing.price(
            provider, real_model, prompt_tokens, completion_tokens
        )
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

    def _bill_web_exhausted(
        client_id: str,
        key_id: str,
        body: Dict[str, Any],
        exc: BaseException,
        hold: Optional[chat_billing.BudgetHold] = None,
    ) -> Dict[str, Any]:
        """Record a web run that spent its turns without answering (502
        web_budget_exhausted, BUG-159): the tokens claude reported, at the
        price of the route that ran it, as a served answer is; without a
        valid report, the prompt's estimate. Returns the billing dict."""
        reported = getattr(exc, "reported_usage", None)
        result = {
            "choices": [{"message": {"role": "assistant", "content": ""}}],
            "usage": dict(reported or {}),
        }
        provider, real_model = getattr(exc, "billing_target", None) or (
            _billing_target(body)
        )
        charge = chat_billing.charge_completion(
            body, result, provider, real_model, reported=reported is not None
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
        *,
        web_exhausted: Optional[BaseException] = None,
    ) -> Dict[str, Any]:
        """Bill a whole answer (a non-streamed chat, or one a CLI route answered
        for a stream) and check the spending alert; {} when nothing was billed.
        A billing error does not fail the answer: it is logged. The usage write
        runs off the event loop (PLU-27). With ``web_exhausted`` (the error of
        a web run stopped by its turns, BUG-159), that run is billed instead."""
        if not client_id:
            return {}
        hold = getattr(request.state, "budget_hold", None)
        if hold is not None:
            hold.settling = True  # never given back while it is billed
        try:
            if web_exhausted is not None:
                billing_info = await _asyncio.to_thread(
                    _bill_web_exhausted,
                    client_id,
                    getattr(request.state, "key_id", ""),
                    body,
                    web_exhausted,
                    hold=hold,
                )
            else:
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
                core._send_spending_alert(
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
        served = core._served_by()
        if served is not None:
            return served
        return core.resolve_provider_and_model(body.get("model", ""))

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
                if (
                    k.get("client_id") == client_id
                    and k.get("monthly_limit_usd", 0) > 0
                ):
                    key_id = k.get("key_id", "")
                    limit = float(k["monthly_limit_usd"])
                    break

        if limit <= 0:
            return
        if current_total_usd is not None:
            total = current_total_usd
        elif key_id:
            total = core._usage.get_total_cost_by_key(pricing_month, key_id)
        else:
            total = core._usage.get_total_cost_by_client(pricing_month, client_id)
        try:
            # Kept until sent and drained at shutdown (PLU-27).
            from plugboard.alerts import in_background

            in_background(core._check_and_alert(client_id, total, limit))
        except Exception as exc:
            log.debug(
                "Failed to schedule spending alert check for %s: %s", client_id, exc
            )

    return SimpleNamespace(
        _bill_streaming=_bill_streaming,
        _bill_completion=_bill_completion,
        _bill_web_exhausted=_bill_web_exhausted,
        _charge_answer=_charge_answer,
        _billing_target=_billing_target,
        _send_spending_alert=_send_spending_alert,
    )
