"""D3 Internet — verify each model's observed web-access capability matches
the ground truth declared in ``catalog.yaml`` (``expected_internet``).

Spec produced by Council C5 (2026-05-31), triplet sécu:
  - PROPOSER : claude-security-officer
  - CRITIC   : codex-security-officer-critic
  - ARBITER  : gemini-security-reviewer (3 BLOCKER + 1 MAJOR-blocking integrated)

Why this is a BLOCKER dimension (eval_config: pass_threshold 1.0): a model
catalogued ``offline`` that confidently states today's date is hallucinating a
capability it does not have; a model catalogued ``online`` that answers
``NO-WEB-ACCESS`` has lost a capability. Either is a real divergence.

Key design decisions (Council C5):
  1. Single source of truth — reuses ``plugboard.probes.internet`` (the same
     probe v2 the production ``/admin/models/check-internet`` endpoint uses).
     D3 never re-implements the date matcher (D3-DEC-01).
  2. CONTENT-ONLY matching — the probe is evaluated with ``ignore_reasoning=True``
     (Council C5 BLOCKER-1). A thinking model that *reasons* "it must be late May
     2026, Sunday" but tells the user ``NO-WEB-ACCESS`` is OFFLINE from the user's
     viewpoint. Consistent with D2's content-only principle.
  3. signal_in_reasoning_only — when the content has no date but the reasoning
     channel does, the read is ambiguous: excluded from the pass-threshold
     denominator, surfaced as a MAJOR diagnostic (Council C5 BLOCKER-1 §extra).
  4. Confidence-tiered severity (Council C5 BLOCKER-2) — an ``offline→online``
     mismatch seen only at ``confidence=low`` (today's date, but neither weekday
     nor yesterday nor a URL) is too weak to assert an online hallucination:
     ``finding_severity=MAJOR``. All other mismatches are ``BLOCKER``. The
     dimension severity is ``max(finding_severity)`` over non-skipped results;
     the 1.0 threshold is unchanged.
  5. Tri-state ground truth — ``expected_internet: unknown`` →
     ``skipped:unknown_ground_truth``, excluded from the denominator and merely
     observed (D3-DEC-03). gpt-oss:120b-cloud sits here until re-probed.
  6. max_tokens is taken from ``build_internet_probe_body`` so the thinking-model
     budget bump lives in ONE place (Council C5 MAJOR-3) — a truncated thinking
     model must not be mistaken for offline.

The non-inferable "recent web fact" 2nd probe was REJECTED (D3-DEC-05): a
hard-coded fact goes stale, and an external-URL nonce would make the test itself
depend on internet. Confidence tiering (decision 4) mitigates the residual
false-positive risk without that cost.
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from plugboard.probes.internet import (
    InternetProbeResult,
    build_internet_probe_body,
    evaluate_probe_response,
)
from tests.live_model_tests.protocols import (
    SURFACE_TIMEOUT_MULTIPLIER,
    SurfaceClient,
)

_BAD_FINISH_REASONS = {"content_filter", "error"}

# error_types retried once on attempt 1 (transient / ambiguous).
_RETRY_ERROR_TYPES = {
    "probe_inconclusive",
    "timeout",
    "http_error:unknown",
    "stream_truncated",
    "malformed_response",
    "empty_response",
}


@dataclass
class InternetResult:
    """Structured verdict of D3 for one (model, surface) pair.

    error_type vocabulary:
      Verdict (stable, NOT retried):
        "internet_mismatch:expected_online_got_offline"  → finding_severity BLOCKER
        "internet_mismatch:expected_offline_got_online"  → BLOCKER, or MAJOR if confidence=low
        "signal_in_reasoning_only"                       → MAJOR, excluded from threshold
      Probe-level (retried):
        "probe_inconclusive"     → content gave today's date but neither weekday
                                   nor yesterday (matcher ambiguity)
      Transport (retried):
        "timeout" / "http_error:<status>" / "stream_truncated"
        / "malformed_response" / "empty_response"
      Stable, NOT retried:
        "content_filtered" / "bad_finish_reason"
      Meta (excluded from threshold denominator):
        "skipped:quarantined"
        "skipped:unknown_ground_truth"
        "skipped:surface_unsupported"
    """

    model_id: str
    surface: str
    passed: bool
    error_type: Optional[str] = None

    # Severity tiering (Council C5 BLOCKER-2). Meaningful when passed=False.
    finding_severity: str = "BLOCKER"  # "BLOCKER" | "MAJOR"
    # When True, the C10 aggregator excludes this result from the pass-threshold
    # denominator (skips + ambiguous reasoning-only reads).
    excluded_from_threshold: bool = False

    # Ground truth + observation (for the scorecard).
    expected_internet: Optional[bool] = None  # None when ground truth is unknown
    observed_status: Optional[str] = None  # "online" | "offline"
    observed_confidence: Optional[str] = None  # "high" | "medium" | "low"
    date_today_present: bool = False
    weekday_present: bool = False
    date_yesterday_present: bool = False
    url_present: bool = False

    # Timing
    total_latency_ms: float = 0.0
    first_token_latency_ms: Optional[float] = None

    # Retry tracking
    attempts: int = 1
    first_attempt_latency_ms: Optional[float] = None

    raw_excerpt: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def _normalize_expected_internet(value: Any) -> Optional[bool]:
    """Map the catalog field to True / False / None(unknown)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"true", "yes", "online", "1"}:
        return True
    if s in {"false", "no", "offline", "0"}:
        return False
    return None  # "unknown" or anything unrecognised


