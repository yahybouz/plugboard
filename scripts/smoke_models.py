#!/usr/bin/env python3
"""Per-model identification smoke test + quarantine + alias detection.

For every model returned by ``/v1/models``, send an identification prompt
and classify the result:

  * ``ok``        — the model answered, and the answer mentions a family
                    name compatible with its catalog ID. Quarantine
                    counter reset.
  * ``mismatch``  — the model answered but did not mention its expected
                    family. Logged as a warning, *not* counted as a
                    failure (LLMs are notoriously vague about their own
                    identity, e.g. "I am Claude, made by Anthropic" with
                    no version).
  * ``fail``      — HTTP 4xx/5xx, timeout, or empty completion. Retried
                    on transient errors (429/5xx/timeout), then the
                    quarantine counter is bumped. After
                    ``QUARANTINE_THRESHOLD`` consecutive nightly fails,
                    the model is filtered out of ``/v1/models``.
  * ``alias``     — two distinct catalog IDs returned the exact same
                    identification answer → flagged as duplicates so the
                    operator can prune one. Not auto-removed (too risky
                    without a human in the loop).

Usage:
  .venv/bin/python scripts/smoke_models.py
  .venv/bin/python scripts/smoke_models.py --only claude
  .venv/bin/python scripts/smoke_models.py --timeout 30 --json
  .venv/bin/python scripts/smoke_models.py --dry-run   # no quarantine writes

Environment:
  PLUGBOARD_URL  base URL (default: http://localhost:8765)
  API_KEY          bearer token for /v1 calls
  ADMIN_KEY        bearer token for /v1/models (admin-gated on some builds)

Costs are capped by ``max_tokens=64`` (enough for a model to say its
name and version, no more). A full nightly across ~150 models stays
well under $1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# After .env is loaded: accept names from before the Plugboard rename.
_OLD = "LLM_" + "ENABLER_"
for _key in list(os.environ):
    if _key.startswith(_OLD):
        os.environ.setdefault("PLUGBOARD_" + _key[len(_OLD) :], os.environ[_key])


BASE_URL = os.environ.get("PLUGBOARD_URL", "http://localhost:8765").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", API_KEY)

# Prompt designed to elicit a structured "I am X" reply. ``max_tokens=64``
# is enough for "I am Claude Sonnet 4.6, an AI assistant by Anthropic."
# but short enough to keep the run cheap.
_IDENT_PROMPT = (
    "In one short sentence: what is your exact model identifier? "
    "Reply with just the model name and version (e.g. 'claude-opus-4-7' "
    "or 'GPT-5 Turbo'). No greeting, no caveats."
)

# Transient HTTP failures we retry. 408/429/5xx are upstream-side; the
# others usually surface a permanent client-side problem (auth, bad
# model name, deprecation) and retrying just burns budget.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_RETRYABLE_KEYWORDS = ("timeout", "timed out", "overloaded", "rate limit")
_DEPRECATION_KEYWORDS = (
    "model_not_found",
    "does not exist",
    "is not available",
    "has been deprecated",
    "model is retired",
    "no longer supported",
)


def _http_json(
    method: str,
    path: str,
    *,
    token: str,
    body: Dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_models(only: str | None) -> List[Dict[str, Any]]:
    try:
        data = _http_json("GET", "/v1/models", token=ADMIN_KEY)
    except HTTPError as exc:
        print(f"failed to list models: HTTP {exc.code}: {exc.read().decode()[:200]}")
        sys.exit(2)
    except URLError as exc:
        print(f"failed to list models: {exc}")
        sys.exit(2)
    models = data.get("data", []) or []
    if only:
        models = [m for m in models if only in m.get("id", "")]
    return models


def _suffix(model_id: str) -> str:
    """Strip the ``provider_id:`` prefix → returns the bare model name
    used by quarantine and dedup logic."""
    return model_id.split(":", 1)[-1] if ":" in model_id else model_id


# ── Identification analysis ─────────────────────────────────────────────────


def _normalize(s: str) -> str:
    """Lowercase + drop non-alphanumeric so 'GPT-4o' matches 'gpt 4o'."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _family_tokens(suffix: str) -> List[str]:
    """Extract the discriminating tokens from a catalog ID.

    Examples:
      claude-opus-4-7        → ['claude', 'opus', '4', '7']
      claude-opus-4-7-20260416 → ['claude', 'opus', '4', '7']  (date dropped)
      gpt-5-mini             → ['gpt', '5', 'mini']
      gemini-2.5-pro         → ['gemini', '2', '5', 'pro']
      llama3.1:70b           → ['llama3', '1', '70b']
    """
    # Drop the dated suffix YYYYMMDD before tokenising
    base = re.sub(r"-20\d{6}$", "", suffix)
    # Split on the usual separators
    parts = re.split(r"[-:./_ ]+", base)
    return [p for p in parts if p]


