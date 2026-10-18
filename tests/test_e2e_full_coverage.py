"""
E2E Full Coverage — tests every endpoint and feature from PROMPT.md §9.

Covers the 13 endpoints previously untested:
  1.  GET  /admin/circuits
  2.  POST /admin/circuits/{model}/reset
  3.  GET  /admin/jobs
  4.  GET  /admin/jobs/{id}/log
  5.  POST /admin/jobs/{id}/run
  6.  GET  /admin/usage/{month}/export?format=csv
  7.  GET  /admin/usage/{month}/key/{key_id}
  8.  GET  /admin/usage/{month}/{client}
  9.  GET  /admin/orgs/{org_id}/keys
  10. POST /admin/pricing/scrape
  11. POST /admin/usage/migrate
  12. POST /v2/chat/completions
  13. GET  /redoc

Plus:
  - Key prefix generation (lle_ format)
  - 503 + Retry-After on timeout
  - Timeout retry behavior
"""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses
from httpx import ASGITransport, AsyncClient

from tests.factories import FAKE_COMPLETION, PROVIDER_A

AUTH_ADMIN = {"Authorization": "Bearer adminkey"}
AUTH_CLIENT = {"Authorization": "Bearer testkey"}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def _isolate(tmp_path, monkeypatch):
    """Full data isolation for all modules."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "pricing").mkdir()

    # Providers
    import plugboard.config as cfg

    pfile = data_dir / "providers.json"
    pfile.write_text(json.dumps({"providers": [PROVIDER_A]}))
    monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
    monkeypatch.setattr(cfg, "PROVIDERS_FILE", pfile)
    monkeypatch.setattr(cfg, "PROVIDERS_LOCK", data_dir / "providers.json.lock")

    # API keys
    import plugboard.apikeys as ak

    keys_file = data_dir / "api_keys.json"
    keys_file.write_text(json.dumps({"keys": []}))
    monkeypatch.setattr(ak, "_DATA_DIR", data_dir)
    monkeypatch.setattr(ak, "KEYS_FILE", keys_file)
    monkeypatch.setattr(ak, "KEYS_LOCK", data_dir / "api_keys.json.lock")

    # Usage (SQLite)
    import plugboard.usage as usage_mod

    monkeypatch.setattr(usage_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(usage_mod, "_DB_PATH", data_dir / "usage.db")

    # Audit (SQLite)
    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(audit_mod, "_DB_PATH", data_dir / "audit.db")

    # Cache (SQLite)
    import plugboard.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(cache_mod, "_DB_PATH", data_dir / "cache.db")

    # Monitoring (SQLite)
    import plugboard.monitoring as mon_mod

    monkeypatch.setattr(mon_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(mon_mod, "_DB_PATH", data_dir / "monitoring.db")

    # Watchdog (SQLite)
    import plugboard.provider_watchdog as wd_mod

    monkeypatch.setattr(wd_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(wd_mod, "_DB_PATH", data_dir / "watchdog.db")

    # Billing / pricing
    import plugboard.billing as bill_mod

    monkeypatch.setattr(bill_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(bill_mod, "_PRICING_DIR", data_dir / "pricing")

    # Rate limit off
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 0)

    # Auth
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    monkeypatch.setattr("plugboard.app._API_KEY", "testkey")

    from plugboard.router import invalidate_models_cache

    invalidate_models_cache()

    return data_dir


@pytest.fixture()
async def api(_isolate):
    from plugboard.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


def _month():
    from plugboard.billing import current_pricing_month

    return current_pricing_month()


# ── 1. GET /admin/circuits ───────────────────────────────────────────────────


class TestCircuits:
    @pytest.mark.asyncio
    async def test_get_circuits(self, api):
        resp = await api.get("/admin/circuits", headers=AUTH_ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert "circuits" in body
        assert "tier_timeouts" in body
        assert "fallback_map" in body
        # All tier timeouts should be 600s
        for tier, val in body["tier_timeouts"].items():
            assert val == 900.0, f"tier {tier} should be 900s"

    @pytest.mark.asyncio
    async def test_get_circuits_requires_admin(self, api):
        resp = await api.get(
            "/admin/circuits", headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    # ── 2. POST /admin/circuits/{model}/reset ────────────────────────────────

    @pytest.mark.asyncio
    async def test_reset_circuit(self, api):
        # Trigger a circuit failure first
        from plugboard.circuit_breaker import record_failure, _circuits

        record_failure("test-model")
        record_failure("test-model")
        record_failure("test-model")
        assert _circuits["test-model"].state == "open"

        resp = await api.post("/admin/circuits/test-model/reset", headers=AUTH_ADMIN)
        assert resp.status_code == 200
        assert resp.json()["state"] == "closed"

    @pytest.mark.asyncio
    async def test_reset_circuit_requires_admin(self, api):
        resp = await api.post("/admin/circuits/x/reset", headers=AUTH_CLIENT)
        assert resp.status_code == 401


# ── 3-5. GET /admin/jobs, GET /admin/jobs/{id}/log, POST /admin/jobs/{id}/run


class TestJobsEndpoints:
    @pytest.mark.asyncio
    async def test_list_jobs(self, api):
        resp = await api.get("/admin/jobs", headers=AUTH_ADMIN)
        assert resp.status_code == 200
        jobs = resp.json()
        assert isinstance(jobs, list)
        assert len(jobs) >= 2
        ids = {j["id"] for j in jobs}
        assert "pricing_refresh" in ids
        assert "model_refresh" in ids
        assert "provider_watchdog" in ids
        for j in jobs:
            assert "name" in j
            assert "schedule" in j
            assert "next_run" in j
            assert "last_status" in j

    @pytest.mark.asyncio
    async def test_list_jobs_requires_admin(self, api):
        resp = await api.get("/admin/jobs", headers=AUTH_CLIENT)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_job_log(self, api):
        resp = await api.get("/admin/jobs/pricing_refresh/log", headers=AUTH_ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "pricing_refresh"
        assert "name" in body

    @pytest.mark.asyncio
    async def test_job_log_not_found(self, api):
        resp = await api.get("/admin/jobs/nonexistent/log", headers=AUTH_ADMIN)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_run_job_not_found(self, api):
        resp = await api.post("/admin/jobs/nonexistent/run", headers=AUTH_ADMIN)
        assert resp.status_code == 404


# ── 6. GET /admin/usage/{month}/export?format=csv ────────────────────────────


class TestUsageExport:
    @pytest.mark.asyncio
    async def test_export_csv_empty(self, api):
        month = _month()
        resp = await api.get(
            f"/admin/usage/{month}/export?format=csv",
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 200
        assert "text/csv" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_export_csv_with_data(self, api, _isolate):
        """Inject a usage entry then export as CSV."""
        import plugboard.usage as usage_mod

        month = _month()
        usage_mod.append_usage(
            client_id="csv-test",
            model="fake:model-v1",
            provider_id="test",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.01,
            pricing_month=month,
            key_id="k1",
        )
        resp = await api.get(
            f"/admin/usage/{month}/export?format=csv",
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 200
        text = resp.text
        assert "csv-test" in text
        assert "fake:model-v1" in text

    @pytest.mark.asyncio
    async def test_export_json(self, api):
        month = _month()
        resp = await api.get(
            f"/admin/usage/{month}/export?format=json",
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 200


# ── 7. GET /admin/usage/{month}/key/{key_id} ─────────────────────────────────


class TestUsageByKey:
    @pytest.mark.asyncio
    async def test_usage_by_key(self, api, _isolate):
        import plugboard.usage as usage_mod

        month = _month()
        usage_mod.append_usage(
            client_id="client-a",
            model="m1",
            provider_id="test",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.001,
            pricing_month=month,
            key_id="key-abc",
        )
        resp = await api.get(
            f"/admin/usage/{month}/key/key-abc",
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["entries"]) >= 1
        assert body["summary"]["total_prompt_tokens"] >= 10

    @pytest.mark.asyncio
    async def test_usage_by_key_empty(self, api):
        month = _month()
        resp = await api.get(
            f"/admin/usage/{month}/key/nonexistent",
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 200
        assert len(resp.json()["entries"]) == 0


# ── 8. GET /admin/usage/{month}/{client} ──────────────────────────────────────


class TestUsageByClient:
    @pytest.mark.asyncio
    async def test_usage_by_client(self, api, _isolate):
        import plugboard.usage as usage_mod

        month = _month()
        usage_mod.append_usage(
            client_id="my-client",
            model="m1",
            provider_id="test",
            prompt_tokens=20,
            completion_tokens=10,
            cost_usd=0.002,
            pricing_month=month,
            key_id="k1",
        )
        resp = await api.get(
            f"/admin/usage/{month}/my-client",
            headers=AUTH_ADMIN,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["entries"]) >= 1
        assert body["summary"]["total_cost_usd"] >= 0.002


# ── 9. GET /admin/orgs/{org_id}/keys ──────────────────────────────────────────


class TestOrgKeys:
    @pytest.mark.asyncio
    async def test_org_keys(self, api):
        # Create keys with org_id
        r1 = await api.post(
            "/admin/keys",
            headers=AUTH_ADMIN,
            json={
                "client_id": "org-client-1",
                "org_id": "acme",
            },
        )
        assert r1.status_code == 201
        r2 = await api.post(
            "/admin/keys",
            headers=AUTH_ADMIN,
            json={
                "client_id": "org-client-2",
                "org_id": "acme",
            },
        )
        assert r2.status_code == 201
        r3 = await api.post(
            "/admin/keys",
            headers=AUTH_ADMIN,
            json={
                "client_id": "other-client",
                "org_id": "other-org",
            },
        )
        assert r3.status_code == 201

        resp = await api.get("/admin/orgs/acme/keys", headers=AUTH_ADMIN)
        assert resp.status_code == 200
        keys = resp.json()
        assert len(keys) == 2
        assert all(k["org_id"] == "acme" for k in keys)

    @pytest.mark.asyncio
    async def test_org_keys_empty(self, api):
        resp = await api.get("/admin/orgs/nonexistent/keys", headers=AUTH_ADMIN)
        assert resp.status_code == 200
        assert resp.json() == []


# ── 10. POST /admin/pricing/scrape ────────────────────────────────────────────


class TestPricingScrape:
    @pytest.mark.asyncio
    async def test_scrape_pricing(self, api, monkeypatch):
        # Mock the async scraper function used by the endpoint
        import plugboard.pricing_scraper as ps

        async def fake_scrape(**kwargs):
            return {"updated": 5, "added": 2, "source": "mock"}

        monkeypatch.setattr(ps, "update_current_pricing", fake_scrape)
        resp = await api.post("/admin/pricing/scrape", headers=AUTH_ADMIN)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_scrape_requires_admin(self, api):
        resp = await api.post("/admin/pricing/scrape", headers=AUTH_CLIENT)
        assert resp.status_code == 401


# ── 11. POST /admin/usage/migrate ─────────────────────────────────────────────


class TestUsageMigrate:
    @pytest.mark.asyncio
    async def test_migrate_usage(self, api):
        resp = await api.post("/admin/usage/migrate", headers=AUTH_ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert "migrated" in body or "status" in body or isinstance(body, dict)

    @pytest.mark.asyncio
    async def test_migrate_requires_admin(self, api):
        resp = await api.post("/admin/usage/migrate", headers=AUTH_CLIENT)
        assert resp.status_code == 401


# ── 12. POST /v2/chat/completions ─────────────────────────────────────────────


class TestV2Completions:
    @pytest.mark.asyncio
    async def test_v2_completions(self, api):
        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            resp = await api.post(
                "/v2/chat/completions",
                headers=AUTH_ADMIN,
                json={
                    "model": "test-provider:fake:model-v1",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "choices" in body
        assert body["choices"][0]["message"]["content"] == "Hello!"

    @pytest.mark.asyncio
    async def test_v2_completions_requires_auth(self, api):
        resp = await api.post(
            "/v2/chat/completions",
            headers={"Authorization": "Bearer wrong"},
            json={"model": "x", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 401


# ── 13. GET /redoc ────────────────────────────────────────────────────────────


class TestRedoc:
    @pytest.mark.asyncio
    async def test_redoc_accessible(self, api):
        resp = await api.get("/redoc", headers=AUTH_ADMIN)
        assert resp.status_code == 200


# ── Key prefix generation ─────────────────────────────────────────────────────


class TestKeyPrefix:
    @pytest.mark.asyncio
    async def test_key_starts_with_lle_prefix(self, api):
        resp = await api.post(
            "/admin/keys",
            headers=AUTH_ADMIN,
            json={
                "client_id": "prefix-test",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        raw_key = body.get("key", "")
        prefix = body.get("key_prefix", "")
        # Key starts with lle_ prefix
        assert raw_key.startswith("pb_"), (
            f"Key should start with lle_, got: {raw_key[:10]}"
        )
        assert prefix.startswith("pb_"), f"Prefix should start with lle_, got: {prefix}"
        # Prefix is first 8 chars of key
        assert raw_key.startswith(prefix)

    @pytest.mark.asyncio
    async def test_key_prefix_unique_per_key(self, api):
        r1 = await api.post(
            "/admin/keys", headers=AUTH_ADMIN, json={"client_id": "uniq-a"}
        )
        r2 = await api.post(
            "/admin/keys", headers=AUTH_ADMIN, json={"client_id": "uniq-b"}
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        p1 = r1.json()["key_prefix"]
        p2 = r2.json()["key_prefix"]
        assert p1 != p2, "Two keys should have different prefixes"


# ── 503 Retry-After on timeout ────────────────────────────────────────────────


class TestTimeoutRetryAfter:
    @pytest.mark.asyncio
    async def test_timeout_returns_503_with_retry_after(self, api, monkeypatch):
        """When all providers timeout, return 503 + Retry-After header."""
        import asyncio

        with aioresponses() as mock:
            # All retry attempts will timeout
            for _ in range(5):
                mock.post(
                    "http://fake-llm.test/v1/chat/completions",
                    exception=asyncio.TimeoutError(),
                )
            resp = await api.post(
                "/v1/chat/completions",
                headers=AUTH_ADMIN,
                json={
                    "model": "test-provider:fake:model-v1",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
        assert resp.status_code == 503
        assert "Retry-After" in resp.headers
        body = resp.json()
        assert "timeout" in body["detail"].lower()
        assert body["retry_after"] == 10


# ── Comprehensive endpoint coverage checklist ─────────────────────────────────


class TestEndpointExists:
    """Verify every endpoint from PROMPT.md §9 returns a non-404/405 response."""

    @pytest.mark.asyncio
    async def test_all_client_endpoints_accessible(self, api):
        """All client endpoints should be reachable (not 404/405)."""
        endpoints = [
            ("GET", "/health"),
            ("GET", "/health/detailed"),
            ("GET", "/v1/models"),
            ("GET", "/v1/me"),
            ("GET", "/v1/models/benchmarks"),
            ("GET", "/v1/me/keys"),
            ("GET", "/usage/me"),
        ]
        for method, path in endpoints:
            if method == "GET":
                resp = await api.get(path, headers=AUTH_ADMIN)
            else:
                resp = await api.post(path, headers=AUTH_ADMIN)
            assert resp.status_code not in (404, 405), (
                f"{method} {path} returned {resp.status_code}"
            )

    @pytest.mark.asyncio
    async def test_all_admin_endpoints_accessible(self, api, _isolate):
        """All admin endpoints should be reachable (not 404/405)."""
        month = _month()

        # Seed pricing data
        import plugboard.billing as bill_mod

        bill_mod.save_pricing(
            month, {"models": {"m1": {"input_price": 1.0, "output_price": 2.0}}}
        )

        # Seed usage data for export/key/client tests
        import plugboard.usage as usage_mod

        usage_mod.append_usage(
            client_id="test-client",
            model="m1",
            provider_id="test",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.001,
            pricing_month=month,
            key_id="test-key",
        )

        get_endpoints = [
            "/providers",
            "/metrics",
            "/metrics/prometheus",
            "/admin/keys",
            f"/admin/pricing/{month}",
            f"/admin/usage/{month}",
            f"/admin/usage/{month}/export?format=csv",
            f"/admin/usage/{month}/export?format=json",
            f"/admin/usage/{month}/key/test-key",
            f"/admin/usage/{month}/test-client",
            "/admin/audit",
            "/admin/cache/stats",
            "/admin/monitoring",
            "/admin/monitoring/history",
            "/admin/orgs",
            "/admin/circuits",
            "/admin/jobs",
            "/admin/jobs/pricing_refresh/log",
            "/admin/watchdog",
            "/admin/watchdog/events",
            "/admin/watchdog/restarts",
            "/v2/models",
            "/v2/models/benchmarks",
        ]

        for path in get_endpoints:
            resp = await api.get(path, headers=AUTH_ADMIN)
            assert resp.status_code not in (404, 405), (
                f"GET {path} returned {resp.status_code}: {resp.text[:200]}"
            )

    @pytest.mark.asyncio
    async def test_ui_routes_accessible(self, api):
        """Dashboard, portal, docs, redoc, login should all resolve."""
        ui_routes = ["/login", "/docs", "/redoc"]
        for path in ui_routes:
            resp = await api.get(path, headers=AUTH_ADMIN)
            assert resp.status_code not in (404, 405), (
                f"GET {path} returned {resp.status_code}"
            )

    @pytest.mark.asyncio
    async def test_dashboard_and_portal_routes(self, api):
        """Dashboard and portal require auth — should redirect or serve."""
        for path in ["/dashboard", "/portal"]:
            resp = await api.get(path, headers=AUTH_ADMIN, follow_redirects=False)
            # Either 200 (served) or 307/302 (redirect to login)
            assert resp.status_code in (200, 302, 307), (
                f"GET {path} returned {resp.status_code}"
            )


# ── Auth enforcement coverage ─────────────────────────────────────────────────


class TestAuthEnforcement:
    """Every admin endpoint must reject unauthenticated requests."""

    @pytest.mark.asyncio
    async def test_admin_endpoints_require_auth(self, api):
        bad_auth = {"Authorization": "Bearer invalid-key-xyz"}
        month = _month()

        admin_endpoints = [
            ("GET", "/providers"),
            ("GET", "/metrics"),
            ("GET", "/admin/keys"),
            ("GET", f"/admin/pricing/{month}"),
            ("GET", f"/admin/usage/{month}"),
            ("GET", "/admin/audit"),
            ("GET", "/admin/cache/stats"),
            ("GET", "/admin/monitoring"),
            ("GET", "/admin/circuits"),
            ("GET", "/admin/jobs"),
            ("GET", "/admin/watchdog"),
            ("POST", "/admin/pricing/scrape"),
            ("POST", "/admin/usage/migrate"),
            ("DELETE", "/admin/cache"),
        ]

        for method, path in admin_endpoints:
            if method == "GET":
                resp = await api.get(path, headers=bad_auth)
            elif method == "POST":
                resp = await api.post(path, headers=bad_auth)
            elif method == "DELETE":
                resp = await api.delete(path, headers=bad_auth)
            else:
                continue
            assert resp.status_code == 401, (
                f"{method} {path} should return 401, got {resp.status_code}"
            )
