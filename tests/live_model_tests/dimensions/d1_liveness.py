"""D1 Liveness — verify that each model actually responds with attributable
content within the surface timeout.

Spec produced by Council C3 (2026-05-31):
  - PROPOSER  : claude-qa-tester       (structure + schema + edge cases)
  - CRITIC    : codex-qa-tester-critic (nonce attribution, hardened criteria)

Key design decisions:
  1. Prompt embeds a single-use NONCE the model must echo verbatim. Without
     it, an upstream cache or a generic fallback ("Hello, how can I help?")
     would pass a naive "content non-empty" check and mask a real failure
     (Codex BLOCKER #1 + #2).
  2. Thinking models that produce reasoning but empty content FAIL D1 —
     from the user's perspective the bubble is empty. The diagnostic flag
     ``alive_internal_only`` is set so the scorecard can distinguish this
     from a fully broken model (Codex BLOCKER #3).
  3. One retry on transient failures (timeout, http_5xx, malformed stream)
     to absorb CLI cold-start and Ollama VRAM load. The verdict uses the
     SECOND attempt; first attempt's latency is reported separately.
  4. Timeouts are surface-specific (API ×1.0, MCP ×1.5, UI ×2.0) and
     SEPARATE from any surface startup timeout owned by the surface client.
  5. Quarantined models are short-circuited with ``skipped:quarantined``
     and excluded from the pass-threshold denominator.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# SurfaceClient + SURFACE_TIMEOUT_MULTIPLIER now live in protocols.py (Council
# C5 — resolves the ARCH-DEBT opened in C4 D2-DEC-01). Re-exported here so the
# existing ``from d1_liveness import SurfaceClient`` call sites keep working.
from tests.live_model_tests.protocols import (  # noqa: F401
    SURFACE_TIMEOUT_MULTIPLIER,
    SurfaceClient,
)

# finish_reason values that cause an automatic FAIL regardless of content.
_BAD_FINISH_REASONS = {"content_filter", "error"}


@dataclass
class LivenessResult:
    """Structured verdict of D1 for one (model, surface) pair."""

    model_id: str
    surface: str  # "api" | "mcp" | "ui"
    passed: bool
    error_type: Optional[str] = None
    # Vocabulary of error_type values (see Council C3 §6):
    #   "timeout"
    #   "http_error:<status>"
    #   "empty_response"           — content + reasoning both empty
    #   "alive_internal_only"      — reasoning non-empty but content empty (thinking model burned its budget thinking)
    #   "nonce_missing"            — content non-empty but the unique nonce was NOT echoed (cache/fallback suspected)
    #   "content_filtered"
    #   "bad_finish_reason"
    #   "refusal"                  — model politely refused a benign prompt
    #   "stream_truncated"
    #   "malformed_response"
    #   "skipped:quarantined"
    first_token_latency_ms: Optional[float] = None
    total_latency_ms: float = 0.0
    time_to_finish_reason_ms: float = 0.0
    content_len: int = 0
    reasoning_len: int = 0
    finish_reason: Optional[str] = None
    nonce: Optional[str] = None
    nonce_echoed: bool = False
    attempts: int = 1
    first_attempt_latency_ms: Optional[float] = None
    # Free-form diagnostic excerpts for the scorecard.
    raw_excerpt: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def _build_prompt(nonce: str) -> List[Dict[str, str]]:
    """Single-message prompt that asks the model to echo the nonce.

    Embedding a fresh nonce per call is the cheapest way to distinguish
    "real generation by the announced model" from a cached or fallback
    response that returned plausible content (Codex C3 BLOCKER #1).
    """
    return [
        {
            "role": "user",
            "content": (
                f"Reply with exactly this token, alone on one line, nothing else: ALIVE:{nonce}"
            ),
        }
    ]


def _looks_like_refusal(content: str) -> bool:
    """Detect a polite refusal to a benign prompt. Conservative: only
    catches the obvious "I cannot / I'm not able / I won't" patterns to
    avoid flagging legitimate echoes of unusual words.
    """
    low = content.lower()
    refusal_markers = (
        "i cannot ",
        "i can not ",
        "i can't ",
        "i'm not able ",
        "i am not able ",
        "i'm unable ",
        "i am unable ",
        "i won't ",
        "i will not ",
        "je ne peux pas ",
        "je ne suis pas en mesure",
        "désolé, mais je",
        "désolé mais je ",
    )
    return any(m in low for m in refusal_markers)


def _verdict_from_response(
    *,
    model_id: str,
    surface: str,
    nonce: str,
    response: Dict[str, Any],
    is_thinking: bool,
    total_latency_ms: float,
    time_to_finish_reason_ms: float,
    attempts: int,
    first_attempt_latency_ms: Optional[float],
) -> LivenessResult:
    """Pure function: turn one parsed response into a LivenessResult."""
    content = (response.get("content") or "").strip()
    reasoning = (response.get("reasoning") or "").strip()
    finish_reason = response.get("finish_reason")
    done_received = response.get("done_received", True)
    first_token_ms = response.get("first_token_latency_ms")

    base = dict(
        model_id=model_id,
        surface=surface,
        nonce=nonce,
        first_token_latency_ms=first_token_ms,
        total_latency_ms=total_latency_ms,
        time_to_finish_reason_ms=time_to_finish_reason_ms,
        content_len=len(content),
        reasoning_len=len(reasoning),
        finish_reason=finish_reason,
        attempts=attempts,
        first_attempt_latency_ms=first_attempt_latency_ms,
        raw_excerpt=content[:200],
    )

    # Cas terminal — stream incomplet
    if not done_received and not content:
        return LivenessResult(passed=False, error_type="stream_truncated", **base)

    # Cas terminal — finish_reason interdit
    if finish_reason == "content_filter":
        return LivenessResult(passed=False, error_type="content_filtered", **base)
    if finish_reason in _BAD_FINISH_REASONS:
        return LivenessResult(passed=False, error_type="bad_finish_reason", **base)

    nonce_echoed = f"ALIVE:{nonce}" in content
    base["nonce_echoed"] = nonce_echoed

    # Cas terminal — content totalement vide
    if not content and not reasoning:
        return LivenessResult(passed=False, error_type="empty_response", **base)

    # Thinking model qui n'a produit que du reasoning → l'utilisateur ne voit rien
    if not content and reasoning:
        return LivenessResult(
            passed=False,
            error_type="alive_internal_only",
            extra={"is_thinking": is_thinking, "reasoning_excerpt": reasoning[:200]},
            **base,
        )

    # Le content existe mais sans nonce → cache amont / fallback suspect
    if not nonce_echoed:
        # Distinguer un vrai refus politique d'un cache silencieux pour
        # affiner le scorecard (Codex MAJOR refus + BLOCKER #2).
        return LivenessResult(
            passed=False,
            error_type="refusal" if _looks_like_refusal(content) else "nonce_missing",
            **base,
        )

    # PASS — content non-vide, nonce présent.
    return LivenessResult(passed=True, error_type=None, **base)


def run_d1_liveness(
    *,
    model_id: str,
    surface_client: SurfaceClient,
    catalog_entry: Dict[str, Any],
    quarantine_ids: Optional[set] = None,
    clock: Any = time.monotonic,
) -> LivenessResult:
    """Run D1 Liveness on one (model, surface) pair.

    Wired by the orchestrator ``run_live_tests.py`` for each model in the
    catalog. ``clock`` is injectable so the unit tests don't have to wait.
    """
    quarantine_ids = quarantine_ids or set()
    surface_name = getattr(surface_client, "name", "?")

    # 1. Quarantine short-circuit
    if model_id in quarantine_ids:
        return LivenessResult(
            model_id=model_id,
            surface=surface_name,
            passed=False,
            error_type="skipped:quarantined",
            total_latency_ms=0.0,
            time_to_finish_reason_ms=0.0,
        )

    # 2. Configuration per-model
    base_timeout = float(catalog_entry.get("timeout_s") or 60)
    surface_mult = SURFACE_TIMEOUT_MULTIPLIER.get(surface_name, 1.0)
    timeout = base_timeout * surface_mult
    is_thinking = bool(catalog_entry.get("is_thinking", False))
    max_tokens = 2048 if is_thinking else 64

    # 3. Up to 2 attempts to absorb CLI cold-start / VRAM load
    first_attempt_latency_ms: Optional[float] = None
    last_exception: Optional[Exception] = None
    last_result: Optional[LivenessResult] = None

    for attempt in (1, 2):
        nonce = secrets.token_hex(6).upper()  # fresh nonce per attempt
        prompt = _build_prompt(nonce)
        t0 = clock()
        try:
            raw = surface_client.chat(
                model=model_id,
                messages=prompt,
                stream=True,
                timeout=timeout,
                max_tokens=max_tokens,
                temperature=0.0,
            )
        except TimeoutError:
            latency = (clock() - t0) * 1000.0
            if attempt == 1:
                first_attempt_latency_ms = latency
                last_exception = TimeoutError("timeout")
                continue
            return LivenessResult(
                model_id=model_id,
                surface=surface_name,
                passed=False,
                error_type="timeout",
                nonce=nonce,
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
            return LivenessResult(
                model_id=model_id,
                surface=surface_name,
                passed=False,
                error_type=err_type,
                nonce=nonce,
                total_latency_ms=latency,
                attempts=attempt,
                first_attempt_latency_ms=first_attempt_latency_ms,
            )

        total_latency_ms = (clock() - t0) * 1000.0
        ttfr_ms = total_latency_ms  # SSE: equal to total since we wait for finish_reason

        last_result = _verdict_from_response(
            model_id=model_id,
            surface=surface_name,
            nonce=nonce,
            response=raw,
            is_thinking=is_thinking,
            total_latency_ms=total_latency_ms,
            time_to_finish_reason_ms=ttfr_ms,
            attempts=attempt,
            first_attempt_latency_ms=first_attempt_latency_ms,
        )

        # If the FIRST attempt already passes, no need to retry.
        if last_result.passed:
            return last_result
        # Transient-looking errors → retry. A nonce_missing on attempt 1
        # might be a cache hit racing the real generation; retry once.
        if attempt == 1 and last_result.error_type in {
            "malformed_response",
            "stream_truncated",
            "nonce_missing",
            "empty_response",
            "alive_internal_only",
        }:
            first_attempt_latency_ms = total_latency_ms
            continue
        # Hard failures (refusal, content_filter, bad_finish_reason) don't
        # benefit from a retry — return immediately.
        return last_result

    # Both attempts exhausted with transient errors.
    if last_result is not None:
        return last_result
    return LivenessResult(
        model_id=model_id,
        surface=surface_name,
        passed=False,
        error_type=(
            "timeout"
            if isinstance(last_exception, TimeoutError)
            else f"http_error:{getattr(last_exception, 'status_code', 'unknown')}"
        ),
        attempts=2,
        first_attempt_latency_ms=first_attempt_latency_ms,
    )
