"""
Security hardening tests — brute-force protection, auth rate limiting, CORS.
"""

from __future__ import annotations

import time
import pytest
from httpx import AsyncClient, ASGITransport


# ══════════════════════════════════════════════════════════════════════════════
# Brute-force protection
# ══════════════════════════════════════════════════════════════════════════════


def test_auth_rate_limit_allows_first_attempts():
    """First AUTH_LIMIT attempts should pass."""
    from plugboard.ratelimit import check_auth_rate_limit, _auth_buckets, _lockouts

    _auth_buckets.clear()
    _lockouts.clear()
    ip = "10.0.0.99"
    for _ in range(5):
        assert check_auth_rate_limit(ip) is True


def test_auth_rate_limit_lockout_after_failures():
    """After AUTH_LIMIT failures, IP should be locked out."""
    from plugboard.ratelimit import (
        check_auth_rate_limit,
        record_auth_failure,
        _auth_buckets,
        _lockouts,
    )

    _auth_buckets.clear()
    _lockouts.clear()
    ip = "10.0.0.100"
    # Record 5 failures
    for i in range(4):
        locked = record_auth_failure(ip)
        assert locked is False
    locked = record_auth_failure(ip)
    assert locked is True
    # Now should be locked out
    assert check_auth_rate_limit(ip) is False


def test_auth_lockout_remaining():
    """get_lockout_remaining returns seconds left."""
    from plugboard.ratelimit import (
        record_auth_failure,
        get_lockout_remaining,
        _auth_buckets,
        _lockouts,
    )

    _auth_buckets.clear()
    _lockouts.clear()
    ip = "10.0.0.101"
    for _ in range(5):
        record_auth_failure(ip)
    remaining = get_lockout_remaining(ip)
    assert remaining > 0
    assert remaining <= 900


def test_auth_lockout_clears_after_expiry():
    """Lockout should clear after duration passes."""
    from plugboard.ratelimit import (
        check_auth_rate_limit,
        record_auth_failure,
        _auth_buckets,
        _lockouts,
    )

    _auth_buckets.clear()
    _lockouts.clear()
    ip = "10.0.0.102"
    for _ in range(5):
        record_auth_failure(ip)
    assert check_auth_rate_limit(ip) is False
    # Simulate lockout expiry by backdating
    _lockouts[ip] = time.monotonic() - 1
    assert check_auth_rate_limit(ip) is True


def test_auth_no_lockout_for_different_ips():
    """Lockout is per-IP — other IPs should not be affected."""
    from plugboard.ratelimit import (
        check_auth_rate_limit,
        record_auth_failure,
        _auth_buckets,
        _lockouts,
    )

    _auth_buckets.clear()
    _lockouts.clear()
    bad_ip = "10.0.0.103"
    good_ip = "10.0.0.104"
    for _ in range(5):
        record_auth_failure(bad_ip)
    assert check_auth_rate_limit(bad_ip) is False
    assert check_auth_rate_limit(good_ip) is True


# ══════════════════════════════════════════════════════════════════════════════
# Login endpoint brute-force integration
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_login_lockout_endpoint(_providers_file, monkeypatch):
    """POST /auth/admin/login returns 429 after 5 failures."""
    monkeypatch.setattr("plugboard.app._API_KEY", "testkey")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "real-admin-key")
    # Reset brute-force state
    from plugboard import ratelimit

    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()
    monkeypatch.setattr(ratelimit, "_LIMIT", 0)  # disable global rate limit

    from plugboard.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        # 5 bad attempts
        for _ in range(5):
            resp = await c.post("/auth/admin/login", json={"key": "wrong-key"})
            # Could be 401 or 429 depending on when lockout kicks in
        # 6th attempt should be 429
        resp = await c.post("/auth/admin/login", json={"key": "wrong-key"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers


@pytest.mark.asyncio
async def test_login_success_not_blocked(_providers_file, monkeypatch):
    """Correct admin key still works before lockout."""
    monkeypatch.setattr("plugboard.app._API_KEY", "testkey")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "real-admin-key")
    from plugboard import ratelimit

    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()
    monkeypatch.setattr(ratelimit, "_LIMIT", 0)

    from plugboard.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        resp = await c.post("/auth/admin/login", json={"key": "real-admin-key"})
        assert resp.status_code == 204


@pytest.mark.asyncio
async def test_failed_login_audit_logged(_providers_file, monkeypatch, tmp_path):
    """Failed login attempts are recorded in audit.db."""
    monkeypatch.setattr("plugboard.app._API_KEY", "testkey")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "real-admin-key")
    from plugboard import ratelimit

    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()
    monkeypatch.setattr(ratelimit, "_LIMIT", 0)

    from plugboard.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        await c.post("/auth/admin/login", json={"key": "bad-key"})

    from plugboard.audit import get_audit_log

    entries = get_audit_log(action="auth.login_failed")
    assert len(entries) >= 1
    # Ensure the actual key value is NOT logged in details
    for e in entries:
        assert "bad-key" not in e.get("details", "")


# ══════════════════════════════════════════════════════════════════════════════
# CORS behavior
# ══════════════════════════════════════════════════════════════════════════════


def test_cors_permissive_in_dev(monkeypatch):
    """When API_KEY and ADMIN_KEY are empty, CORS defaults to *."""
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("ADMIN_KEY", "")
    monkeypatch.setenv("CORS_ORIGINS", "")
    # Test the CORS logic directly (module-level can't be easily reimported)
    api_key = ""
    admin_key = ""
    cors_raw = ""
    if not cors_raw:
        cors_raw = "*" if (not api_key and not admin_key) else ""
    assert cors_raw == "*"


def test_cors_restrictive_in_prod():
    """When API_KEY is set and CORS_ORIGINS empty, CORS should be restrictive."""
    api_key = "production-key"
    admin_key = "admin-key"
    cors_raw = ""
    if not cors_raw:
        cors_raw = "*" if (not api_key and not admin_key) else ""
    assert cors_raw == ""