def _adapt_to_probe_shape(unified: Dict[str, Any]) -> Dict[str, Any]:
    """Map the surface's unified dict (flat content/reasoning) onto the
    OpenAI-style payload ``evaluate_probe_response`` expects."""
    return {
        "choices": [
            {
                "message": {
                    "content": unified.get("content") or "",
                    "reasoning": unified.get("reasoning") or "",
                }
            }
        ]
    }


def _signals_from_probe(probe: InternetProbeResult) -> Dict[str, Any]:
    return dict(
        observed_status=probe.status,
        observed_confidence=probe.confidence,
        date_today_present=probe.date_today_present,
        weekday_present=probe.weekday_present,
        date_yesterday_present=probe.date_yesterday_present,
        url_present=probe.url_present,
    )


def _verdict_from_unified(
    *,
    model_id: str,
    surface: str,
    expected_internet: Optional[bool],
    unified: Dict[str, Any],
    now: Optional[_dt.datetime],
    total_latency_ms: float,
    attempts: int,
    first_attempt_latency_ms: Optional[float],
) -> InternetResult:
    """Pure function: one surface response → one InternetResult."""
    content = (unified.get("content") or "").strip()
    reasoning = (unified.get("reasoning") or "").strip()
    finish_reason = unified.get("finish_reason")
    done_received = unified.get("done_received", True)
    first_token_ms = unified.get("first_token_latency_ms")

    base: Dict[str, Any] = dict(
        model_id=model_id,
        surface=surface,
        expected_internet=expected_internet,
        first_token_latency_ms=first_token_ms,
        total_latency_ms=total_latency_ms,
        attempts=attempts,
        first_attempt_latency_ms=first_attempt_latency_ms,
        raw_excerpt=content[:200],
    )

    # ── Terminal transport-ish ───────────────────────────────────────────────
    if not done_received and not content and not reasoning:
        return InternetResult(
            passed=False,
            error_type="stream_truncated",
            finding_severity="MAJOR",
            **base,
        )
    if finish_reason == "content_filter":
        return InternetResult(
            passed=False,
            error_type="content_filtered",
            finding_severity="MAJOR",
            **base,
        )
    if finish_reason in _BAD_FINISH_REASONS:
        return InternetResult(
            passed=False,
            error_type="bad_finish_reason",
            finding_severity="MAJOR",
            **base,
        )
    if not content and not reasoning:
        return InternetResult(
            passed=False, error_type="empty_response", finding_severity="MAJOR", **base
        )

    payload = _adapt_to_probe_shape(unified)
    primary = evaluate_probe_response(payload, now=now, ignore_reasoning=True)
    base.update(_signals_from_probe(primary))

    if primary.status == "offline":
        # Date in CONTENT but safeguards missing → genuinely ambiguous, retry.
        if primary.date_today_present:
            return InternetResult(
                passed=False,
                error_type="probe_inconclusive",
                finding_severity="MAJOR",
                **base,
            )
        # No date in content, but reasoning revealed it → ambiguous read,
        # excluded from threshold (Council C5 BLOCKER-1 §extra).
        if reasoning:
            secondary = evaluate_probe_response(
                payload, now=now, ignore_reasoning=False
            )
            if secondary.status == "online":
                return InternetResult(
                    passed=False,
                    error_type="signal_in_reasoning_only",
                    finding_severity="MAJOR",
                    excluded_from_threshold=True,
                    extra={"reasoning_excerpt": reasoning[:200]},
                    **base,
                )

    observed_online = primary.has_internet

    # ── Ground truth comparison ──────────────────────────────────────────────
    if expected_internet is True:
        if observed_online:
            return InternetResult(passed=True, error_type=None, **base)
        return InternetResult(
            passed=False,
            error_type="internet_mismatch:expected_online_got_offline",
            finding_severity="BLOCKER",
            **base,
        )

    # expected_internet is False
    if not observed_online:
        return InternetResult(passed=True, error_type=None, **base)
    # offline expected but observed online → severity depends on confidence.
    severity = "MAJOR" if primary.confidence == "low" else "BLOCKER"
    return InternetResult(
        passed=False,
        error_type="internet_mismatch:expected_offline_got_online",
        finding_severity=severity,
        **base,
    )


