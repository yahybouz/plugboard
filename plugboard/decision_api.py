"""Client-key-only typed decisions, with separate admission and no chat fallback."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from plugboard.apikeys import get_key_record
from plugboard.decisions import (
    DECISION_MODELS,
    DecisionRequest,
    DecisionTimeout,
    DecisionUnavailable,
    infer_jev,
    validate_answers,
)
from plugboard.ratelimit import check_client_rate_limit

TIMEOUT = 45.0
MAX_BODY_BYTES = 65_536
# One engine slot; other keys wait their turn in arrival order, briefly.
MAX_QUEUED = 8
QUEUE_TIMEOUT = 15.0
# Official Jev price (docs.typesafe.ai/models, checked 2026-10-24): input tokens
# only, read from the response's usage. Laya runs locally and is free. The price
# applied is the month's grid entry (an admin's manual price wins), else the
# dated official table (plugboard.official_prices), else these constants.
JEV_PRICE_URL = "https://docs.typesafe.ai/models#current-models"
PRICING = {
    "jev": {"input_usd_per_mtok": 0.042, "output_usd_per_mtok": 0.0, "unit": "token"},
    "laya": {"input_usd_per_mtok": 0.0, "output_usd_per_mtok": 0.0, "unit": "token"},
    "laya-multilingual": {
        "input_usd_per_mtok": 0.0,
        "output_usd_per_mtok": 0.0,
        "unit": "token",
    },
}
# Usage missing or implausible (not an int from 0 to 2**20): bill Jev's default
# request context instead.
JEV_CEILING_TOKENS = 64_000
JEV_MAX_TOKENS = 1_048_576
OBSOLETE_PRICE_ENV = "PLUGBOARD_JEV_PRICE_PER_CALL_USD"
_obsolete_warned = False
# Usage breaker, per process: once a decision could not be recorded, keys with
# a monthly budget are refused (503, no engine call) until a write succeeds.
# Every USAGE_RETRY_SECONDS one budgeted call is let through as the probe;
# a decision recorded for any key closes the breaker.
USAGE_RETRY_SECONDS = 60.0
_usage_down_since: float | None = None

log = logging.getLogger(__name__)


class _FairSlot:
    """Single slot handed directly to the oldest waiter on release."""

    def __init__(self):
        self._busy = False
        self._waiters: deque[asyncio.Future] = deque()

    async def acquire(self) -> None:
        if not self._busy and not self._waiters:
            self._busy = True
            return
        if len(self._waiters) >= MAX_QUEUED:
            raise HTTPException(429, "Decision capacity reached")
        granted = asyncio.get_running_loop().create_future()
        self._waiters.append(granted)
        try:
            await asyncio.wait_for(asyncio.shield(granted), QUEUE_TIMEOUT)
        except BaseException as exc:
            if granted.done() and not granted.cancelled():
                # Handed the slot just as the wait ended: pass it on.
                self.release()
            else:
                granted.cancel()
                self._waiters.remove(granted)
            if isinstance(exc, TimeoutError):
                raise HTTPException(429, "Decision capacity reached") from None
            raise

    def release(self) -> None:
        while self._waiters:
            granted = self._waiters.popleft()
            if not granted.done():
                granted.set_result(None)  # ownership moves; the slot stays busy
                return
        self._busy = False


_SLOT = _FairSlot()
_active_keys: set[str] = set()


def _warn_obsolete_price() -> None:
    global _obsolete_warned
    if _obsolete_warned or not os.environ.get(OBSOLETE_PRICE_ENV, "").strip():
        return
    _obsolete_warned = True
    log.warning(
        "%s is obsolete and ignored: Jev is billed at its official per-token price",
        OBSOLETE_PRICE_ENV,
    )


def _token_count(value) -> int | None:
    if type(value) is not int or not 0 <= value <= JEV_MAX_TOKENS:
        return None
    return value


def _usage(model: str, raw: dict) -> tuple[int, int, bool]:
    """(input tokens, output tokens, exact) to bill; Laya reports none."""
    if model != "jev":
        return 0, 0, True
    reported = raw.get("usage")
    if not isinstance(reported, dict):
        reported = {}
    output_tokens = _token_count(reported.get("output_tokens")) or 0
    input_tokens = _token_count(reported.get("input_tokens"))
    if input_tokens is None:
        return JEV_CEILING_TOKENS, output_tokens, False
    return input_tokens, output_tokens, True


def _per_mtok(inp: float, out: float) -> dict:
    return {"input_usd_per_mtok": inp, "output_usd_per_mtok": out, "unit": "token"}


def _price(model: str) -> tuple[dict, str]:
    """(price applied this month, origin): ``official`` or ``grid:<source>:<month>``.

    ``billing.resolve_price`` reads the month's grid, then the official table;
    the constants above apply only if both fail or the grid entry is invalid.
    """
    from plugboard import billing, pricing_merge

    month = billing.current_pricing_month()
    match = billing.resolve_price(model, month, decision=True)
    if match is not None and (match.space != "builtin" or match.source == "official"):
        valid = pricing_merge.validate_price(match.entry)
        if valid is not None:
            if match.source == "official":
                origin = "official"
            else:
                label = match.source or "unlabelled"
                origin = f"grid:{label}:{match.month or month}"
            return _per_mtok(valid["input_price"], valid["output_price"]), origin
        log.warning("Invalid price for %s in the %s grid: ignored", model, month)
    return dict(PRICING[model]), "official"


def _usage_breaker_refuses(record: dict) -> bool:
    """True while usage cannot be recorded and the key has a monthly budget."""
    global _usage_down_since
    if _usage_down_since is None:
        return False
    from plugboard.app import _spending_scope

    limit, _ = _spending_scope(record["client_id"], key_record=record)
    if limit <= 0:
        return False  # no budget to protect: served, as before
    now = time.monotonic()
    if now - _usage_down_since < USAGE_RETRY_SECONDS:
        return True
    _usage_down_since = now  # this call is the probe; the next ones wait again
    return False


def _record_decision(record: dict, model: str, raw: dict) -> dict:
    global _usage_down_since
    from plugboard import billing, usage

    input_tokens, output_tokens, exact = _usage(model, raw)
    price, origin = _price(model)
    cost = round(
        (
            input_tokens * price["input_usd_per_mtok"]
            + output_tokens * price["output_usd_per_mtok"]
        )
        / 1_000_000,
        12,
    )
    if origin != "official":
        kind, _, detail = origin.partition(":")
        source = origin if exact else f"{kind}-estimate:{detail}"
    elif model != "jev":
        source = "builtin:laya-free"
    elif exact:
        source = f"official:{JEV_PRICE_URL}"
    else:
        source = f"official-estimate:{JEV_PRICE_URL}"
    try:
        usage.append_usage(
            client_id=record["client_id"],
            model=f"decision:{model}",
            provider_id="decisions",
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            pricing_month=billing.current_pricing_month(),
            cost_usd=cost,
            key_id=record["key_id"],
            price_source=source,
        )
    except Exception as exc:  # noqa: BLE001 - the engine already answered
        log.error(
            "Decision answered but not recorded (key %s, model %s): %s",
            record["key_id"],
            model,
            type(exc).__name__,
        )
        _usage_down_since = time.monotonic()
        return {"status": "unrecorded", "cost_usd": None}
    _usage_down_since = None
    return {"status": "billed" if exact else "estimated", "cost_usd": cost}


def require_decision_key(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    record = get_key_record(token) if scheme.lower() == "bearer" and token else None
    if not record:
        raise HTTPException(401, "Client API key required for decisions")
    return record


def _allowed(record: dict) -> list[str]:
    policy = record.get("allowed_decision_models")
    if not isinstance(policy, list):
        return []
    return [mid for mid in DECISION_MODELS if mid in policy]


class _DecisionRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request: Request):
            # Authenticate before parsing a potentially invalid, sensitive body.
            require_decision_key(request)
            chunks = bytearray()
            async for chunk in request.stream():
                if len(chunks) + len(chunk) > MAX_BODY_BYTES:
                    raise HTTPException(413, "Decision request too large")
                chunks.extend(chunk)
            request._body = bytes(chunks)
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI's default errors include the rejected state/instructions.
                return JSONResponse(
                    status_code=422, content={"detail": "Invalid decision request"}
                )

        return safe_handler


router = APIRouter(
    prefix="/v1/decisions", route_class=_DecisionRoute, tags=["decisions"]
)


def _metadata(model: str) -> dict:
    if model not in DECISION_MODELS:
        raise ValueError("unknown_model")
    from plugboard import decision_runtime
    from plugboard.decision_assets import laya_config

    if model == "jev":
        ready = bool(os.environ.get("PLUGBOARD_JEV_API_KEY", "").strip())
        reason = "" if ready else "credentials_missing"
        revision = "jev-latest (hosted, vendor-managed)"
    else:
        ready, reason = decision_runtime.available(model)
        revision = {
            "laya": "7b928d828b7b0e022f929d9bd2e44165aa270148",
            "laya-multilingual": "1720e3e3357cfe1e281542e223f8273b0890ca34",
        }[model]
    laya = model.startswith("laya")
    cfg = laya_config(model) if laya else None
    return {
        "id": model,
        "provider": "typesafe" if model == "jev" else "convaiinnovations",
        "revision": revision,
        "available": ready,
        "unavailable_reason": reason,
        "languages": ["en", "fr", "100+ languages"]
        if model == "laya-multilingual"
        else ["en"]
        if laya
        else ["vendor-managed; validate on your data"],
        "primitives": ["choice", "noul", "score"],
        "limits": {
            "state_bytes": 4000,
            "questions": 8,
            "options": 16,
            "score_levels": 10 if model == "jev" else 16,
            "context_tokens": cfg["max_len"] if cfg else None,
            "state_tokens": None,
            "question_branch_tokens": cfg["head_max_len"] if cfg else None,
            "head_tokens": cfg["head_max_len"] if cfg else None,
            "instruction_tokens": None,
            "option_tokens": 48 if laya else None,
            "request_bytes": MAX_BODY_BYTES,
            "response_bytes": 65_536,
            "timeout_seconds": decision_runtime.TIMEOUT_SECONDS
            if model != "jev"
            else 30,
            "api_timeout_seconds": TIMEOUT,
            "concurrency": 1,
        },
        "pricing": _price(model)[0],
        "confidence_semantics": "1 - entropy(p)/log(K); not probability of correctness"
        if laya
        else "Choice: chance-corrected top probability; score: normalized distance to mode; noul: probability true",
        "limitations": [
            "Official price per input token from the response usage; output free; "
            "missing usage is billed at the 64,000-token default context"
            if model == "jev"
            else "Runs locally, free: no gateway charge",
            "One decision per key at a time; other keys wait in a short queue",
            "Validate calibration on your workload",
            "Local token limits are conservative; excess input is rejected before inference",
            "Local availability checks sizes; worker rechecks every SHA256 before loading",
        ],
    }


@router.get("/models")
async def list_decision_models(record: dict = Depends(require_decision_key)):
    return {"object": "list", "data": [_metadata(mid) for mid in _allowed(record)]}


async def dispatch(model: str, payload: dict) -> dict:
    if model == "jev":
        return await infer_jev(payload)
    from plugboard.decision_runtime import RuntimeFailure, infer

    try:
        return await infer(model, payload)
    except RuntimeFailure as exc:
        code = str(exc)
        if code in {
            "runtime_not_configured",
            "assets_missing_or_not_private",
            "assets_incomplete",
            "assets_integrity_failed",
            "unknown_model",
        }:
            raise DecisionUnavailable() from None
        if code == "worker_timeout":
            raise DecisionTimeout() from None
        if code == "worker_busy":
            raise HTTPException(429, "Decision capacity reached") from None
        if code in {"worker_context_limit", "worker_input_limit"}:
            raise HTTPException(422, "Decision engine input limit exceeded") from None
        raise


@router.post("")
async def decide(body: DecisionRequest, record: dict = Depends(require_decision_key)):
    if body.model not in _allowed(record):
        raise HTTPException(403, "Decision model not permitted")
    # Jev's official score primitive supports at most ten ordinal levels.
    if body.model == "jev" and any(
        q.type == "score" and len(q.criteria) > 10 for q in body.questions.values()
    ):
        raise HTTPException(422, "Jev supports at most ten score levels")
    from plugboard.app import _check_spending_limit

    _warn_obsolete_price()
    if _usage_breaker_refuses(record):
        raise HTTPException(503, "Decision usage recording unavailable")
    _check_spending_limit(record["client_id"], key_record=record)
    rpm = record.get("rpm_limit", 0)
    if rpm and not check_client_rate_limit(record["key_id"], rpm):
        raise HTTPException(429, "Client rate limit exceeded")
    key_id = record["key_id"]
    if key_id in _active_keys:
        raise HTTPException(429, "Decision capacity reached")
    _active_keys.add(key_id)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TIMEOUT
        await _SLOT.acquire()
        error: HTTPException | None = None
        # What a failed Jev call bills, set by infer_jev once TypeSafe may have
        # taken it. None: not sent, or cut by the API deadline (at most 15 s
        # queued out of 45 s, so Jev's own 30 s timeout normally fires first).
        charge: dict | None = None
        try:
            # Time spent queued counts: queue plus engine stay within TIMEOUT.
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            raw = await asyncio.wait_for(
                dispatch(body.model, body.model_dump()), timeout=remaining
            )
            answers = validate_answers(body, raw)
        except DecisionUnavailable:
            error = HTTPException(503, "Decision engine unavailable")
        except (TimeoutError, DecisionTimeout) as exc:
            error = HTTPException(504, "Decision engine timed out")
            charge = getattr(exc, "charge", None)
        except HTTPException:
            raise
        except Exception as exc:
            # Upstream exceptions may contain credentials or full submitted questions.
            error = HTTPException(502, "Invalid decision engine response")
            charge = getattr(exc, "charge", None)
        finally:
            _SLOT.release()
        if error is not None:
            if charge is not None and body.model == "jev":
                _record_decision(record, body.model, charge)
            raise error
        # Recorded before the key may call again.
        billing = _record_decision(record, body.model, raw)
    finally:
        _active_keys.discard(key_id)
    return {"model": body.model, "answers": answers, "billing": billing}
