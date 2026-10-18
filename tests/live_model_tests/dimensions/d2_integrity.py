"""D2 Integrity — verify the proxy delivers structurally intact,
correctly-encoded JSON output with attributable nonce and UTF-8 payload.

Spec produced by Council C4 (2026-05-31):
  - PROPOSER  : claude-qa-tester        (structure, schema, edge cases)
  - CRITIC    : codex-qa-tester-critic  (2 BLOCKER + 4 MAJOR + 4 MINOR integrated)

What D2 proves, beyond D1 (the model merely answered):
  the model returns a well-formed JSON object whose ``echo`` field carries the
  per-call nonce verbatim, and whose ``payload`` field round-trips a non-trivial
  UTF-8 string through the proxy WITHOUT corruption (mojibake, replacement char,
  truncation). It is a transport-fidelity + structure test, not a comprehension
  test — the payload is shown verbatim in the prompt on purpose (a faithful copy
  still proves end-to-end encoding integrity).

Key design decisions (Council C4):
  1. Single prompt, two assertions: ``{"echo": <nonce>, "payload": <utf8>}``.
     One call simultaneously tests JSON structure, nonce attribution
     (anti-cache / anti-fallback, like D1) and proxy encoding fidelity.
  2. ``payload_matched`` is computed as a STRICT byte-equality, never inferred
     from "no corruption pattern matched" (Council C4 BLOCKER-1). A payload that
     differs for an unknown reason (truncation, translation, base64) is an
     explicit ``payload_mismatch`` FAIL — never a silent pass.
  3. Mojibake detection is round-trip based (latin-1 / cp1252 re-decode),
     covering 2-byte (é, à) AND 3/4-byte (em-dash, CJK, emoji) sequences
     (Council C4 BLOCKER-2). A regex on the 2-byte range alone would miss
     corruption on 日本語 / 🎯 / — entirely.
  4. NFC/NFD normalisation is a LEGITIMATE transformation, not corruption.
     A payload that is NFC-equal to the expected NFD form PASSES, with
     ``extra["informative"] == "utf8_corrupted:nfc_mismatch"`` and
     ``payload_matched=False`` for an honest diagnostic.
  5. Markdown fence stripping is permitted at one level (```json...```).
     Prose surrounding a fence → ``schema_invalid`` (Council C4 MAJOR-1).
     A stripped-but-valid JSON passes with ``fence_stripped=True`` — not a
     penalty: D2 targets corruption, not instruction-following style.
  6. ``echo`` / ``payload`` must be JSON strings. A non-string (number, list)
     is ``schema_invalid`` (Council C4 MAJOR-2), never coerced via ``str()``.
  7. Content-only matching: all analysis runs on ``content``; ``reasoning`` is
     inspected solely to raise ``echo_in_reasoning_only`` when a thinking model
     burned its budget reasoning and left ``content`` empty (Council C4 + D1 §2).
  8. Retry policy: format/echo/transport errors are retried once;
     ``utf8_corrupted:*``, ``echo_in_reasoning_only``, ``schema_invalid``,
     ``content_filtered``, ``bad_finish_reason`` are NOT retried — they are
     systemic/stable, a retry only burns quota (Council C4 MAJOR-4).
  9. ``is_thinking`` → ``max_tokens`` bumped to 2048 (aligned with D1, not 4096
     — Council C4 MINOR-1), giving room to think before writing the JSON.

ARCH-DEBT (Council C4 D2-DEC-01): ``SurfaceClient`` is imported from
``d1_liveness``. When D3+ land, extract it to a shared ``protocols.py``.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Reuse the surface contract and timeout policy established by D1 (Council C3).
from tests.live_model_tests.dimensions.d1_liveness import (  # noqa: F401
    SURFACE_TIMEOUT_MULTIPLIER,
    SurfaceClient,
)

# ── Constants ────────────────────────────────────────────────────────────────

# Fixed UTF-8 payload covering: accented Latin (2-byte), em-dash (3-byte
# punctuation), CJK (3-byte), emoji (4-byte) and a combining sequence
# (e + COMBINING ACUTE = NFD form of "é") to exercise NFC normalisation.
_UTF8_PAYLOAD: str = "éàçü — 日本語 — 🎯 — é"
#                     ^Latin   ^em ^CJK    ^emoji ^combining (NFD é)

# Markdown fence that spans the WHOLE content (anchored). re.DOTALL so the
# captured JSON body may contain newlines. Backticks inside a JSON string
# value are irrelevant here — the fence is detected on the raw content, and
# json.loads treats any inner backticks as ordinary string data.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)
# Cheap "is there a fence anywhere" probe, used to distinguish prose-around-a
# -fence (schema_invalid) from raw prose (json_parse_error).
_FENCE_PRESENT_RE = re.compile(r"```")

# Fast-path mojibake marker substrings (Council C4 BLOCKER-2). The reliable
# detector is the latin-1/cp1252 round-trip below; these only speed up the
# common cases. Granularity is diagnostic-only — both mojibake and
# payload_mismatch are FAIL, so a marker false-positive never flips a verdict.
_MOJIBAKE_MARKERS: Tuple[str, ...] = (
    "Ã",        # Latin-1 2-byte lead (é→Ã©, à→Ã , ç→Ã§, ü→Ã¼)
    "Â",        # Latin-1 2-byte lead (©, °, NBSP...)
    "â€",  # em-dash / smart-punct mojibake lead (—→â€")
    "ðŸ",  # 4-byte emoji mojibake lead (🎯→ðŸŽ¯)
)

# finish_reason values that are an immediate hard fail (same as D1).
_BAD_FINISH_REASONS = {"content_filter", "error"}

# Transient error_types retried once on attempt 1. schema_invalid is NOT here
# (Council C4 MAJOR-4): a model that returns the wrong JSON SHAPE will not fix
# itself on retry — it is a stable behaviour, not a network blip.
_RETRY_ERROR_TYPES = {
    "json_parse_error",
    "echo_mismatch",
    "echo_missing",
    "payload_missing",
    "payload_mismatch",
    "empty_response",
    "stream_truncated",
    "malformed_response",
    "timeout",
    "http_error:unknown",
}


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class IntegrityResult:
    """Structured verdict of D2 for one (model, surface) pair.

    error_type vocabulary:
      Transport:
        "timeout"
        "http_error:<status>"
        "stream_truncated"
        "malformed_response"
      Format:
        "json_parse_error"   — content not parseable as JSON (after fence-strip)
        "schema_invalid"     — parsed JSON wrong shape / non-string fields /
                               prose wrapping a fence
        "empty_response"     — content + reasoning both empty
      Functional integrity:
        "echo_mismatch"      — "echo" present but value != nonce
        "echo_missing"       — "echo" field absent
        "payload_missing"    — "payload" field absent
        "payload_mismatch"   — payload differs, no known corruption heuristic
      Encoding integrity:
        "utf8_corrupted:mojibake"
        "utf8_corrupted:replacement_char"
      Channel:
        "echo_in_reasoning_only" — nonce in reasoning, content empty
        "content_filtered"
        "bad_finish_reason"
      Meta:
        "skipped:quarantined"

    NB: NFC/NFD divergence is NOT an error_type — it PASSES with
    ``extra["informative"] == "utf8_corrupted:nfc_mismatch"`` and
    ``payload_matched=False``.
    """

    model_id: str
    surface: str  # "api" | "mcp" | "ui"
    passed: bool
    error_type: Optional[str] = None

    # Timing
    total_latency_ms: float = 0.0
    first_token_latency_ms: Optional[float] = None

    # Content metadata
    content_len: int = 0
    reasoning_len: int = 0
    finish_reason: Optional[str] = None

    # Nonce / payload tracking
    nonce: Optional[str] = None
    echo_matched: bool = False
    payload_matched: bool = False  # STRICT equality only (Council C4 BLOCKER-1)
    fence_stripped: bool = False

    # Retry tracking
    attempts: int = 1
    first_attempt_latency_ms: Optional[float] = None

    # Diagnostics
    raw_excerpt: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_fence(content: str) -> Tuple[str, bool, bool]:
    """Return ``(candidate, fence_stripped, prose_with_fence)``.

    - A fence spanning the whole content → ``(inner, True, False)``.
    - A fence with prose before/after → ``(content, False, True)``; the caller
      must classify this as ``schema_invalid`` (Council C4 MAJOR-1).
    - No fence → ``(content, False, False)``.
    """
    m = _FENCE_RE.match(content)
    if m:
        return m.group(1).strip(), True, False
    if _FENCE_PRESENT_RE.search(content):
        return content, False, True
    return content, False, False


def _looks_like_mojibake(returned: str, expected: str) -> bool:
    """True if ``returned`` is ``expected`` re-decoded through a single-byte
    codec (the classic UTF-8 → latin-1/cp1252 double-decode), covering 2-byte
    AND 3/4-byte source sequences (Council C4 BLOCKER-2)."""
    if any(marker in returned for marker in _MOJIBAKE_MARKERS):
        return True
    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = returned.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired == expected:
            return True
    return False


def _detect_utf8_corruption(
    returned: str, expected: str
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """Classify a returned payload against the expected one.

    Returns ``(accepted, error_type, extra)``:
      - exact match            → ``(True,  None, {})``
      - replacement char       → ``(False, "utf8_corrupted:replacement_char", {})``
      - NFC-equal only         → ``(True,  None, {"informative": "...nfc_mismatch"})``
      - mojibake               → ``(False, "utf8_corrupted:mojibake", {})``
      - unknown difference     → ``(False, "payload_mismatch", {})``

    The key invariant (Council C4 BLOCKER-1): a difference is NEVER accepted
    just because no corruption heuristic matched. Only an exact match or a
    benign NFC normalisation passes.
    """
    if returned == expected:
        return True, None, {}
    if "�" in returned:
        return False, "utf8_corrupted:replacement_char", {}
    if unicodedata.normalize("NFC", returned) == unicodedata.normalize("NFC", expected):
        # Canonically-equivalent normalisation — legitimate, not corruption.
        return True, None, {"informative": "utf8_corrupted:nfc_mismatch"}
    if _looks_like_mojibake(returned, expected):
        return False, "utf8_corrupted:mojibake", {}
    return False, "payload_mismatch", {}


def _build_prompt(nonce: str) -> List[Dict[str, str]]:
    """Single-message prompt asking for a strict two-field JSON object.

    The payload is shown verbatim ON PURPOSE: D2 tests proxy transport
    fidelity, not model comprehension. A model that copies the payload
    faithfully still proves end-to-end encoding integrity in both directions.
    """
    return [
        {
            "role": "user",
            "content": (
                "Reply with a JSON object and NOTHING else — "
                "no markdown fence, no explanation, no extra text. "
                "The object must have exactly these two string fields:\n"
                f'  "echo": "{nonce}"\n'
                f'  "payload": "{_UTF8_PAYLOAD}"\n\n'
                "Output only the raw JSON object, starting with { and ending with }."
            ),
        }
    ]


# ── Pure verdict function ─────────────────────────────────────────────────────

def _verdict_from_response(
    *,
    model_id: str,
    surface: str,
    nonce: str,
    response: Dict[str, Any],
    is_thinking: bool,
    total_latency_ms: float,
    attempts: int,
    first_attempt_latency_ms: Optional[float],
) -> IntegrityResult:
    """Pure function: turn one parsed surface response into an IntegrityResult.

    All structural / encoding analysis runs on ``content`` only. ``reasoning``
    is read solely to raise ``echo_in_reasoning_only`` (Council C4 D2-DEC-10).
    """
    content = (response.get("content") or "").strip()
    reasoning = (response.get("reasoning") or "").strip()
    finish_reason = response.get("finish_reason")
    done_received = response.get("done_received", True)
    first_token_ms = response.get("first_token_latency_ms")

    base: Dict[str, Any] = dict(
        model_id=model_id,
        surface=surface,
        nonce=nonce,
        first_token_latency_ms=first_token_ms,
        total_latency_ms=total_latency_ms,
        content_len=len(content),
        reasoning_len=len(reasoning),
        finish_reason=finish_reason,
        attempts=attempts,
        first_attempt_latency_ms=first_attempt_latency_ms,
        raw_excerpt=content[:200],
    )

    # ── Terminal: stream incomplete ──────────────────────────────────────────
    if not done_received and not content:
        return IntegrityResult(passed=False, error_type="stream_truncated", **base)

    # ── Terminal: bad finish_reason ──────────────────────────────────────────
    if finish_reason == "content_filter":
        return IntegrityResult(passed=False, error_type="content_filtered", **base)
    if finish_reason in _BAD_FINISH_REASONS:
        return IntegrityResult(passed=False, error_type="bad_finish_reason", **base)

    # ── Terminal: nothing at all ─────────────────────────────────────────────
    if not content and not reasoning:
        return IntegrityResult(passed=False, error_type="empty_response", **base)

    # ── Terminal: content empty, reasoning present (thinking model) ──────────
    if not content and reasoning:
        nonce_in_reasoning = nonce in reasoning
        return IntegrityResult(
            passed=False,
            error_type="echo_in_reasoning_only" if nonce_in_reasoning else "empty_response",
            extra={
                "is_thinking": is_thinking,
                "reasoning_excerpt": reasoning[:200],
                "nonce_in_reasoning": nonce_in_reasoning,
            },
            **base,
        )

    # ── content is non-empty from here ───────────────────────────────────────
    candidate, fence_stripped, prose_with_fence = _strip_fence(content)
    base["fence_stripped"] = fence_stripped

    # Prose wrapping a fence: the model failed the "only JSON" instruction.
    if prose_with_fence:
        return IntegrityResult(
            passed=False,
            error_type="schema_invalid",
            extra={"reason": "prose surrounding a markdown fence"},
            **base,
        )

    # ── JSON parsing ─────────────────────────────────────────────────────────
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return IntegrityResult(passed=False, error_type="json_parse_error", **base)

    if not isinstance(parsed, dict):
        return IntegrityResult(
            passed=False,
            error_type="schema_invalid",
            extra={"reason": "top-level JSON is not an object"},
            **base,
        )

    # ── Field presence + strict type checks (Council C4 MAJOR-2) ─────────────
    if "echo" not in parsed:
        return IntegrityResult(passed=False, error_type="echo_missing", **base)
    if not isinstance(parsed["echo"], str):
        return IntegrityResult(
            passed=False,
            error_type="schema_invalid",
            extra={"reason": "echo field is not a string"},
            **base,
        )
    if "payload" not in parsed:
        return IntegrityResult(passed=False, error_type="payload_missing", **base)
    if not isinstance(parsed["payload"], str):
        return IntegrityResult(
            passed=False,
            error_type="schema_invalid",
            extra={"reason": "payload field is not a string"},
            **base,
        )

    echo_val: str = parsed["echo"]
    payload_val: str = parsed["payload"]

    # ── Echo verification (anti-cache / anti-fallback) ───────────────────────
    if echo_val != nonce:
        return IntegrityResult(
            passed=False,
            error_type="echo_mismatch",
            extra={"expected_echo": nonce, "got_echo": echo_val[:80]},
            **base,
        )
    base["echo_matched"] = True

    # ── UTF-8 payload verification (Council C4 BLOCKER-1 invariant) ──────────
    accepted, err_type, extra = _detect_utf8_corruption(payload_val, _UTF8_PAYLOAD)
    if not accepted:
        return IntegrityResult(
            passed=False,
            error_type=err_type,
            payload_matched=False,
            extra={**extra, "returned_payload_excerpt": payload_val[:80]},
            **base,
        )

    # PASS — echo verbatim, payload intact (exact or benign NFC). payload_matched
    # is STRICT equality, never inferred.
    return IntegrityResult(
        passed=True,
        error_type=None,
        payload_matched=(payload_val == _UTF8_PAYLOAD),
        extra=extra,
        **base,
    )


# ── Orchestrator ─────────────────────────────────────────────────────────────

def run_d2_integrity(
    *,
    model_id: str,
    surface_client: SurfaceClient,
    catalog_entry: Dict[str, Any],
    quarantine_ids: Optional[set] = None,
    clock: Any = time.monotonic,
) -> IntegrityResult:
    """Run D2 Integrity on one (model, surface) pair.

    Wired by the orchestrator ``run_live_tests.py``. ``clock`` is injectable so
    the unit tests do not have to wait.
    """
    quarantine_ids = quarantine_ids or set()
    surface_name = getattr(surface_client, "name", "?")

    # 1. Quarantine short-circuit
    if model_id in quarantine_ids:
        return IntegrityResult(
            model_id=model_id,
            surface=surface_name,
            passed=False,
            error_type="skipped:quarantined",
            total_latency_ms=0.0,
        )

    # 2. Per-model configuration
    base_timeout = float(catalog_entry.get("timeout_s") or 60)
    surface_mult = SURFACE_TIMEOUT_MULTIPLIER.get(surface_name, 1.0)
    timeout = base_timeout * surface_mult
    is_thinking = bool(catalog_entry.get("is_thinking", False))
    max_tokens = 2048 if is_thinking else 256  # Council C4 MINOR-1 (aligned w/ D1)

    # 3. Up to 2 attempts to absorb CLI cold-start / VRAM load
    first_attempt_latency_ms: Optional[float] = None
    last_exception: Optional[Exception] = None
    last_result: Optional[IntegrityResult] = None

    for attempt in (1, 2):
        nonce = secrets.token_hex(8).upper()  # fresh 16-char opaque nonce
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
            return IntegrityResult(
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
            return IntegrityResult(
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

        last_result = _verdict_from_response(
            model_id=model_id,
            surface=surface_name,
            nonce=nonce,
            response=raw,
            is_thinking=is_thinking,
            total_latency_ms=total_latency_ms,
            attempts=attempt,
            first_attempt_latency_ms=first_attempt_latency_ms,
        )

        if last_result.passed:
            return last_result

        # Retry policy (Council C4 D2-DEC-07 + MAJOR-4)
        if attempt == 1:
            et = last_result.error_type or ""
            if et in _RETRY_ERROR_TYPES or et.startswith("http_error:"):
                first_attempt_latency_ms = total_latency_ms
                continue
        # Hard failure (utf8_corrupted, schema_invalid, echo_in_reasoning_only,
        # content_filtered, bad_finish_reason) or attempt 2 — return as-is.
        return last_result

    # Both attempts exhausted with transient errors.
    if last_result is not None:
        return last_result
    return IntegrityResult(
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