def _classify_response(suffix: str, reply: str) -> str:
    """Return ``ok`` if the reply mentions enough of the model family to
    plausibly be the right model, ``mismatch`` otherwise.

    Heuristic: at least the family name (first non-numeric token) must
    appear, normalized, in the reply. ``claude-opus-4-7`` needs at least
    "claude" or "opus" to be present.
    """
    if not reply:
        return "mismatch"
    norm_reply = _normalize(reply)
    tokens = _family_tokens(suffix)
    if not tokens:
        return "mismatch"
    # Skip purely numeric tokens for the match — "4" or "7" alone is
    # not strong evidence of identity.
    informative = [t for t in tokens if not t.isdigit()][:2]
    if not informative:
        informative = tokens[:1]
    for tok in informative:
        if _normalize(tok) in norm_reply:
            return "ok"
    return "mismatch"


# ── Single-model probe with retry ───────────────────────────────────────────


def _one_call(model_id: str, *, timeout: float) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        resp = _http_json(
            "POST",
            "/v1/chat/completions",
            token=API_KEY,
            body={
                "model": model_id,
                "messages": [{"role": "user", "content": _IDENT_PROMPT}],
                "max_tokens": 64,
                "temperature": 0,
            },
            timeout=timeout,
        )
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:200]
        except Exception:  # noqa: BLE001
            pass
        retryable = exc.code in _RETRYABLE_STATUS or any(
            kw in body.lower() for kw in _RETRYABLE_KEYWORDS
        )
        deprecated = any(kw in body.lower() for kw in _DEPRECATION_KEYWORDS)
        return {
            "outcome": "fail",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": f"HTTP {exc.code}: {body}",
            "retryable": retryable,
            "deprecated": deprecated,
            "http_status": exc.code,
        }
    except URLError as exc:
        return {
            "outcome": "fail",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": str(exc)[:200],
            "retryable": any(kw in str(exc).lower() for kw in _RETRYABLE_KEYWORDS),
            "deprecated": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "outcome": "fail",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "retryable": False,
            "deprecated": False,
        }
    latency_ms = int((time.perf_counter() - started) * 1000)
    usage = resp.get("usage") or {}
    content = ""
    try:
        content = resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        content = ""
    reply = content.strip()
    if not reply:
        return {
            "outcome": "fail",
            "latency_ms": latency_ms,
            "error": "empty completion",
            "retryable": False,
            "deprecated": False,
        }
    return {
        "outcome": "ok",
        "latency_ms": latency_ms,
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
        "reply": reply,
    }


def _probe(model_id: str, *, timeout: float, max_retries: int) -> Dict[str, Any]:
    """Call ``_one_call`` with retries on transient failures. Returns
    the same shape as ``_one_call`` plus an ``attempts`` field."""
    last: Dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        last = _one_call(model_id, timeout=timeout)
        last["attempts"] = attempt + 1
        if last["outcome"] == "ok":
            return last
        if not last.get("retryable"):
            return last
        # exponential backoff: 1s, 2s, 4s, capped at 8s
        time.sleep(min(2**attempt, 8))
    return last


# ── Main loop ───────────────────────────────────────────────────────────────


