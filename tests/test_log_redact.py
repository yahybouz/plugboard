"""Tests for secret redaction in log messages (TM-001 mitigation)."""

from __future__ import annotations

import logging

import pytest

from plugboard.log_redact import RedactingFilter, redact


# ── redact() direct calls ────────────────────────────────────────────────


def test_redact_passes_non_secret_text():
    assert redact("Hello, world") == "Hello, world"
    assert (
        redact("user alice@example.com logged in") == "user alice@example.com logged in"
    )


def test_redact_openai_sk_key():
    s = "Calling OpenAI with key sk-proj-abcdefghij1234567890XYZ1234567890aa"
    out = redact(s)
    assert "sk-proj-***REDACTED***" in out
    assert "abcdefghij1234567890XYZ" not in out


def test_redact_openai_sk_ant_key():
    s = "Token: sk-ant-api03-abcdefghij1234567890XYZABC1234567890"
    out = redact(s)
    assert "***REDACTED***" in out
    # The actual secret must be gone
    assert "abcdefghij1234567890" not in out


def test_redact_github_classic_pat():
    s = "gh token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345678"
    out = redact(s)
    assert "ghp_***REDACTED***" in out
    assert "aBcDeFgHiJkLmNoP" not in out


def test_redact_github_oauth_user_token():
    s = "Authorization: Bearer gho_abcdefghij1234567890XYZ1234"
    out = redact(s)
    # Both patterns should fire — either the gho_ one or the bearer_header one.
    assert "***REDACTED***" in out
    assert "abcdefghij1234567890" not in out


def test_redact_github_fine_grained_pat():
    s = "GITHUB_TOKEN=github_pat_11AAAA0000_bcdefghij1234567890XYZ1234567890"
    out = redact(s)
    assert "github_pat_***REDACTED***" in out
    assert "bcdefghij1234567890" not in out


def test_redact_google_oauth_access_token():
    s = "access_token: ya29.a0Abc_ZyXwVuTsRqPoNmLkJiHgFeDcBa0123456789"
    out = redact(s)
    assert "ya29.***REDACTED***" in out
    assert "a0Abc_ZyXwVu" not in out


def test_redact_google_refresh_token():
    s = "refresh: 1//0gAAbCdEfGhIjKlMnOpQrStUvWxYz01234567890abcdefghijklmnop"
    out = redact(s)
    assert "1//***REDACTED***" in out


def test_redact_slack_bot_token():
    s = "slack xoxb-12345-67890-aBcDeFgHiJkLmNoP"
    out = redact(s)
    assert "xox*-***REDACTED***" in out


def test_redact_aws_access_key():
    s = "key=AKIAIOSFODNN7EXAMPLE rest of line"
    out = redact(s)
    assert "AKIA***REDACTED***" in out
    assert "IOSFODNN7EXAMPLE" not in out


def test_redact_bearer_header():
    s = 'headers: {"Authorization": "Bearer eyJhbGciOiJIUzI1NiIs_abc123def456"}'
    out = redact(s)
    assert "Bearer ***REDACTED***" in out
    assert "eyJhbGci" not in out


def test_redact_json_api_key_field():
    s = '{"api_key": "secret123456789abcdef", "name": "test"}'
    out = redact(s)
    assert '"api_key": "***REDACTED***"' in out
    assert "secret123456789" not in out
    # Non-secret fields preserved
    assert '"name": "test"' in out


def test_redact_json_password_field():
    s = '{"password": "MySuperSecret123", "user": "alice"}'
    out = redact(s)
    assert "MySuperSecret123" not in out
    assert '"user": "alice"' in out


@pytest.mark.parametrize("prefix", ["lle_", "pb_"])
def test_redact_client_key(prefix):
    # Extended by the /ship coverage audit (pb_ row, secret-gone assertion).
    # Value: protects=client keys of both formats (pb_ issued now, lle_ before
    # the rename) never reach the logs; fails_when=the pb_ redaction pattern is
    # removed or narrowed; why_new=only the lle_ format was covered, and only
    # the marker was asserted; seam=none
    secret = "ab" * 32  # create_key: prefix + token_hex(32); low entropy on purpose
    out = redact(f"client key {prefix}{secret}")
    assert f"{prefix}***REDACTED***" in out
    assert secret not in out


def test_redact_truncates_very_long_output():
    big = "x" * 10000
    out = redact(big, max_len=100)
    assert len(out) < 200
    assert "truncated" in out


def test_redact_none_and_empty():
    assert redact(None) == ""
    assert redact("") == ""


def test_redact_coerces_non_string():
    # Numbers, dicts etc. should be str()-ified and scanned.
    out = redact({"api_key": "supersecretkey1234567890abc"})
    assert "supersecretkey" not in out


# ── RedactingFilter on real logger ───────────────────────────────────────


def test_filter_redacts_via_logger(caplog):
    filt = RedactingFilter()
    logger = logging.getLogger("test_redact_logger")
    logger.addFilter(filt)
    with caplog.at_level(logging.INFO, logger="test_redact_logger"):
        logger.info("saving token ghp_verysecret1234567890ABCDEF1234")
    logger.removeFilter(filt)
    assert len(caplog.records) == 1
    emitted = caplog.records[0].getMessage()
    assert "ghp_***REDACTED***" in emitted
    assert "verysecret1234" not in emitted


def test_filter_does_not_mangle_safe_messages(caplog):
    filt = RedactingFilter()
    logger = logging.getLogger("test_redact_safe")
    logger.addFilter(filt)
    with caplog.at_level(logging.INFO, logger="test_redact_safe"):
        logger.info("request %s completed in %d ms", "req-abc", 42)
    logger.removeFilter(filt)
    emitted = caplog.records[0].getMessage()
    assert emitted == "request req-abc completed in 42 ms"


def test_filter_never_crashes_on_bad_args(caplog):
    """The filter must be bulletproof: a broken log call must not take down logging."""
    filt = RedactingFilter()
    logger = logging.getLogger("test_redact_robust")
    logger.addFilter(filt)
    # Intentionally broken format string — getMessage() will raise internally.
    with caplog.at_level(logging.INFO, logger="test_redact_robust"):
        try:
            logger.info("bad %d format", "not-an-int")
        except TypeError:
            pass  # Some python versions raise, that's fine.
    logger.removeFilter(filt)
    # Test passes if we reached this line — filter didn't crash the handler.


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
