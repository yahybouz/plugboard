"""D5 Streaming integrity — verify the proxy's SSE stream is structurally
intact: well-formed ``data:`` frames, parseable JSON, ordered deltas, a single
terminal ``[DONE]``, no duplicated chunks, and no reasoning-channel leak into
the content output.

Spec produced by Council C5 (2026-05-31), triplet sécu:
  - PROPOSER : claude-security-officer
  - CRITIC   : codex-security-officer-critic
  - ARBITER  : gemini-security-reviewer

A broken stream is a proxy bug — eval_config sets pass_threshold 1.0, BLOCKER.

Key design decisions (Council C5):
  1. D5 inspects ``raw_sse_lines`` — the wire-level ``data: ...`` lines BEFORE
     json.loads — which only the API HTTP surface can expose. MCP stdio and UI
     Playwright cannot, so they set ``raw_sse_lines=None`` and D5 returns
     ``skipped:surface_non_streaming`` (Council C5 BLOCKER-3). The field lives in
     the derived ``StreamingCapableSurface`` Protocol, never in the base
     ``SurfaceClient`` (LSP).
  2. The prompt embeds a NONCE (Council C5 MAJOR-1). I7 (reasoning-leak)
     fires only when that nonce appears in the CONTENT output of a thinking
     model — eliminating the false positive where a deterministic "1..10" prompt
     made a model's reasoning trivially coincide with its content.
  3. SSE heartbeats — empty ``data:`` lines and ``:`` comment lines — are
     tolerated anywhere, including after ``[DONE]`` (Council C5 MAJOR-2).
  4. The proxy splices a usage chunk before ``[DONE]``; a chunk carrying
     ``usage`` with an empty/absent delta is tolerated, not flagged (D5-DEC-03).
  5. ``finish_reason == "length"`` → PASS with ``extra["truncated"]=True`` — a
     truncated-but-well-formed stream is still structurally sound (D5-DEC-04).
  6. Structural failures are NOT retried (a malformed stream is a stable proxy
     bug, not a network blip); only transport errors are (D5-DEC-10).
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from tests.live_model_tests.protocols import (
    SURFACE_TIMEOUT_MULTIPLIER,
    StreamingCapableSurface,
)

_DONE_SENTINEL = "[DONE]"
_VALID_FINISH_REASONS = {"stop", "length", None}
_BAD_FINISH_REASONS = {"content_filter", "error"}

# Only transport errors are retried; structural failures are stable proxy bugs.
_RETRY_ERROR_TYPES = {
    "timeout",
    "http_error:unknown",
    "empty_response",
}


@dataclass
class StreamingResult:
    """Structured verdict of D5 for one (model, surface) pair.

    error_type vocabulary:
      Structure:
        "sse_malformed:bad_prefix"        — a non-empty, non-comment line not starting with "data:"
        "sse_malformed:json_parse_error"  — a data payload (not [DONE]) is not JSON
        "sse_malformed:missing_choices"   — a chunk has neither choices nor usage
        "sse_malformed:missing_delta"     — a choices chunk lacks choices[0].delta
      Termination:
        "done_missing"        — no [DONE] sentinel
        "done_duplicated"     — [DONE] more than once
        "done_not_terminal"   — a non-empty data frame after [DONE]
      Finish reason:
        "finish_reason_missing" — no stop/length in any chunk
        "bad_finish_reason"     — content_filter / error
      Content integrity:
        "chunk_duplicated"          — consecutive bit-exact non-empty content delta
        "reasoning_leak_in_content" — nonce leaked into content (thinking models)
      Transport:
        "timeout" / "http_error:<status>" / "empty_response"
      Meta (excluded from threshold denominator):
        "skipped:quarantined"
        "skipped:surface_unsupported"
        "skipped:surface_non_streaming"   — raw_sse_lines unavailable (MCP/UI)
    """

    model_id: str
    surface: str
    passed: bool
    error_type: Optional[str] = None
    finding_severity: str = "BLOCKER"
    excluded_from_threshold: bool = False

    # Stream metadata
    chunk_count: int = 0
    done_received: bool = False
    finish_reason: Optional[str] = None
    truncated: bool = False  # finish_reason == "length"

    nonce: Optional[str] = None

    # Timing
    total_latency_ms: float = 0.0
    first_token_latency_ms: Optional[float] = None

    # Retry tracking
    attempts: int = 1
    first_attempt_latency_ms: Optional[float] = None

    raw_excerpt: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


def _build_prompt_d5(nonce: str) -> List[Dict[str, str]]:
    """Single-message prompt for D5.

    The nonce is anti-cache (like D1/D2) AND the I7 reasoning-leak sentinel: if
    it surfaces in the CONTENT output of a thinking model, the reasoning channel
    has leaked. Integers 1..10 guarantee enough delta chunks to exercise
    ordering / duplication checks while staying cheap.
    """
    return [
        {
            "role": "user",
            "content": (
                f"[{nonce}] Output the integers 1 to 10, one per line. "
                "No extra text. Just the numbers."
            ),
        }
    ]


def _classify_lines(raw_sse_lines: List[str]) -> Tuple[List[str], Optional[str]]:
    """Split raw SSE lines into ordered data payloads.

    Returns ``(payloads, bad_prefix_error)``. Empty lines and ``:`` comments
    (SSE heartbeats) are skipped. A non-empty, non-comment line that does not
    start with ``data:`` yields ``"sse_malformed:bad_prefix"``. ``payloads`` are
    the strings after ``data:`` (stripped), INCLUDING ``[DONE]`` and any empty
    data payloads (caller filters empties where relevant).
    """
    payloads: List[str] = []
    for line in raw_sse_lines:
        stripped = (line or "").strip()
        if not stripped:
            continue
        if stripped.startswith(":"):
            continue  # SSE comment / heartbeat
        if not stripped.startswith("data:"):
            return payloads, "sse_malformed:bad_prefix"
        payload = stripped[len("data:") :].strip()
        if not payload:
            continue  # empty `data:` heartbeat — tolerated (Council C5 MAJOR-2)
        payloads.append(payload)
    return payloads, None


def _verdict_from_stream(
    *,
    model_id: str,
    surface: str,
    nonce: str,
    unified: Dict[str, Any],
    is_thinking: bool,
    total_latency_ms: float,
    attempts: int,
    first_attempt_latency_ms: Optional[float],
) -> StreamingResult:
    """Pure function: one streamed surface response → one StreamingResult."""
    raw_sse_lines = unified.get("raw_sse_lines")
    content = (unified.get("content") or "").strip()
    reasoning = (unified.get("reasoning") or "").strip()
    first_token_ms = unified.get("first_token_latency_ms")

    base: Dict[str, Any] = dict(
        model_id=model_id,
        surface=surface,
        nonce=nonce,
        first_token_latency_ms=first_token_ms,
        total_latency_ms=total_latency_ms,
        attempts=attempts,
        first_attempt_latency_ms=first_attempt_latency_ms,
        raw_excerpt=content[:200],
    )

    # ── Surface cannot expose raw SSE → skip ─────────────────────────────────
    if raw_sse_lines is None:
        return StreamingResult(
            passed=False,
            error_type="skipped:surface_non_streaming",
            excluded_from_threshold=True,
            **base,
        )

    # ── Transport: nothing came back ─────────────────────────────────────────
    if not raw_sse_lines:
        return StreamingResult(
            passed=False, error_type="empty_response", finding_severity="MAJOR", **base
        )

    # ── I1: well-formed line prefixes ────────────────────────────────────────
    payloads, bad_prefix = _classify_lines(raw_sse_lines)
    if bad_prefix:
        return StreamingResult(passed=False, error_type=bad_prefix, **base)

    # ── I5: [DONE] sentinel — exactly once, terminal ────────────────────────
    done_indices = [i for i, p in enumerate(payloads) if p == _DONE_SENTINEL]
    if len(done_indices) == 0:
        return StreamingResult(passed=False, error_type="done_missing", **base)
    if len(done_indices) > 1:
        return StreamingResult(passed=False, error_type="done_duplicated", **base)
    done_idx = done_indices[0]
    # Anything (non-empty data) after [DONE] is a violation. Empty data frames
    # and comments were already filtered by _classify_lines.
    if done_idx != len(payloads) - 1:
        return StreamingResult(passed=False, error_type="done_not_terminal", **base)

    data_payloads = payloads[:done_idx]  # all JSON chunks before [DONE]
    base["done_received"] = True
    base["chunk_count"] = len(data_payloads)

    # ── I2 + I3: each chunk parses and has a usable shape ────────────────────
    finish_reason: Optional[str] = None
    prev_dup_key: Optional[str] = None
    for payload in data_payloads:
        try:
            chunk = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return StreamingResult(
                passed=False, error_type="sse_malformed:json_parse_error", **base
            )

        choices = chunk.get("choices")
        has_usage = "usage" in chunk
        if not choices:
            # A usage-only chunk (no choices) is tolerated; anything else is malformed.
            if has_usage:
                continue
            return StreamingResult(
                passed=False, error_type="sse_malformed:missing_choices", **base
            )

        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        if "delta" not in first_choice:
            return StreamingResult(
                passed=False, error_type="sse_malformed:missing_delta", **base
            )

        # ── I4: collect finish_reason ────────────────────────────────────────
        fr = first_choice.get("finish_reason")
        if fr is not None:
            if fr in _BAD_FINISH_REASONS:
                base["finish_reason"] = fr
                return StreamingResult(
                    passed=False, error_type="bad_finish_reason", **base
                )
            if fr not in _VALID_FINISH_REASONS:
                base["finish_reason"] = fr
                return StreamingResult(
                    passed=False, error_type="bad_finish_reason", **base
                )
            finish_reason = fr

        # ── I6: consecutive bit-exact duplicate of a non-empty content delta ─
        delta = first_choice.get("delta") or {}
        delta_content = delta.get("content") or ""
        if delta_content.strip():
            if payload == prev_dup_key:
                base["finish_reason"] = finish_reason
                return StreamingResult(
                    passed=False, error_type="chunk_duplicated", **base
                )
            prev_dup_key = payload
        else:
            prev_dup_key = None  # empty-content deltas don't anchor duplication

    base["finish_reason"] = finish_reason

    # ── I4: at least one stop/length must have been seen ────────────────────
    if finish_reason is None:
        return StreamingResult(passed=False, error_type="finish_reason_missing", **base)
    truncated = finish_reason == "length"
    base["truncated"] = truncated

    # ── I7: reasoning leak — nonce in content output (thinking models only) ──
    leaked = _detect_reasoning_leak(reasoning, content, nonce, is_thinking)
    if leaked is not None:
        return StreamingResult(
            passed=False,
            error_type="reasoning_leak_in_content",
            extra={"leaked_nonce": leaked},
            **base,
        )

    # ── PASS (length is sound, just flagged) ─────────────────────────────────
    extra = {"truncated": True} if truncated else {}
    return StreamingResult(passed=True, error_type=None, extra=extra, **base)


def _detect_reasoning_leak(
    aggregated_reasoning: str,
    aggregated_content: str,
    nonce: str,
    is_thinking: bool,
) -> Optional[str]:
    """I7 — nonce-based leak detection (Council C5 MAJOR-1 arbitrage).

    A thinking model's reasoning may legitimately contain the nonce (it was in
    the prompt). A leak occurs when the nonce ALSO appears in the content output
    — the proxy failed to isolate the reasoning channel. Non-thinking models
    have no reasoning channel, so no leak is possible. Returns the nonce on a
    leak, else None.
    """
    if not is_thinking:
        return None
    if nonce in aggregated_content:
        return nonce
    return None


def run_d5_streaming_integrity(
    *,
    model_id: str,
    surface_client: StreamingCapableSurface,
    catalog_entry: Dict[str, Any],
    quarantine_ids: Optional[set] = None,
    clock: Any = time.monotonic,
) -> StreamingResult:
    """Run D5 Streaming integrity on one (model, surface) pair.

    In C5 only the API surface implements ``StreamingCapableSurface``; MCP/UI
    clients return ``raw_sse_lines=None`` and short-circuit to
    ``skipped:surface_non_streaming``.
    """
    quarantine_ids = quarantine_ids or set()
    surface_name = getattr(surface_client, "name", "?")

    if model_id in quarantine_ids:
        return StreamingResult(
            model_id=model_id,
            surface=surface_name,
            passed=False,
            error_type="skipped:quarantined",
            excluded_from_threshold=True,
        )

    valid_surfaces = catalog_entry.get("surfaces") or []
    if valid_surfaces and surface_name not in valid_surfaces:
        return StreamingResult(
            model_id=model_id,
            surface=surface_name,
            passed=False,
            error_type="skipped:surface_unsupported",
            excluded_from_threshold=True,
        )

    base_timeout = float(catalog_entry.get("timeout_s") or 60)
    timeout = base_timeout * SURFACE_TIMEOUT_MULTIPLIER.get(surface_name, 1.0)
    is_thinking = bool(catalog_entry.get("is_thinking", False))
    max_tokens = 2048 if is_thinking else 64

    first_attempt_latency_ms: Optional[float] = None
    last_exception: Optional[Exception] = None
    last_result: Optional[StreamingResult] = None

    for attempt in (1, 2):
        nonce = secrets.token_hex(8).upper()
        prompt = _build_prompt_d5(nonce)
        t0 = clock()
        try:
            raw = surface_client.chat(
                model=model_id,
                messages=prompt,
                stream=True,  # D5-DEC-08 — always streamed
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
            return StreamingResult(
                model_id=model_id,
                surface=surface_name,
                passed=False,
                error_type="timeout",
                finding_severity="MAJOR",
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
            return StreamingResult(
                model_id=model_id,
                surface=surface_name,
                passed=False,
                error_type=err_type,
                finding_severity="MAJOR",
                nonce=nonce,
                total_latency_ms=latency,
                attempts=attempt,
                first_attempt_latency_ms=first_attempt_latency_ms,
            )

        total_latency_ms = (clock() - t0) * 1000.0
        last_result = _verdict_from_stream(
            model_id=model_id,
            surface=surface_name,
            nonce=nonce,
            unified=raw,
            is_thinking=is_thinking,
            total_latency_ms=total_latency_ms,
            attempts=attempt,
            first_attempt_latency_ms=first_attempt_latency_ms,
        )

        if (
            last_result.passed
            or last_result.error_type == "skipped:surface_non_streaming"
        ):
            return last_result
        # Only transport errors are retried; structural fails are stable.
        if attempt == 1:
            et = last_result.error_type or ""
            if et in _RETRY_ERROR_TYPES or et.startswith("http_error:"):
                first_attempt_latency_ms = total_latency_ms
                continue
        return last_result

    if last_result is not None:
        return last_result
    return StreamingResult(
        model_id=model_id,
        surface=surface_name,
        passed=False,
        error_type=(
            "timeout"
            if isinstance(last_exception, TimeoutError)
            else f"http_error:{getattr(last_exception, 'status_code', 'unknown')}"
        ),
        finding_severity="MAJOR",
        attempts=2,
        first_attempt_latency_ms=first_attempt_latency_ms,
    )
