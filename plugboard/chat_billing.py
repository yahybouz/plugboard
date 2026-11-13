"""Counting, pricing and recording a chat call, and replaying a whole answer
as a stream: one place for the chat routes and the router (PLU-28).

A chat answer comes either streamed by its provider (``app._bill_streaming``
reads the counts from the stream) or whole: a non-streamed chat, a CLI
route, the response cache. A whole answer asked as a stream is replayed as
OpenAI SSE chunks by :func:`replay_as_sse`, its last chunk carrying the
usage priced here, so what the client sees is what was recorded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from plugboard import billing as _billing
from plugboard import usage as _usage
from plugboard.tokenizer import estimate_completion_tokens, estimate_prompt_tokens

log = logging.getLogger(__name__)

# Token details a provider (agy) may report next to its counts.
USAGE_DETAIL_KEYS = ("prompt_tokens_details", "completion_tokens_details")


@dataclass(frozen=True)
class Charge:
    """A priced call: its counts, its cost and the grid entry that priced it."""

    model: str
    provider_id: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    pricing_month: str
    price_key: str = ""
    price_source: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def billing_info(self) -> Dict[str, Any]:
        """The ``billing`` object of a chat response."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "model": self.model,
            "pricing_month": self.pricing_month,
            "price_key": self.price_key,
        }

    def usage_block(self, reported_usage: Optional[Dict[str, Any]] = None) -> dict:
        """The ``usage`` a replayed stream ends with (see :func:`usage_block`)."""
        return usage_block(self.billing_info(), reported_usage)


def usage_block(
    billing_info: Dict[str, Any], reported_usage: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """The ``usage`` of a billed answer: its counts and cost, and the token
    details the provider reported (``reported_usage``), so a replayed stream
    ends with what was recorded."""
    block: Dict[str, Any] = {
        "prompt_tokens": billing_info["prompt_tokens"],
        "completion_tokens": billing_info["completion_tokens"],
        "total_tokens": billing_info["prompt_tokens"]
        + billing_info["completion_tokens"],
        "cost_usd": round(billing_info["cost_usd"], 6),
    }
    for key in USAGE_DETAIL_KEYS:
        if isinstance((reported_usage or {}).get(key), dict):
            block[key] = reported_usage[key]
    return block


def counts(
    body: Dict[str, Any], result: Dict[str, Any], *, reported: bool
) -> Tuple[int, int]:
    """(prompt, completion) tokens of a whole answer. Counts the provider
    reported stay, zeros included (``reported``); otherwise a missing or zero
    count is estimated with tiktoken."""
    usage = result.get("usage") or {}
    prompt = usage.get("prompt_tokens") or 0
    completion = usage.get("completion_tokens") or 0
    if not reported:
        if not prompt:
            prompt = estimate_prompt_tokens(body.get("messages", []))
        if not completion:
            completion = estimate_completion_tokens(result)
    return int(prompt), int(completion)


def price(
    provider: Dict[str, Any],
    real_model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> Charge:
    """Price a call with this month's grid (``billing.resolve_price``)."""
    month = _billing.current_pricing_month()
    match = _billing.resolve_price(real_model, month, provider=provider)
    cost = _billing.calculate_cost(
        prompt_tokens, completion_tokens, match.entry if match else None
    )
    return Charge(
        model=real_model,
        provider_id=provider["id"],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        pricing_month=month,
        price_key=match.price_key if match else "",
        price_source=match.source if match else "",
    )


def charge_completion(
    body: Dict[str, Any],
    result: Dict[str, Any],
    provider: Dict[str, Any],
    real_model: str,
    *,
    reported: bool,
) -> Charge:
    """Count and price a whole answer."""
    prompt, completion = counts(body, result, reported=reported)
    return price(provider, real_model, prompt, completion)


class BudgetHold:
    """The hold a budgeted call took on its monthly budget (BUG-52).

    ``done`` once the hold was settled into a usage entry, released, or kept
    because the settlement failed: a hold is used at most once.
    """

    __slots__ = ("id", "done")

    def __init__(self, hold_id: str) -> None:
        self.id = hold_id
        self.done = False

    def release(self) -> None:
        """Give the hold back (a call that was not billed). A failure is
        logged: the hold then lapses after usage.HOLD_TTL_S."""
        if self.done:
            return
        self.done = True
        try:
            _usage.release_hold(self.id)
        except Exception as exc:
            log.error("budget hold %s not released: %s", self.id, exc)


def record(
    client_id: str, key_id: str, charge: Charge, hold: Optional[BudgetHold] = None
) -> None:
    """Write the call to the usage log (raises when it cannot).

    With a ``hold``, the entry replaces it in one transaction. When that
    write fails, the hold is kept: the call's estimate stays counted on the
    budget until the hold lapses, rather than the call counting nothing.
    """
    fields = dict(
        client_id=client_id,
        model=charge.model,
        provider_id=charge.provider_id,
        prompt_tokens=charge.prompt_tokens,
        completion_tokens=charge.completion_tokens,
        pricing_month=charge.pricing_month,
        cost_usd=charge.cost_usd,
        key_id=key_id,
        price_key=charge.price_key,
        price_source=charge.price_source,
    )
    if hold is not None and not hold.done:
        hold.done = True
        _usage.settle_hold(hold.id, **fields)
    else:
        _usage.append_usage(**fields)


async def replay_as_sse(
    completion: Dict[str, Any], *, completion_id: str, created: int, model: str
) -> AsyncIterator[bytes]:
    """Replay a whole chat completion as OpenAI SSE chunks.

    The text goes out word by word, then the tool calls in one delta
    (``index`` = position), then a final chunk with the answer's own
    ``finish_reason`` and its ``usage``, then ``[DONE]``. An empty or missing
    ``content`` sends no content delta.
    """
    choice = (completion.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []
    finish_reason = choice.get("finish_reason") or "stop"
    usage = completion.get("usage") or {}

    def _chunk(delta: Dict[str, Any], finish: Optional[str]) -> bytes:
        evt = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if finish is not None:
            evt["usage"] = usage
        return f"data: {json.dumps(evt)}\n\n".encode("utf-8")

    if content:
        words = content.split(" ")
        for i, word in enumerate(words):
            yield _chunk({"content": word + (" " if i < len(words) - 1 else "")}, None)
    if tool_calls:
        calls = []
        for i, tc in enumerate(tool_calls):
            fn = tc.get("function") or {}
            calls.append(
                {
                    "index": i,
                    "id": tc.get("id"),
                    "type": tc.get("type") or "function",
                    "function": {
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "",
                    },
                }
            )
        yield _chunk({"tool_calls": calls}, None)
    yield _chunk({}, finish_reason)
    yield b"data: [DONE]\n\n"