def _format_row(row: Dict[str, Any]) -> str:
    badge = {
        "ok": "ok      ",
        "mismatch": "MISMATCH",
        "fail": "FAIL    ",
        "alias": "ALIAS   ",
    }.get(row["status"], "?       ")
    left = f"[{badge}] {row['model']:<70}"
    parts = [left, f"{row['latency_ms']:>6} ms"]
    if row["status"] in ("ok", "mismatch"):
        parts.append(
            f"in={row.get('tokens_in', 0):<4} out={row.get('tokens_out', 0):<4}"
        )
        parts.append(f"→ {row.get('reply', '')!r}")
    elif row["status"] == "alias":
        parts.append(f"→ alias of {row.get('alias_of')!r}")
    else:
        parts.append(f"({row.get('attempts', 1)} tries)")
        parts.append(row.get("error", "")[:120])
    if row.get("quarantine_note"):
        parts.append(f"⚠ {row['quarantine_note']}")
    return "  ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        help="Substring filter on the model ID (e.g. 'claude', 'ollama').",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-call request timeout in seconds (default 60).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="Retries on transient failures (default 2 → up to 3 attempts).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the final summary as JSON instead of a text table.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe everything but don't update the quarantine store.",
    )
    args = parser.parse_args()

    if not API_KEY:
        print("API_KEY missing — set it in .env or environment.", file=sys.stderr)
        return 2

    models = _fetch_models(args.only)
    if not models:
        print("no models returned by /v1/models", file=sys.stderr)
        return 2

    # Import lazily so the script remains importable without the project
    # virtualenv being active (CI uses .venv/bin/python explicitly).
    if not args.dry_run:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from plugboard.model_quarantine import (  # noqa: E402
            QUARANTINE_THRESHOLD,
            is_account_unsupported_error,
            record_account_unsupported,
            record_failure,
            record_success,
        )
    else:
        QUARANTINE_THRESHOLD = 3  # noqa: N806

        def is_account_unsupported_error(*_a, **_k):  # type: ignore[no-redef]
            return False

        def record_account_unsupported(*_a, **_k):  # type: ignore[no-redef]
            return {"consecutive_fails": 0}

        def record_failure(*_a, **_k):  # type: ignore[no-redef]
            return {"consecutive_fails": 0}

        def record_success(*_a, **_k):  # type: ignore[no-redef]
            return None

    print(f"→ identifying {len(models)} models via {BASE_URL}\n")
    results: List[Dict[str, Any]] = []
    reply_to_first_suffix: Dict[str, str] = {}

    for m in models:
        model_id = m.get("id", "")
        if not model_id:
            continue
        suffix = _suffix(model_id)
        probe = _probe(model_id, timeout=args.timeout, max_retries=args.max_retries)
        row: Dict[str, Any] = {
            "model": model_id,
            "suffix": suffix,
            "attempts": probe.get("attempts", 1),
            "latency_ms": probe["latency_ms"],
        }
        if probe["outcome"] == "ok":
            row["tokens_in"] = probe.get("tokens_in", 0)
            row["tokens_out"] = probe.get("tokens_out", 0)
            row["reply"] = probe.get("reply", "")
            # Identification analysis
            verdict = _classify_response(suffix, row["reply"])
            row["status"] = verdict
            if verdict == "ok":
                record_success(suffix)
                row["quarantine_note"] = ""
            else:
                # mismatch: log only, do NOT bump quarantine.
                row["quarantine_note"] = "self-id doesn't match catalog ID"
            # Alias detection: same normalized reply seen before → mark
            # this one as alias of the first.
            key = _normalize(row["reply"])
            if key:
                if (
                    key in reply_to_first_suffix
                    and reply_to_first_suffix[key] != suffix
                ):
                    row["status"] = "alias"
                    row["alias_of"] = reply_to_first_suffix[key]
                else:
                    reply_to_first_suffix[key] = suffix
        else:
            row["status"] = "fail"
            row["error"] = probe.get("error", "")
            row["http_status"] = probe.get("http_status")
            if is_account_unsupported_error(probe.get("error", "")):
                entry = record_account_unsupported(
                    suffix, reason=probe.get("error", "")[:200]
                )
                fails = int(entry.get("consecutive_fails", 0))
                row["quarantine_note"] = (
                    "account unsupported → quarantined"
                    if fails >= QUARANTINE_THRESHOLD
                    else f"{fails}/{QUARANTINE_THRESHOLD} fails"
                )
            elif probe.get("deprecated"):
                # Deprecation signals are strong — fast-track to quarantine
                # by bumping the counter QUARANTINE_THRESHOLD times. Saves
                # 2 nightlies for an obviously-dead model.
                for _ in range(QUARANTINE_THRESHOLD):
                    entry = record_failure(suffix, reason=probe.get("error", "")[:200])
                row["quarantine_note"] = "deprecated → quarantined"
            else:
                entry = record_failure(suffix, reason=probe.get("error", "")[:200])
                fails = int(entry.get("consecutive_fails", 0))
                if fails >= QUARANTINE_THRESHOLD:
                    row["quarantine_note"] = f"quarantined ({fails} fails)"
                else:
                    row["quarantine_note"] = f"{fails}/{QUARANTINE_THRESHOLD} fails"
        results.append(row)
        if not args.json:
            print(_format_row(row))

    ok = sum(1 for r in results if r["status"] == "ok")
    mismatch = sum(1 for r in results if r["status"] == "mismatch")
    alias = sum(1 for r in results if r["status"] == "alias")
    fail = sum(1 for r in results if r["status"] == "fail")
    quarantined = sum(
        1
        for r in results
        if r["status"] == "fail" and "quarantined" in r.get("quarantine_note", "")
    )

    summary: Dict[str, Any] = {
        "total": len(results),
        "ok": ok,
        "mismatch": mismatch,
        "alias": alias,
        "fail": fail,
        "quarantined_this_run": quarantined,
        "results": results,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(
            f"\nsummary: {ok} ok / {mismatch} mismatch / {alias} alias / "
            f"{fail} fail ({quarantined} newly quarantined) / "
            f"{len(results)} total"
        )

    # Exit non-zero only on hard fails — mismatch and alias are advisory.
    return 0 if fail == 0 else 1


def _smoke_one_for_tests(model_id: str, reply: str = "") -> Tuple[str, Optional[str]]:
    """Test seam: classify a fake reply for a given model ID.
    Returns (status, suffix). Used by unit tests so they don't have to
    boot the proxy."""
    suffix = _suffix(model_id)
    return _classify_response(suffix, reply), suffix


if __name__ == "__main__":
    sys.exit(main())
