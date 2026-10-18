"""D4 Identity (cooperative) — verify each model's self-declared family/vendor
matches the catalog ground truth, using an alias-tolerant matcher.

Spec produced by Council C6 (2026-05-31), triplet sécu:
  - PROPOSER : claude-security-officer
  - CRITIC   : codex-security-officer-critic (1 BLOCKER + 4 MAJOR integrated)

⚠️  COOPERATIVE TEST — IMPORTANT LIMITATION ⚠️
This dimension asks the model to declare itself and compares the declaration to
``catalog.yaml``. A misbehaving model CAN lie; a misbehaving proxy CAN disguise
the routing. **D4 is not a cryptographic anti-disguise proof.** True disguise
detection (cross-checking backend X-Provider / X-Model trace headers against
signed vendor metadata) is tracked as **T-01** and explicitly NOT in C6 scope.

Key design decisions (Council C6):
  1. Cooperative, MAJOR severity (eval_config 0.80). A wrong self-id is a signal,
     not proof of intrusion (D4-DEC-01).
  2. Catalog is the single source of truth: ``expected_family``/``expected_vendor``
     + the top-level ``aliases:`` section, injected via the ``aliases`` argument.
     No alias is hard-coded here (D4-DEC-02).
  3. Bilingual FR+EN JSON prompt with a nonce echo (D2 pattern). Fields:
     ``{"echo", "family", "vendor", "training_cutoff"}`` (D4-DEC-03).
  4. Content-only matching; declaration only in reasoning →
     ``identity_in_reasoning_only`` (MAJOR, excluded) — D4-DEC-04.
  5. TOLERANT matcher = phrase-token containment, NOT raw substring
     (Council C6 BLOCKER-1 + MAJOR-1): a declared value must contribute ≥3
     characters and an alias matches only as a CONTIGUOUS token sub-sequence.
     This kills the ``vendor="a"`` single-letter bypass and the
     ``"opusculum"⊃"opus"`` false positive.
  6. Refusal detection = exact-field + word-boundary phrases, never short
     substrings (Council C6 MAJOR-2): ``"lunar-ai"`` is no longer a false
     ``"na"`` refusal. A WHOLESALE refusal (BOTH fields refused) →
     ``identity_refused`` (MINOR, excluded); a PARTIAL refusal (one field
     refused, the other declared) is a ``identity_mismatch`` and stays in the
     denominator — closing the "declare one field unknown to escape" evasion
     (Council C6 MAJOR-3).
  7. ``aliases`` empty → strict canonical matching with an explicit
     ``extra["warn"]="aliases_empty_strict_mode"`` (Council C6 MAJOR-4), never a
     silent wave of false mismatches.
  8. ``training_cutoff`` is informative only, never blocking (D4-DEC-07).
"""

from __future__ import annotations

import json
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from tests.live_model_tests.protocols import (
    SURFACE_TIMEOUT_MULTIPLIER,
    SurfaceClient,
)

# ── Constants ────────────────────────────────────────────────────────────────

# Exact field values that signal a polite refusal to self-identify.
_EXACT_REFUSAL_FIELDS = frozenset({
    "unknown", "undisclosed", "unspecified", "none", "null",
    "n/a", "na", "not applicable", "not specified", "not provided",
    "not available", "prefer not to say", "confidential", "withheld",
    "redacted", "private",
})

# Word-boundary phrase patterns for refusals embedded in a verbose field.
_REFUSAL_PHRASE_PATTERNS = [
    re.compile(r"\b(?:cannot|can't|can\s+not)\s+(?:disclose|say|determine|identify|share)\b"),
    re.compile(r"\bprefer\s+not\s+(?:to\s+)?(?:say|disclose)\b"),
    re.compile(r"\bnot\s+able\s+to\s+(?:disclose|say|determine|identify)\b"),
    re.compile(r"\b(?:do\s+not|don't)\s+know\s+(?:my\s+)?(?:name|identity|family|vendor|maker)\b"),
]

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# training_cutoff plausibility — YYYY or YYYY-MM, lenient (D4-DEC-07).
_CUTOFF_FMT_RE = re.compile(r"^\d{4}(-\d{2})?$")

# Markdown fence (same as D2).
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)
_FENCE_PRESENT_RE = re.compile(r"```")