def run_d3_internet(
    *,
    model_id: str,
    surface_client: SurfaceClient,
    catalog_entry: Dict[str, Any],
    quarantine_ids: Optional[set] = None,
    clock: Any = time.monotonic,
    now: Optional[_dt.datetime] = None,
) -> InternetResult:
    """Run D3 Internet on one (model, surface) pair.

    ``now`` is injectable so the probe matcher (which compares against "today")
    is deterministic under test. ``clock`` is injectable for latency.
    """
    quarantine_ids = quarantine_ids or set()
    surface_name = getattr(surface_client, "name", "?")

    # 1. Quarantine short-circuit
    if model_id in quarantine_ids:
        return InternetResult(
            model_id=model_id,
            surface=surface_name,
            passed=False,
            error_type="skipped:quarantined",
            excluded_from_threshold=True,
        )

    # 2. Surface not valid for this model per the catalog
    valid_surfaces = catalog_entry.get("surfaces") or []
    if valid_surfaces and surface_name not in valid_surfaces:
        return InternetResult(
            model_id=model_id,
            surface=surface_name,
            passed=False,
            error_type="skipped:surface_unsupported",
            excluded_from_threshold=True,
        )

    # 3. Unknown ground truth → observe only, out of denominator
    expected_internet = _normalize_expected_internet(
        catalog_entry.get("expected_internet")
    )
    if expected_internet is None:
        # Still probe once so the scorecard can show what was observed.
        return _observe_only(
            model_id=model_id,
            surface_client=surface_client,
            catalog_entry=catalog_entry,
            surface_name=surface_name,
            clock=clock,
            now=now,
        )

    # 4. Configuration — single source of truth for max_tokens (MAJOR-3)
    base_timeout = float(catalog_entry.get("timeout_s") or 60)
    timeout = base_timeout * SURFACE_TIMEOUT_MULTIPLIER.get(surface_name, 1.0)
    body = build_internet_probe_body(model_id)

    first_attempt_latency_ms: Optional[float] = None
    last_exception: Optional[Exception] = None
    last_result: Optional[InternetResult] = None

    for attempt in (1, 2):
        t0 = clock()
        try:
            raw = surface_client.chat(
                model=model_id,
                messages=body["messages"],
                stream=True,
                timeout=timeout,
                max_tokens=body["max_tokens"],
                temperature=body.get("temperature", 0.0),
            )
        except TimeoutError:
            latency = (clock() - t0) * 1000.0
            if attempt == 1:
                first_attempt_latency_ms = latency
                last_exception = TimeoutError("timeout")
                continue
            return InternetResult(
                model_id=model_id,
                surface=surface_name,
                passed=False,
                error_type="timeout",
                finding_severity="MAJOR",
                expected_internet=expected_internet,
                total_latency_ms=latency,
                attempts=attempt,
                first_attempt_latency_ms=first_attempt_latency_ms,
            )
        except Exception as exc:  # noqa: BLE001
            latency = (clock() - t0) * 1000.0
            err_type = getattr(exc, "error_type", None) or "http_error:unknown"
            if hasattr(exc, "status_code"):
                err_type = f"http_error:{exc.status_code}"
            if attempt == 1:
                first_attempt_latency_ms = latency
                last_exception = exc
                continue
            return InternetResult(
                model_id=model_id,
                surface=surface_name,
                passed=False,
                error_type=err_type,
                finding_severity="MAJOR",
                expected_internet=expected_internet,
                total_latency_ms=latency,
                attempts=attempt,
                first_attempt_latency_ms=first_attempt_latency_ms,
            )

        total_latency_ms = (clock() - t0) * 1000.0
        last_result = _verdict_from_unified(
            model_id=model_id,
            surface=surface_name,
            expected_internet=expected_internet,
            unified=raw,
            now=now,
            total_latency_ms=total_latency_ms,
            attempts=attempt,
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
    return InternetResult(
        model_id=model_id,
        surface=surface_name,
        passed=False,
        error_type=(
            "timeout"
            if isinstance(last_exception, TimeoutError)
            else f"http_error:{getattr(last_exception, 'status_code', 'unknown')}"
        ),
        finding_severity="MAJOR",
        expected_internet=expected_internet,
        attempts=2,
        first_attempt_latency_ms=first_attempt_latency_ms,
    )


def _observe_only(
    *,
    model_id: str,
    surface_client: SurfaceClient,
    catalog_entry: Dict[str, Any],
    surface_name: str,
    clock: Any,
    now: Optional[_dt.datetime],
) -> InternetResult:
    """Probe a model whose ground truth is ``unknown`` — record what was
    observed, but the verdict is ``skipped:unknown_ground_truth`` (excluded
    from the threshold denominator)."""
    base_timeout = float(catalog_entry.get("timeout_s") or 60)
    timeout = base_timeout * SURFACE_TIMEOUT_MULTIPLIER.get(surface_name, 1.0)
    body = build_internet_probe_body(model_id)
    t0 = clock()
    try:
        raw = surface_client.chat(
            model=model_id,
            messages=body["messages"],
            stream=True,
            timeout=timeout,
            max_tokens=body["max_tokens"],
            temperature=body.get("temperature", 0.0),
        )
    except Exception:  # noqa: BLE001 — observation is best-effort
        return InternetResult(
            model_id=model_id,
            surface=surface_name,
            passed=False,
            error_type="skipped:unknown_ground_truth",
            excluded_from_threshold=True,
            expected_internet=None,
        )
    total_latency_ms = (clock() - t0) * 1000.0
    probe = evaluate_probe_response(
        _adapt_to_probe_shape(raw), now=now, ignore_reasoning=True
    )
    return InternetResult(
        model_id=model_id,
        surface=surface_name,
        passed=False,
        error_type="skipped:unknown_ground_truth",
        excluded_from_threshold=True,
        expected_internet=None,
        total_latency_ms=total_latency_ms,
        raw_excerpt=(raw.get("content") or "")[:200],
        **_signals_from_probe(probe),
    )
