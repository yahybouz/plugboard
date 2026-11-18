"""Secret/PII redaction for log messages.

Threat model TM-001 identified token leakage via subprocess stderr logging
as a HIGH-severity risk. This module provides a single redact() function
that masks tokens and common secret patterns before they hit the JSON
formatter.

Covered patterns:
- OpenAI / ChatGPT / Codex API keys: sk-*, sk-proj-*, sk-ant-*
- GitHub OAuth tokens: gho_*, ghp_*, ghs_*, github_pat_*
- Google OAuth access tokens: ya29.*
- Slack tokens: xoxb-*, xoxp-*
- AWS access keys: AKIA* (20 chars)
- Generic Bearer headers: "Authorization: Bearer <token>"
- API keys in JSON: "api_key": "<value>", "key": "<value>"
- Google API keys: AIza* (BUG-143)
- Secret headers, header, JSON or ("name", "value") form, any case:
  x-api-key, x-goog-api-key, api-key, Authorization (Bearer, Basic, bare
  value) (BUG-143)
- Query and form parameters: token=, access_token=, api_key=, key=…, their
  quoted form (token="…", key='…') and environment variables
  (ANTHROPIC_API_KEY=, GITHUB_TOKEN=, *_SECRET=, *_PASSWORD=) (BUG-143)
- Every pattern runs in time near linear in the input (no nested or
  adjacent unbounded repeats that can backtrack): redact() runs on every
  log line
- UUIDs are NOT redacted (too noisy; they're rarely secret)
- Email addresses are NOT redacted (needed for audit trails)

Usage:
    from plugboard.log_redact import redact
    logger.warning("CLI failed: %s", redact(stderr_text))
"""

from __future__ import annotations

import re
from typing import Any