_BAD_FINISH_REASONS = {"content_filter", "error"}

# Transient error_types retried once on attempt 1 (Council C6 D4-DEC-09).
_RETRY_ERROR_TYPES = {
    "json_parse_error",
    "echo_mismatch",
    "echo_missing",
    "family_missing",
    "vendor_missing",
    "empty_response",
    "stream_truncated",
    "malformed_response",
    "timeout",
    "http_error:unknown",
}

_DISCLAIMER = "cooperative_test_see_T01"


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class IdentityResult:
    """Structured verdict of D4 for one (model, surface) pair.

    error_type vocabulary:
      Verdict (stable, NOT retried):
        "identity_mismatch:family"   "identity_mismatch:vendor"
        "identity_mismatch:both"      → MAJOR
        "identity_refused"            → MINOR, excluded (BOTH fields refused only)
        "identity_in_reasoning_only"  → MAJOR, excluded
        "schema_invalid"              → MAJOR
      Format (retried):
        "json_parse_error" "echo_mismatch" "echo_missing"
        "family_missing" "vendor_missing"
        "empty_response" "stream_truncated" "malformed_response"
      Transport (retried):
        "timeout" "http_error:<status>"
      Terminal (stable):
        "content_filtered" "bad_finish_reason"
      Meta (excluded):
        "skipped:quarantined" "skipped:surface_unsupported" "skipped:no_ground_truth"
    """

    model_id: str
    surface: str
    passed: bool
    error_type: Optional[str] = None
    finding_severity: str = "MAJOR"  # "MAJOR" | "MINOR"
    excluded_from_threshold: bool = False

    expected_family: Optional[str] = None
    expected_vendor: Optional[str] = None
    declared_family: Optional[str] = None
    declared_vendor: Optional[str] = None
    declared_cutoff: Optional[str] = None
    cutoff_format_valid: Optional[bool] = None
    family_matched: bool = False
    vendor_matched: bool = False

    nonce: Optional[str] = None
    echo_matched: bool = False
    fence_stripped: bool = False

    content_len: int = 0
    reasoning_len: int = 0
    finish_reason: Optional[str] = None

    total_latency_ms: float = 0.0
    first_token_latency_ms: Optional[float] = None

    attempts: int = 1
    first_attempt_latency_ms: Optional[float] = None

    raw_excerpt: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tokenize(value: str) -> List[str]:
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    return _TOKEN_RE.findall(text)


def _phrase_in_tokens(haystack: List[str], needle: List[str]) -> bool:
    """True iff ``needle`` appears as a contiguous sub-sequence of ``haystack``."""
    if not needle:
        return False
    n = len(needle)
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def _matches_alias(declared: str, canonical: str, alias_list: List[str]) -> bool:
    """Phrase-token matcher (Council C6 BLOCKER-1/MAJOR-1).

    A declared value matches the expected canonical (or one of its aliases)
    only when the candidate's token sequence appears CONTIGUOUSLY inside the
    declared token sequence — and the declaration must carry ≥3 meaningful
    characters. This neutralises single-letter bypasses (``"a"`` ⊄ "anthropic")
    and prefix accidents (``"opusculum"`` ⊅ "opus"). Comparison is confined to
    the single JSON field value, so free-form prose mentioning a vendor cannot
    forge a match (alias-collision resistance).
    """
    if not isinstance(declared, str):
        return False
    declared_tokens = _tokenize(declared)
    if sum(len(t) for t in declared_tokens) < 3:
        return False
    for candidate in [canonical, *(alias_list or [])]:
        candidate_tokens = _tokenize(candidate)
        if candidate_tokens and _phrase_in_tokens(declared_tokens, candidate_tokens):
            return True
    return False


def _normalize_field(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold().strip())


def _is_refusal(value: Any) -> bool:
    """Refusal detection via exact-field + word-boundary phrases only
    (Council C6 MAJOR-2). No short substring matching."""
    if not isinstance(value, str):
        return False
    text = _normalize_field(value)
    if not text:
        return False
    if text in _EXACT_REFUSAL_FIELDS:
        return True
    return any(p.search(text) for p in _REFUSAL_PHRASE_PATTERNS)


