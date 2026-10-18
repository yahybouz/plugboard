"""Security & TNR (non-regression) tests for Plugboard."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from httpx import ASGITransport, AsyncClient

from tests.factories import FAKE_COMPLETION

AUTH = {"Authorization": "Bearer testkey"}
ADMIN_AUTH = {"Authorization": "Bearer adminkey"}


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_sql_injection_usage_month(admin_client):
    """GET /admin/usage with SQL injection in month param must not crash."""
    payloads = [
        "2026_01' OR '1'='1",
        "2026_01; DROP TABLE usage;--",
        "2026_01 UNION SELECT * FROM keys",
    ]
    for payload in payloads:
        resp = await admin_client.get(
            f"/admin/usage/{payload}",
            headers=ADMIN_AUTH,
        )
        # Must not return 500 (server error) -- 200 with empty data or 404 are fine
        assert resp.status_code in (200, 404), (
            f"SQL injection payload returned {resp.status_code}: {payload}"
        )


@pytest.mark.asyncio
async def test_path_traversal_dashboard(admin_client):
    """GET /dashboard/../../etc/passwd must return 404 or redirect, never 200."""
    traversal_paths = [
        "/dashboard/../../etc/passwd",
        "/dashboard/../../../etc/shadow",
        "/dashboard/..%2F..%2Fetc%2Fpasswd",
    ]
    for path in traversal_paths:
        resp = await admin_client.get(path, headers=ADMIN_AUTH, follow_redirects=False)
        # Must never return 200 with file contents
        assert resp.status_code in (301, 302, 307, 308, 400, 404, 405), (
            f"Path traversal returned {resp.status_code}: {path}"
        )


@pytest.mark.asyncio
async def test_path_traversal_prefix_bypass_blocked(
    admin_client, tmp_path, monkeypatch
):
    """Sibling directories with a matching prefix must not be accessible."""
    import plugboard.app as app_mod

    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>ok</html>")
    evil_dir = tmp_path / "static_evil"
    evil_dir.mkdir()
    (evil_dir / "secret.txt").write_text("TOP-SECRET")

    monkeypatch.setattr(app_mod, "_STATIC_DIR", static_dir)
    monkeypatch.setattr(app_mod, "_STATIC_DIR_RESOLVED", static_dir.resolve())

    resp = await admin_client.get(
        "/dashboard/../static_evil/secret.txt",
        headers=ADMIN_AUTH,
        follow_redirects=False,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_auth_bypass_empty_bearer(client):
    """Authorization: Bearer '' must fail on protected endpoints."""
    resp = await client.get(
        "/v1/models",
        headers={"Authorization": "Bearer "},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_bypass_none(client):
    """No Authorization header on protected endpoints must return 401."""
    protected = [
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
        ("GET", "/providers"),
        ("GET", "/admin/keys"),
    ]
    for method, path in protected:
        resp = await client.request(method, path)
        assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}"


@pytest.mark.asyncio
async def test_xss_in_client_id(admin_client):
    """Create key with XSS payload in client_id -- verify it is stored safely."""
    xss_payload = '<script>alert("xss")</script>'
    resp = await admin_client.post(
        "/admin/keys",
        headers=ADMIN_AUTH,
        json={"client_id": xss_payload, "label": "xss-test"},
    )
    assert resp.status_code == 201
    body = resp.json()
    # The client_id should be stored as-is (string), not executed
    assert body["client_id"] == xss_payload
    # Verify listing does not corrupt/execute
    list_resp = await admin_client.get("/admin/keys", headers=ADMIN_AUTH)
    assert list_resp.status_code == 200
    keys = list_resp.json()
    xss_keys = [k for k in keys if k.get("client_id") == xss_payload]
    assert len(xss_keys) >= 1
    # Response content-type must be application/json, not text/html
    assert "application/json" in list_resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_key_never_logged_in_response(admin_client):
    """POST /admin/keys returns the key, but GET /admin/keys masks it."""
    # Create a key -- response includes the full key
    create_resp = await admin_client.post(
        "/admin/keys",
        headers=ADMIN_AUTH,
        json={"client_id": "mask-test", "label": "mask-test"},
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    raw_key = created.get("key", "")
    assert len(raw_key) > 0, "POST /admin/keys must return the key"

    # List keys -- the raw key must NOT appear
    list_resp = await admin_client.get("/admin/keys", headers=ADMIN_AUTH)
    assert list_resp.status_code == 200
    list_body = list_resp.text
    assert raw_key not in list_body, "Raw API key must not appear in GET /admin/keys"


@pytest.mark.asyncio
async def test_rate_limit_actually_blocks(tmp_path, monkeypatch):
    """Send requests over RATE_LIMIT and verify 429."""
    import plugboard.ratelimit as rl

    # Set a very low global rate limit
    monkeypatch.setattr(rl, "_LIMIT", 3)
    # Clear existing buckets
    rl._buckets.clear()

    # Providers setup
    import json

    from plugboard.router import invalidate_models_cache
    import plugboard.config as cfg

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pfile = data_dir / "providers.json"
    pfile.write_text(json.dumps({"providers": []}))
    monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
    cfg.PROVIDERS_FILE = pfile
    cfg.PROVIDERS_LOCK = data_dir / "providers.json.lock"
    invalidate_models_cache()

    # Auth disabled so we can test rate limit alone
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")

    from plugboard.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        statuses = []
        for _ in range(6):
            resp = await c.get("/health")
            statuses.append(resp.status_code)
        # First 3 should pass (200), rest should be 429
        assert 429 in statuses, f"Rate limit never triggered: {statuses}"
        assert statuses.count(200) <= 3


@pytest.mark.asyncio
async def test_cors_headers_absent_for_unknown_origin(client):
    """CORS headers must NOT be present for unknown origins in prod mode."""
    # In prod mode (API_KEY set), CORS_ORIGINS defaults to empty — no CORS headers
    resp = await client.get("/health", headers={"Origin": "http://evil.com"})
    assert "access-control-allow-origin" not in resp.headers


@pytest.mark.asyncio
async def test_security_headers_all_present(client):
    """Verify all security headers are set on every response."""
    resp = await client.get("/health")
    expected_headers = [
        "x-content-type-options",
        "x-frame-options",
        "x-xss-protection",
        "referrer-policy",
        "permissions-policy",
        "cache-control",
        "x-robots-tag",
        "content-security-policy",
    ]
    for header in expected_headers:
        assert header in resp.headers, f"Missing security header: {header}"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"


@pytest.mark.asyncio
async def test_admin_endpoints_require_auth(admin_client):
    """Batch test: all /admin/* endpoints without auth must return 401."""
    admin_endpoints = [
        ("GET", "/admin/keys"),
        ("POST", "/admin/keys"),
        ("GET", "/admin/pricing/2026_01"),
        ("PUT", "/admin/pricing/2026_01"),
        ("GET", "/admin/usage/2026_01"),
        ("DELETE", "/admin/usage/2026_01"),
        ("GET", "/admin/audit"),
        ("GET", "/admin/monitoring"),
        ("GET", "/admin/cache/stats"),
        ("DELETE", "/admin/cache"),
        ("GET", "/admin/orgs"),
        ("PATCH", "/admin/pricing/2026_01"),
        ("POST", "/admin/pricing/2026_01/revert"),
        ("GET", "/admin/pricing/suggestions"),
        ("POST", "/admin/pricing/suggestions/apply"),
        ("POST", "/admin/pricing/suggestions/reject"),
        ("POST", "/admin/pricing/suggestions/abc/apply"),
        ("POST", "/admin/pricing/suggestions/abc/reject"),
        ("GET", "/admin/pricing/llm/status"),
        ("POST", "/admin/pricing/llm/run"),
        ("GET", "/admin/auto-update/status"),
        ("POST", "/admin/auto-update/run"),
        ("POST", "/admin/direct-providers/test"),
        ("POST", "/providers/test-provider/discover"),
    ]
    for method, path in admin_endpoints:
        resp = await admin_client.request(method, path)
        assert resp.status_code == 401, (
            f"{method} {path} returned {resp.status_code} without auth (expected 401)"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TNR (NON-REGRESSION) TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tnr_health(client):
    """/health returns ok:true."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "version" in body
    assert "providers" in body


@pytest.mark.asyncio
async def test_tnr_models_list(client):
    """/v1/models returns list with >0 models."""
    resp = await client.get("/v1/models", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert len(body["data"]) > 0


@pytest.mark.asyncio
async def test_tnr_create_and_revoke_key(admin_client):
    """Full key lifecycle: create -> list -> revoke -> verify inactive."""
    # Create
    resp = await admin_client.post(
        "/admin/keys",
        headers=ADMIN_AUTH,
        json={"client_id": "tnr-lifecycle", "label": "TNR test"},
    )
    assert resp.status_code == 201
    key_id = resp.json()["key_id"]
    assert key_id

    # List -- key must be there
    list_resp = await admin_client.get("/admin/keys", headers=ADMIN_AUTH)
    key_ids = [k["key_id"] for k in list_resp.json()]
    assert key_id in key_ids

    # Revoke
    revoke_resp = await admin_client.post(
        f"/admin/keys/{key_id}/revoke",
        headers=ADMIN_AUTH,
    )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_tnr_usage_logging(client):
    """A completion must log a usage entry."""
    from plugboard import billing as _billing
    from plugboard import usage as _usage

    month = _billing.current_pricing_month()

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
    assert resp.status_code == 200

    # Check that usage was logged
    entries = _usage.get_usage(month)
    assert len(entries) >= 1
    latest = entries[-1]
    assert latest["model"] == "fake:model-v1"


@pytest.mark.asyncio
async def test_tnr_billing_calculates_cost(client):
    """cost_usd > 0 for a known model with pricing."""
    from plugboard import billing as _billing

    # Set up pricing for the test model
    month = _billing.current_pricing_month()
    _billing.save_pricing(
        month,
        {
            "models": {
                "fake:model-v1": {"input_price": 3.0, "output_price": 15.0},
            },
        },
    )

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hello world"}],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    # Billing info must be injected
    assert "billing" in body
    assert body["billing"]["cost_usd"] > 0


@pytest.mark.asyncio
async def test_tnr_pricing_grid(admin_client, tmp_path, monkeypatch):
    """GET/PUT pricing round-trip works.

    Uses an isolated tmp directory so tests never pollute real
    data/pricing/ (previously left data/pricing/2099_01.json behind).
    """
    from plugboard import billing as _billing

    monkeypatch.setattr(_billing, "_PRICING_DIR", tmp_path / "pricing")

    month = "2099_01"
    pricing_data = {
        "models": {"test:model": {"input_price": 1.0, "output_price": 5.0}},
        "note": "TNR test pricing",
    }

    # PUT
    put_resp = await admin_client.put(
        f"/admin/pricing/{month}",
        headers=ADMIN_AUTH,
        json=pricing_data,
    )
    assert put_resp.status_code == 200

    # GET
    get_resp = await admin_client.get(
        f"/admin/pricing/{month}",
        headers=ADMIN_AUTH,
    )
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert "test:model" in body.get("models", {})
    assert body["models"]["test:model"]["input_price"] == 1.0


@pytest.mark.asyncio
async def test_tnr_export_csv(admin_client):
    """Export returns CSV content."""
    from plugboard import billing as _billing

    month = _billing.current_pricing_month()

    resp = await admin_client.get(
        f"/admin/usage/{month}/export?format=csv",
        headers=ADMIN_AUTH,
    )
    assert resp.status_code == 200
    assert "text/csv" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_tnr_audit_trail(admin_client):
    """An admin action must create an audit entry."""
    # Perform an admin action (create a key)
    await admin_client.post(
        "/admin/keys",
        headers=ADMIN_AUTH,
        json={"client_id": "audit-test", "label": "audit TNR"},
    )

    # Check audit log
    resp = await admin_client.get("/admin/audit", headers=ADMIN_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] > 0
    actions = [e.get("action") for e in body["entries"]]
    assert "key.create" in actions


@pytest.mark.asyncio
async def test_tnr_benchmarks(client):
    """/v1/models/benchmarks returns enriched data."""
    resp = await client.get("/v1/models/benchmarks", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)


@pytest.mark.asyncio
async def test_tnr_monitoring(admin_client):
    """/admin/monitoring returns provider status."""
    with aioresponses() as mock:
        # Mock provider health endpoints
        mock.get("http://fake-llm.test/health", status=200)
        mock.get("http://other-llm.test/health", status=200)
        # Mock /v1/models for monitoring
        mock.get("http://fake-llm.test/v1/models", status=200, payload={"data": []})
        mock.get("http://other-llm.test/v1/models", status=200, payload={"data": []})
        # The monitoring module may also call the local app's endpoints
        mock.get(
            "http://test/health",
            status=200,
            payload={"ok": True},
            repeat=True,
        )
        resp = await admin_client.get("/admin/monitoring", headers=ADMIN_AUTH)
    # 200 or 502 (if providers unreachable) -- just not 500/401
    assert resp.status_code in (200, 502), f"Monitoring returned {resp.status_code}"
    if resp.status_code == 200:
        body = resp.json()
        assert "providers" in body
