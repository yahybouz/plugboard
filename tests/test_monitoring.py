"""Phase 7 tests -- observability / monitoring (Prometheus, alerts, request-ID propagation)."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses, CallbackResult

from tests.factories import FAKE_COMPLETION

AUTH = {"Authorization": "Bearer testkey"}


# ── Prometheus text format ────────────────────────────────────────────────────


def test_prometheus_format():
    """format_prometheus returns valid Prometheus text."""
    from plugboard.metrics import format_prometheus, record

    # Use a unique provider id to avoid test interference
    pid = "prom-test-prov"
    from plugboard.metrics import _stats

    _stats.pop(pid, None)

    record(pid, 0.5)
    record(pid, 0.3)
    record(pid, 0.0, error=True)
    text = format_prometheus()
    assert "plugboard_requests_total" in text
    assert f'provider="{pid}"' in text
    assert "plugboard_up 1" in text
    assert "plugboard_errors_total" in text
    assert "plugboard_latency_avg_seconds" in text


@pytest.mark.asyncio
async def test_prometheus_endpoint(client):
    """GET /metrics/prometheus returns Prometheus text format with admin auth."""
    resp = await client.get("/metrics/prometheus", headers=AUTH)
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    assert "plugboard_up 1" in resp.text


@pytest.mark.asyncio
async def test_prometheus_endpoint_requires_admin(client):
    """GET /metrics/prometheus without auth returns 401."""
    resp = await client.get("/metrics/prometheus")
    assert resp.status_code == 401


# ── Provider failure tracking ─────────────────────────────────────────────────


def test_provider_failure_tracking():
    """record_provider_success resets failure count."""
    from plugboard.alerts import _provider_failures, record_provider_success

    _provider_failures["test-alert"] = 5
    record_provider_success("test-alert")
    assert _provider_failures.get("test-alert", 0) == 0


@pytest.mark.asyncio
async def test_provider_failure_increments():
    """record_provider_failure increments the counter."""
    from plugboard.alerts import _provider_failures, record_provider_failure

    _provider_failures.pop("test-inc", None)
    await record_provider_failure("test-inc")
    assert _provider_failures["test-inc"] == 1
    await record_provider_failure("test-inc")
    assert _provider_failures["test-inc"] == 2


# ── Request-ID propagation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_id_propagated(_providers_file):
    """forward() should send X-Request-ID header to backend."""
    from plugboard.logging_config import _request_id_var
    from plugboard.router import forward

    _request_id_var.set("test-rid-123")
    captured: dict = {}

    with aioresponses() as mock:

        def callback(_url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            return CallbackResult(status=200, payload=FAKE_COMPLETION)

        mock.post("http://fake-llm.test/v1/chat/completions", callback=callback)
        # Also mock the fallback provider
        mock.post("http://other-llm.test/v1/chat/completions", callback=callback)
        await forward({"model": "test-provider:fake:model-v1", "messages": []})

    assert captured["headers"].get("X-Request-ID") == "test-rid-123"


# ── Security headers ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_security_headers(client):
    """All responses must have security headers."""
    resp = await client.get("/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("x-xss-protection") == "1; mode=block"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "camera=()" in resp.headers.get("permissions-policy", "")
    assert resp.headers.get("cache-control") == "no-store"


@pytest.mark.asyncio
async def test_swagger_requires_admin(client):
    """GET /docs without admin key returns 401."""
    resp = await client.get("/docs")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_swagger_accessible_with_admin(client):
    """GET /docs with admin key returns 200."""
    resp = await client.get("/docs", headers=AUTH)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_openapi_json_requires_admin(client):
    """GET /openapi.json without admin key returns 401."""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 401


# ── Dashboard auth protection ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_redirects_to_login_without_auth(client):
    """GET /dashboard without auth must redirect to /login."""
    resp = await client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 307
    assert "/login" in resp.headers.get("location", "")


@pytest.mark.asyncio
async def test_dashboard_css_is_public(client):
    """GET /dashboard/style.css is public (CSS/JS assets don't require auth)."""
    resp = await client.get("/dashboard/style.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_dashboard_accessible_with_admin(client):
    """GET /dashboard with admin key returns 200."""
    resp = await client.get("/dashboard", headers=AUTH)
    assert resp.status_code == 200
    assert "html" in resp.headers.get("content-type", "").lower()


@pytest.mark.asyncio
async def test_login_page_is_public(client):
    """GET /login is accessible without auth."""
    resp = await client.get("/login")
    assert resp.status_code == 200
    assert "admin key" in resp.text.lower()


@pytest.mark.asyncio
async def test_portal_redirects_to_public_portal(client):
    """GET /portal redirects to the public client portal page."""
    resp = await client.get("/portal", follow_redirects=False)
    assert resp.status_code == 307
    assert "/dashboard/portal.html" in resp.headers.get("location", "")


@pytest.mark.asyncio
async def test_admin_login_sets_secure_session_cookie(client):
    """POST /auth/admin/login sets an HttpOnly admin session cookie."""
    resp = await client.post("/auth/admin/login", json={"key": "testkey"})
    assert resp.status_code == 204
    set_cookie = resp.headers.get("set-cookie", "")
    assert "plugboard_admin_session=" in set_cookie
    assert "HttpOnly" in set_cookie

    # Cookie-based auth should work without Authorization header
    resp2 = await client.get("/admin/keys")
    assert resp2.status_code == 200