# Each pattern: (name, regex, replacement template)
# Keep the prefix visible (first 4-8 chars) so operators can tell providers
# apart without leaking the secret.
_PATTERNS = [
    # OpenAI family — sk-, sk-proj-, sk-ant-, sk-or-, etc.
    (
        "openai",
        re.compile(r"\b(sk-(?:proj-|ant-|or-|svcacct-)?)[A-Za-z0-9_-]{20,}"),
        r"\1***REDACTED***",
    ),
    # GitHub tokens — PAT classic (ghp_), fine-grained (github_pat_),
    # OAuth user (gho_), app (ghs_), refresh (ghr_), session (ghu_).
    (
        "github",
        re.compile(r"\b(gh[opusr]_|github_pat_)[A-Za-z0-9_]{20,}"),
        r"\1***REDACTED***",
    ),
    # Google OAuth access tokens (ya29. prefix, long base64url-ish payload).
    (
        "google_oauth",
        re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"),
        "ya29.***REDACTED***",
    ),
    # Google refresh tokens (1// prefix, common in oauth_creds.json dumps).
    (
        "google_refresh",
        re.compile(r"\b1//[A-Za-z0-9_-]{40,}"),
        "1//***REDACTED***",
    ),
    # Slack tokens.
    (
        "slack",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
        r"xox*-***REDACTED***",
    ),
    # AWS access key IDs.
    (
        "aws",
        re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
        r"\1***REDACTED***",
    ),
    # Generic Bearer header (case-insensitive), keeps "Bearer " prefix.
    # Matches both HTTP header form (Authorization: Bearer xyz) and JSON form
    # ("Authorization": "Bearer xyz") — the intermediate quotes + colon are
    # captured as part of the non-token delimiter class.
    (
        "bearer_header",
        re.compile(r"(?i)(authorization[\"'\s:]+bearer\s+)[a-z0-9._\-~+/=]{16,}"),
        r"\1***REDACTED***",
    ),
    # JSON "api_key" / "apiKey" / "access_token" fields.
    (
        "json_secret",
        re.compile(
            r"""(["'](?:api_?key|access_?token|refresh_?token|bearer_?token|"""
            r"""client_?secret|password|token)["']\s*:\s*["'])"""
            r"""[^"']{8,}(["'])""",
            re.IGNORECASE,
        ),
        r"\1***REDACTED***\2",
    ),
    # Anthropic claude-ant prefix (covered by openai sk-ant- too but belt & braces).
    (
        "anthropic",
        re.compile(r"\bsk-ant-api\d+-[A-Za-z0-9_-]{20,}"),
        "sk-ant-***REDACTED***",
    ),
    # Plugboard client key format (avoid logging your own dispatched key).
    (
        "plugboard",
        re.compile(r"\blle_[A-Za-z0-9]{32,}"),
        "lle_***REDACTED***",
    ),
    (
        "plugboard",
        re.compile(r"\bpb_[A-Za-z0-9]{32,}"),
        "pb_***REDACTED***",
    ),
    # Personal dashboard keys (PB-AUTH-01): "pba_" + token_urlsafe(32). No
    # \b: a key glued to a word character ("id_pba_…") or behind a separator
    # encoded in a URL ("%20pba_…", "%3Dpba_…") is masked too, and so is a
    # key cut short from 8 characters on. The 10-character display prefix
    # ("pba_" + 6) stays readable. Possessive: no backtracking.
    (
        "plugboard",
        re.compile(r"pba_[A-Za-z0-9_-]{8,}+"),
        "pba_***REDACTED***",
    ),
    # BUG-143: Anthropic keys anywhere, even glued to a word ("id_sk-ant-…",
    # where the \b of the "openai" pattern fails) or cut short upstream.
    (
        "anthropic_any",
        re.compile(r"sk-ant-[A-Za-z0-9_-]{4,}"),
        "sk-ant-***REDACTED***",
    ),
    # BUG-143: Google API keys (AIza + 35 characters), cut ones included.
    (
        "google_api_key",
        re.compile(r"AIza[0-9A-Za-z_-]{6,}"),
        "AIza***REDACTED***",
    ),
    # BUG-143: secret headers, as a header line, a JSON field (escaped JSON
    # included) or a ("name", "value") pair as httpx prints a header list,
    # any case: x-api-key, x-goog-api-key, api-key and Authorization
    # (Bearer, Basic, Token, Digest or a bare value). The scheme stays
    # visible; a value already masked above is left alone. Every run of
    # blanks is possessive (\s*+): with plain \s* on both sides of the
    # separator, "authorization:" followed by n blanks took O(n²) time, and
    # this runs on every log line.
    (
        "secret_header",
        re.compile(
            r"(?i)(\b(?:x-api-key|x-goog-api-key|api-key|proxy-authorization"
            r"""|authorization)(?:\\?["']?\s*+[:=]|\\?["']\s*+,)"""
            r"""\s*+\\?["']?\s*+)"""
            r"(?!(?:(?:bearer|basic|token|digest)\s++)?[\w.-]*\*\*\*REDACTED)"
            r"((?:bearer|basic|token|digest)\s++)?"
            r"""[^\s"'\\,;&}]+"""
        ),
        lambda m: f"{m[1]}{m[2] or ''}***REDACTED***",
    ),
    # BUG-143: secrets after "=": a query string or a form body (token=,
    # access_token=, api_key=, key=, client_secret=…), a Python repr or
    # kwargs with a quoted value (token="…", key='…', escaped quotes too) and
    # an environment variable (ANTHROPIC_API_KEY=, GITHUB_TOKEN=, *_SECRET=,
    # *_PASSWORD=). The name starts a word, so monkey=, sort_key= and
    # max_tokens= stay. The quotes stay; a value already masked is left
    # alone. A quoted value runs to its closing quote, or to the end of the
    # line when it was cut.
    (
        "secret_param",
        re.compile(
            r"(?i)(?<![\w-])("
            r"\w*_(?:api_?key|access_?key|private_?key|token|secret|password|passwd)"
            r"|token|api[_-]?key|key|secret|password)="
            r"""(?!\\?["']?[\w.-]*\*\*\*REDACTED)"""
            r"""(?:(\\?")[^"\\\n]++(\\?")?|(\\?')[^'\\\n]++(\\?')?"""
            r"""|[^\s&#"'\\,;<>]+)"""
        ),
        lambda m: f"{m[1]}={m[2] or m[4] or ''}***REDACTED***{m[3] or m[5] or ''}",
    ),
]


def redact(value: Any, max_len: int = 4096) -> str:
    """Redact known secret patterns from a string (or stringify anything).

    Args:
        value: anything; will be coerced to str via `str(value)`.
        max_len: cap output length to avoid log explosion from huge payloads.

    Returns:
        A redacted string safe to emit in JSON logs.
    """
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    if not s:
        return s
    for _name, pattern, replacement in _PATTERNS:
        s = pattern.sub(replacement, s)
    if len(s) > max_len:
        s = s[:max_len] + f"...[truncated {len(s) - max_len} chars]"
    return s


class RedactingFilter:
    """logging.Filter that redacts the final rendered message in-place.

    Installed on the root handler via setup_logging() so every log line
    from every logger (including third-party libraries) is scrubbed before
    it hits the JSON formatter.
    """

    def filter(self, record) -> bool:  # noqa: D401 — stdlib signature
        # Walk args first — we want the format-substituted result to be
        # redacted, not just the static template.
        try:
            rendered = record.getMessage()
            redacted = redact(rendered)
            if redacted != rendered:
                record.msg = redacted
                record.args = None  # already substituted
        except Exception:
            # Never crash logging; prefer leaving the original line.
            pass
        return True