def _strip_fence(content: str) -> Tuple[str, bool, bool]:
    m = _FENCE_RE.match(content)
    if m:
        return m.group(1).strip(), True, False
    if _FENCE_PRESENT_RE.search(content):
        return content, False, True
    return content, False, False


def _try_parse_json(candidate: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_prompt(nonce: str) -> List[Dict[str, str]]:
    """Bilingual FR+EN cooperative self-id prompt (D4-DEC-03)."""
    return [
        {
            "role": "user",
            "content": (
                "Please identify yourself / Merci de t'identifier.\n\n"
                "Reply with a JSON object and NOTHING else (no markdown fence, no prose).\n"
                "Réponds UNIQUEMENT par un objet JSON (pas de markdown, pas de texte autour).\n\n"
                "The object MUST contain these fields / L'objet DOIT contenir ces champs:\n"
                f'  "echo": "{nonce}"            (copy verbatim / recopie à l\'identique)\n'
                '  "family": "<your model family>"   (e.g. claude, gpt, gemini, kimi, qwen, deepseek, glm, llama)\n'
                '  "vendor": "<your maker>"          (e.g. anthropic, openai, google, moonshot, alibaba, deepseek, zhipu, meta)\n'
                '  "training_cutoff": "<YYYY-MM or YYYY or null>"\n\n'
                'If you cannot disclose a field, use the string "unknown".\n'
                'Si tu ne peux pas révéler un champ, utilise "unknown".\n\n'
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
    expected_family: str,
    expected_vendor: str,
    aliases: Dict[str, Dict[str, List[str]]],
    total_latency_ms: float,
    attempts: int,
    first_attempt_latency_ms: Optional[float],
) -> IdentityResult:
    """Pure function: one surface response → one IdentityResult (content-only)."""
    content = (response.get("content") or "").strip()
    reasoning = (response.get("reasoning") or "").strip()
    finish_reason = response.get("finish_reason")
    done_received = response.get("done_received", True)
    first_token_ms = response.get("first_token_latency_ms")

    base: Dict[str, Any] = dict(
        model_id=model_id,
        surface=surface,
        nonce=nonce,
        expected_family=expected_family,
        expected_vendor=expected_vendor,
        first_token_latency_ms=first_token_ms,
        total_latency_ms=total_latency_ms,
        content_len=len(content),
        reasoning_len=len(reasoning),
        finish_reason=finish_reason,
        attempts=attempts,
        first_attempt_latency_ms=first_attempt_latency_ms,
        raw_excerpt=content[:200],
    )

    def _extra(**kw: Any) -> Dict[str, Any]:
        return {"disclaimer": _DISCLAIMER, **kw}

    # ── Terminal transport-ish ───────────────────────────────────────────────
    if not done_received and not content:
        return IdentityResult(passed=False, error_type="stream_truncated", extra=_extra(), **base)
    if finish_reason == "content_filter":
        return IdentityResult(passed=False, error_type="content_filtered", extra=_extra(), **base)
    if finish_reason in _BAD_FINISH_REASONS:
        return IdentityResult(passed=False, error_type="bad_finish_reason", extra=_extra(), **base)
    if not content and not reasoning:
        return IdentityResult(passed=False, error_type="empty_response", extra=_extra(), **base)

    # ── Content empty, reasoning carries identity (parity with D3) ───────────
    if not content and reasoning:
        candidate_r, _, _ = _strip_fence(reasoning)
        parsed_r = _try_parse_json(candidate_r)
        carries = bool(
            parsed_r
            and isinstance(parsed_r.get("family"), str)
            and isinstance(parsed_r.get("vendor"), str)
        )
        if carries:
            return IdentityResult(
                passed=False,
                error_type="identity_in_reasoning_only",
                finding_severity="MAJOR",
                excluded_from_threshold=True,
                extra=_extra(
                    reasoning_excerpt=reasoning[:200],
                    reasoning_declared_family=parsed_r.get("family"),
                    reasoning_declared_vendor=parsed_r.get("vendor"),
                ),
                **base,
            )
        return IdentityResult(passed=False, error_type="empty_response", extra=_extra(), **base)

    # ── content non-empty ────────────────────────────────────────────────────
    candidate, fence_stripped, prose_with_fence = _strip_fence(content)
    base["fence_stripped"] = fence_stripped

    if prose_with_fence:
        return IdentityResult(
            passed=False, error_type="schema_invalid",
            extra=_extra(reason="prose surrounding a markdown fence"), **base,
        )

    parsed = _try_parse_json(candidate)
    if parsed is None:
        return IdentityResult(passed=False, error_type="json_parse_error", extra=_extra(), **base)

    # ── Required fields + strict types ───────────────────────────────────────
    if "echo" not in parsed:
        return IdentityResult(passed=False, error_type="echo_missing", extra=_extra(), **base)
    if not isinstance(parsed["echo"], str):
        return IdentityResult(passed=False, error_type="schema_invalid",
                              extra=_extra(reason="echo not a string"), **base)
    if "family" not in parsed:
        return IdentityResult(passed=False, error_type="family_missing", extra=_extra(), **base)
    if not isinstance(parsed["family"], str):
        return IdentityResult(passed=False, error_type="schema_invalid",
                              extra=_extra(reason="family not a string"), **base)
    if "vendor" not in parsed:
        return IdentityResult(passed=False, error_type="vendor_missing", extra=_extra(), **base)
    if not isinstance(parsed["vendor"], str):
        return IdentityResult(passed=False, error_type="schema_invalid",
                              extra=_extra(reason="vendor not a string"), **base)

    echo_val: str = parsed["echo"]
    family_val: str = parsed["family"]
    vendor_val: str = parsed["vendor"]
    cutoff_val = parsed.get("training_cutoff")

    if echo_val != nonce:
        return IdentityResult(passed=False, error_type="echo_mismatch",
                              extra=_extra(expected_echo=nonce, got_echo=echo_val[:80]), **base)
    base["echo_matched"] = True
    base["declared_family"] = family_val
    base["declared_vendor"] = vendor_val

    # ── training_cutoff: informative only (D4-DEC-07) ────────────────────────
    cutoff_extra: Dict[str, Any] = {}
    if isinstance(cutoff_val, str) and cutoff_val.strip():
        base["declared_cutoff"] = cutoff_val.strip()
        valid = bool(_CUTOFF_FMT_RE.match(cutoff_val.strip()))
        base["cutoff_format_valid"] = valid
        if not valid:
            cutoff_extra["cutoff_warning"] = "format_not_YYYY_or_YYYY-MM"

    # ── Refusal handling (Council C6 MAJOR-3) ────────────────────────────────
    family_refused = _is_refusal(family_val)
    vendor_refused = _is_refusal(vendor_val)
    if family_refused and vendor_refused:
        return IdentityResult(
            passed=False, error_type="identity_refused",
            finding_severity="MINOR", excluded_from_threshold=True,
            extra=_extra(family_refused=True, vendor_refused=True, **cutoff_extra), **base,
        )

    # ── Tolerant matcher via aliases (Council C6 BLOCKER-1 fix) ──────────────
    warn_extra: Dict[str, Any] = {}
    if not aliases:
        warn_extra["warn"] = "aliases_empty_strict_mode"  # Council C6 MAJOR-4
    family_aliases = (aliases.get("family", {}) or {}).get(expected_family.lower(), [expected_family])
    vendor_aliases = (aliases.get("vendor", {}) or {}).get(expected_vendor.lower(), [expected_vendor])

    # A refused field can never count as a match (closes the partial-refusal escape).
    family_ok = (not family_refused) and _matches_alias(family_val, expected_family, family_aliases)
    vendor_ok = (not vendor_refused) and _matches_alias(vendor_val, expected_vendor, vendor_aliases)
    base["family_matched"] = family_ok
    base["vendor_matched"] = vendor_ok

    merged_extra = _extra(**cutoff_extra, **warn_extra)

    if family_ok and vendor_ok:
        return IdentityResult(passed=True, error_type=None, extra=merged_extra, **base)

    if not family_ok and not vendor_ok:
        et = "identity_mismatch:both"
    elif not family_ok:
        et = "identity_mismatch:family"
    else:
        et = "identity_mismatch:vendor"
    return IdentityResult(passed=False, error_type=et, finding_severity="MAJOR",
                          extra=merged_extra, **base)


# ── Orchestrator ─────────────────────────────────────────────────────────────

def run_d4_identity(
    *,
    model_id: str,
    surface_client: SurfaceClient,
    catalog_entry: Dict[str, Any],
    aliases: Optional[Dict[str, Dict[str, List[str]]]] = None,
    quarantine_ids: Optional[set] = None,
    clock: Any = time.monotonic,
) -> IdentityResult:
    """Run D4 Identity on one (model, surface) pair.

    ``aliases`` is the catalog's top-level ``aliases:`` section, injected so the
    function stays pure and unit-testable. ``None``/empty falls back to strict
    canonical matching with an explicit warning (Council C6 MAJOR-4).
    """
    aliases = aliases or {}
    quarantine_ids = quarantine_ids or set()
    surface_name = getattr(surface_client, "name", "?")

    if model_id in quarantine_ids:
        return IdentityResult(model_id=model_id, surface=surface_name, passed=False,
                              error_type="skipped:quarantined", excluded_from_threshold=True)

    valid_surfaces = catalog_entry.get("surfaces") or []
    if valid_surfaces and surface_name not in valid_surfaces:
        return IdentityResult(model_id=model_id, surface=surface_name, passed=False,
                              error_type="skipped:surface_unsupported", excluded_from_threshold=True)

    expected_family = (catalog_entry.get("expected_family") or "").strip()
    expected_vendor = (catalog_entry.get("expected_vendor") or "").strip()
    if not expected_family or not expected_vendor:
        return IdentityResult(
            model_id=model_id, surface=surface_name, passed=False,
            error_type="skipped:no_ground_truth", excluded_from_threshold=True,
            expected_family=expected_family or None, expected_vendor=expected_vendor or None,
        )

    base_timeout = float(catalog_entry.get("timeout_s") or 60)
    timeout = base_timeout * SURFACE_TIMEOUT_MULTIPLIER.get(surface_name, 1.0)
    is_thinking = bool(catalog_entry.get("is_thinking", False))
    max_tokens = 2048 if is_thinking else 256

    first_attempt_latency_ms: Optional[float] = None
    last_exception: Optional[Exception] = None
    last_result: Optional[IdentityResult] = None

    for attempt in (1, 2):
        nonce = secrets.token_hex(8).upper()
        prompt = _build_prompt(nonce)
        t0 = clock()
        try:
            raw = surface_client.chat(
                model=model_id, messages=prompt, stream=True,
                timeout=timeout, max_tokens=max_tokens, temperature=0.0,
            )
        except TimeoutError:
            latency = (clock() - t0) * 1000.0
            if attempt == 1:
                first_attempt_latency_ms = latency
                last_exception = TimeoutError("timeout")
                continue
            return IdentityResult(
                model_id=model_id, surface=surface_name, passed=False, error_type="timeout",
                expected_family=expected_family, expected_vendor=expected_vendor, nonce=nonce,
                total_latency_ms=latency, attempts=attempt,
                first_attempt_latency_ms=first_attempt_latency_ms, extra={"disclaimer": _DISCLAIMER},
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
            return IdentityResult(
                model_id=model_id, surface=surface_name, passed=False, error_type=err_type,
                expected_family=expected_family, expected_vendor=expected_vendor, nonce=nonce,
                total_latency_ms=latency, attempts=attempt,
                first_attempt_latency_ms=first_attempt_latency_ms, extra={"disclaimer": _DISCLAIMER},
            )

        total_latency_ms = (clock() - t0) * 1000.0
        last_result = _verdict_from_response(
            model_id=model_id, surface=surface_name, nonce=nonce, response=raw,
            expected_family=expected_family, expected_vendor=expected_vendor, aliases=aliases,
            total_latency_ms=total_latency_ms, attempts=attempt,
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
    return IdentityResult(
        model_id=model_id, surface=surface_name, passed=False,
        error_type=("timeout" if isinstance(last_exception, TimeoutError)
                    else f"http_error:{getattr(last_exception, 'status_code', 'unknown')}"),
        expected_family=expected_family, expected_vendor=expected_vendor,
        attempts=2, first_attempt_latency_ms=first_attempt_latency_ms,
        extra={"disclaimer": _DISCLAIMER},
    )
