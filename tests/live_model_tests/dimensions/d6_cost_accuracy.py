"""D6 Cost accuracy — verify the proxy's billed ``X-Cost-USD`` is consistent
with ``calculate_cost(prompt_tokens, completion_tokens, price)`` using the SAME
price resolution the proxy used.

Spec produced by Council C6-bis (2026-05-31), duo data:
  - PROPOSER : claude-data-engineer
  - CRITIC   : codex-data-engineer-critic (2 BLOCKER + 4 MAJOR integrated)

What D6 proves (and its limit): the proxy's cost equals price × reported tokens.
It validates the COHERENCE cost↔tokens↔pricing, NOT the absolute truthfulness
of the reported token counts — a proxy that lies consistently about both tokens
and cost is invisible to D6 (documented limitation).

Key design decisions (Council C6-bis):
  1. Price is resolved on the ROUTED/BILLED model and the proxy's OWN
     ``pricing_month`` (Council C6-bis BLOCKER-1) — both read back from the
     response ``billing`` dict — never recomputed from wall-clock, which would
     produce false MAJORs across a month boundary or a fallback route.
  2. ``BillingAwareSurface`` declares ``supports_billing`` so D6 can actually
     SKIP non-billing surfaces (MCP/UI) instead of flooding the denominator with
     MAJORs (Council C6-bis BLOCKER-2). API only in C5/C6 MVP.
  3. Tolerance has an absolute floor (Council C6-bis MAJOR-1): below
     ``COST_FLOOR`` ($1e-6) a relative 5% is meaningless against 8-decimal
     rounding, so an absolute tolerance applies; accounting-zero costs compare
     exactly. Charging where the price is zero (or zero where it should charge)
     is a BLOCKER finding.
  4. D6 works on the ``billing`` dict only, never on ``content`` — a thinking
     model that returns empty content but real tokens is still evaluated
     (Council C6-bis MAJOR-4).
  5. ``pricing_not_found`` / ``cache_hit`` / ``surface_billing_unavailable`` are
     INFO-severity skips, excluded from the threshold and from MAJOR alerts
     (Council C6-bis MAJOR-3).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from plugboard.billing import calculate_cost
from tests.live_model_tests.protocols import SURFACE_TIMEOUT_MULTIPLIER, SurfaceClient

# Cost regime thresholds (Council C6-bis MAJOR-1).
_ZERO_EPSILON_USD = 1e-8   # min representable at 8 decimal places
_COST_FLOOR_USD = 1e-6     # $0.000001 — economic significance threshold
_ABS_TOL_USD = 5e-8        # absolute tolerance in the micro-cost regime

# Resolver: (billed_model, pricing_month) -> price_entry | None
PricingResolver = Callable[[str, Optional[str]], Optional[Dict[str, Any]]]

_RETRY_ERROR_TYPES = {
    "timeout",
    "http_error:unknown",
    "malformed_response",
    "empty_response",
}


def _default_pricing_resolver(billed_model: str, pricing_month: Optional[str]) -> Optional[Dict[str, Any]]:
    """Production resolver — same mechanism the proxy uses (single source of
    truth). Imported lazily so unit tests (which inject their own resolver) do
    not need the pricing snapshots on disk."""
    from plugboard.billing import resolve_model_price_with_fallback

    return resolve_model_price_with_fallback(billed_model, pricing_month)


@dataclass
class CostResult:
    """Structured verdict of D6 for one (model, surface) pair.

    error_type vocabulary:
      Verdict:
        "cost_match"                                  → PASS
        "cost_mismatch:over" / "cost_mismatch:under"  → MAJOR
        "cost_mismatch:expected_zero_got_positive"    → BLOCKER (charged on free model)
        "cost_mismatch:expected_positive_got_zero"    → BLOCKER (didn't charge)
      Billing completeness (NOT excluded):
        "cost_header_missing"  → MAJOR — no/unparseable X-Cost-USD on a 200
        "usage_missing"        → MAJOR — cost present but no token counts
      Transport (retried):
        "timeout" / "http_error:<status>" / "malformed_response" / "empty_response"
      Meta (excluded, INFO):
        "skipped:quarantined" / "skipped:surface_unsupported"
        "skipped:surface_billing_unavailable"
        "skipped:pricing_not_found" / "skipped:cache_hit"
    """

    model_id: str
    surface: str
    passed: bool
    error_type: Optional[str] = None
    finding_severity: str = "MAJOR"  # "BLOCKER" | "MAJOR" | "INFO"
    excluded_from_threshold: bool = False

    # Cost comparison
    expected_cost_usd: Optional[float] = None
    reported_cost_usd: Optional[float] = None
    abs_diff_usd: Optional[float] = None
    rel_diff: Optional[float] = None

    # Billing context
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    provider: Optional[str] = None
    billed_model: Optional[str] = None
    pricing_month: Optional[str] = None

    # Timing
    total_latency_ms: float = 0.0
    first_token_latency_ms: Optional[float] = None

    attempts: int = 1
    first_attempt_latency_ms: Optional[float] = None

    extra: Dict[str, Any] = field(default_factory=dict)


def _compare_costs(expected: float, reported: float, tolerance_pct: float) -> tuple[bool, str, str]:
    """Return (passed, error_type, finding_severity). Council C6-bis MAJOR-1."""
    if expected <= _ZERO_EPSILON_USD and reported <= _ZERO_EPSILON_USD:
        return True, "cost_match", "MAJOR"
    if expected <= _ZERO_EPSILON_USD and reported > _ZERO_EPSILON_USD:
        return False, "cost_mismatch:expected_zero_got_positive", "BLOCKER"
    if reported <= _ZERO_EPSILON_USD and expected > _ZERO_EPSILON_USD:
        return False, "cost_mismatch:expected_positive_got_zero", "BLOCKER"
    if expected < _COST_FLOOR_USD:
        if abs(reported - expected) <= _ABS_TOL_USD:
            return True, "cost_match", "MAJOR"
        direction = "over" if reported > expected else "under"
        return False, f"cost_mismatch:{direction}", "MAJOR"
    rel = (reported - expected) / expected
    if abs(rel) <= tolerance_pct:
        return True, "cost_match", "MAJOR"
    direction = "over" if rel > 0 else "under"
    return False, f"cost_mismatch:{direction}", "MAJOR"


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _verdict_from_billing(
    *,
    model_id: str,
    surface: str,
    billing: Optional[Dict[str, Any]],
    price_entry: Optional[Dict[str, Any]],
    tolerance_pct: float,
    total_latency_ms: float,
    attempts: int,
    first_attempt_latency_ms: Optional[float],
) -> CostResult:
    """Pure function: one billing dict + resolved price → one CostResult."""
    base: Dict[str, Any] = dict(
        model_id=model_id,
        surface=surface,
        total_latency_ms=total_latency_ms,
        attempts=attempts,
        first_attempt_latency_ms=first_attempt_latency_ms,
    )

    # ── No billing at all on a 200 → header missing ──────────────────────────
    if not billing:
        return CostResult(passed=False, error_type="cost_header_missing", **base)

    base["provider"] = billing.get("provider")
    base["billed_model"] = billing.get("billed_model")
    base["pricing_month"] = billing.get("pricing_month")

    # ── Cache hit → no fresh cost to validate (excluded) ─────────────────────
    if billing.get("cache_hit"):
        return CostResult(
            passed=False, error_type="skipped:cache_hit",
            finding_severity="INFO", excluded_from_threshold=True, **base,
        )

    reported = _coerce_float(billing.get("cost_usd"))
    if reported is None:
        return CostResult(passed=False, error_type="cost_header_missing", **base)
    base["reported_cost_usd"] = reported

    prompt_tokens = _coerce_int(billing.get("prompt_tokens"))
    completion_tokens = _coerce_int(billing.get("completion_tokens"))
    base["prompt_tokens"] = prompt_tokens
    base["completion_tokens"] = completion_tokens
    if prompt_tokens is None or completion_tokens is None:
        # Cost present but no token basis — a real billing-integrity gap.
        return CostResult(passed=False, error_type="usage_missing", **base)

    # ── Price unresolved → can't evaluate (excluded, INFO) ───────────────────
    if not price_entry:
        return CostResult(
            passed=False, error_type="skipped:pricing_not_found",
            finding_severity="INFO", excluded_from_threshold=True, **base,
        )

    expected = calculate_cost(prompt_tokens, completion_tokens, price_entry)
    base["expected_cost_usd"] = expected
    base["abs_diff_usd"] = abs(reported - expected)
    base["rel_diff"] = ((reported - expected) / expected) if expected > _ZERO_EPSILON_USD else None

    passed, error_type, severity = _compare_costs(expected, reported, tolerance_pct)
    if passed:
        return CostResult(passed=True, error_type=None, **base)
    return CostResult(passed=False, error_type=error_type, finding_severity=severity, **base)


def _build_prompt(nonce: str) -> list:
    """Deterministic, tiny prompt with a nonce to defeat the semantic cache."""
    return [{"role": "user", "content": f"[{nonce}] Reply with only the word: pong"}]


def run_d6_cost_accuracy(
    *,
    model_id: str,
    surface_client: SurfaceClient,
    catalog_entry: Dict[str, Any],
    quarantine_ids: Optional[set] = None,
    pricing_resolver: PricingResolver = _default_pricing_resolver,
    tolerance_pct: float = 0.05,
    clock: Any = time.monotonic,
) -> CostResult:
    """Run D6 Cost accuracy on one (model, surface) pair.

    ``pricing_resolver`` is injectable so unit tests need no pricing snapshots.
    Price is resolved AFTER the call, on the billed model + the proxy's own
    pricing month (Council C6-bis BLOCKER-1).
    """
    quarantine_ids = quarantine_ids or set()
    surface_name = getattr(surface_client, "name", "?")

    if model_id in quarantine_ids:
        return CostResult(model_id=model_id, surface=surface_name, passed=False,
                          error_type="skipped:quarantined",
                          finding_severity="INFO", excluded_from_threshold=True)

    valid_surfaces = catalog_entry.get("surfaces") or []
    if valid_surfaces and surface_name not in valid_surfaces:
        return CostResult(model_id=model_id, surface=surface_name, passed=False,
                          error_type="skipped:surface_unsupported",
                          finding_severity="INFO", excluded_from_threshold=True)

    # Discriminant — only billing-capable surfaces are evaluated (BLOCKER-2).
    if not getattr(surface_client, "supports_billing", False):
        return CostResult(model_id=model_id, surface=surface_name, passed=False,
                          error_type="skipped:surface_billing_unavailable",
                          finding_severity="INFO", excluded_from_threshold=True)

    base_timeout = float(catalog_entry.get("timeout_s") or 60)
    timeout = base_timeout * SURFACE_TIMEOUT_MULTIPLIER.get(surface_name, 1.0)
    is_thinking = bool(catalog_entry.get("is_thinking", False))
    max_tokens = 512 if is_thinking else 32

    first_attempt_latency_ms: Optional[float] = None
    last_exception: Optional[Exception] = None
    last_result: Optional[CostResult] = None

    for attempt in (1, 2):
        nonce = secrets.token_hex(8).upper()
        prompt = _build_prompt(nonce)
        t0 = clock()
        try:
            raw = surface_client.chat(
                model=model_id, messages=prompt, stream=False,
                timeout=timeout, max_tokens=max_tokens, temperature=0.0,
            )
        except TimeoutError:
            latency = (clock() - t0) * 1000.0
            if attempt == 1:
                first_attempt_latency_ms = latency
                last_exception = TimeoutError("timeout")
                continue
            return CostResult(model_id=model_id, surface=surface_name, passed=False,
                              error_type="timeout", total_latency_ms=latency, attempts=attempt,
                              first_attempt_latency_ms=first_attempt_latency_ms)
        except Exception as exc:  # noqa: BLE001
            latency = (clock() - t0) * 1000.0
            err_type = getattr(exc, "error_type", None) or "http_error:unknown"
            if hasattr(exc, "status_code"):
                err_type = f"http_error:{exc.status_code}"
            if attempt == 1:
                first_attempt_latency_ms = latency
                last_exception = exc
                continue
            return CostResult(model_id=model_id, surface=surface_name, passed=False,
                              error_type=err_type, total_latency_ms=latency, attempts=attempt,
                              first_attempt_latency_ms=first_attempt_latency_ms)

        total_latency_ms = (clock() - t0) * 1000.0
        billing = raw.get("billing")
        # Resolve price on the ROUTED model + proxy's pricing month (BLOCKER-1).
        billed_model = (billing or {}).get("billed_model") or model_id
        pricing_month = (billing or {}).get("pricing_month")
        price_entry = pricing_resolver(billed_model, pricing_month)

        last_result = _verdict_from_billing(
            model_id=model_id, surface=surface_name, billing=billing, price_entry=price_entry,
            tolerance_pct=tolerance_pct, total_latency_ms=total_latency_ms, attempts=attempt,
            first_attempt_latency_ms=first_attempt_latency_ms,
        )

        if last_result.passed:
            return last_result
        if attempt == 1:
            et = last_result.error_type or ""
            if et in _RETRY_ERROR_TYPES or et.startswith("http_error:"):
                first_attempt_latency_ms = total_latency_ms
                continue
        return last_result

    if last_result is not None:
        return last_result
    return CostResult(
        model_id=model_id, surface=surface_name, passed=False,
        error_type=("timeout" if isinstance(last_exception, TimeoutError)
                    else f"http_error:{getattr(last_exception, 'status_code', 'unknown')}"),
        attempts=2, first_attempt_latency_ms=first_attempt_latency_ms,
    )
