"""
Audit complet — sécurité + performance + E2E bout-en-bout.

Couverture :
  SÉCURITÉ
    S-01  HSTS header présent sur toutes les réponses
    S-02  Body size limit (413 sur Content-Length > MAX)
    S-03  Injection SQL dans month param → pas de 500
    S-04  Path traversal → 404 (pas de fuite de fichier)
    S-05  Auth bypass : Bearer vide → 401
    S-06  Auth bypass : pas d'Authorization → 401 sur endpoints protégés
    S-07  XSS payload dans client_id → stocké proprement, réponse JSON
    S-08  Clé en clair jamais exposée dans GET /admin/keys
    S-09  model allowed_models : modèle interdit → 403
    S-10  model allowed_models : modèle autorisé → 200
    S-11  Tous les headers sécurité présents (CSP, X-Frame, etc.)
    S-12  Rate limit global déclenche 429
    S-13  Admin cookie session : login → cookie → accès admin
    S-14  Admin cookie invalide rejeté → 401

  E2E FLOWS
    E-01  Lifecycle billing complet : create key → chat → usage logged → cost > 0
    E-02  Spending limit : key avec limite → dépasse → 429
    E-03  Spending limit : bien en dessous → 200
    E-04  Model access restriction : allowed_models filtre correctement
    E-05  Key rotation : ancienne clé rejetée, nouvelle acceptée
    E-06  Cache HIT : même requête deux fois → X-Cache: HIT
    E-07  Cache MISS : cache=false → X-Cache: MISS toujours
    E-08  Per-client RPM rate limit : rpm_limit=2 → 3ème req → 429
    E-09  Org usage aggregation : /admin/orgs/{org}/usage/{month}
    E-10  Admin key CRUD complet : create → list → revoke → delete
    E-11  Audit trail : key.create + key.revoke + key.delete loggués
    E-12  Usage purge endpoint répond 200
    E-13  Provider delete et re-create (CRUD providers)
    E-14  Pricing round-trip : PUT → GET → valeurs identiques
    E-15  /v1/me retourne client_id + usage + spending
    E-16  Timeout retourne 503 + Retry-After: 10
    E-17  Semantic cache HIT (MinHash) : paraphrase → X-Cache: SEMANTIC-HIT-MINHASH
    E-18  Semantic cache MISS : prompt unrelated → MISS, provider rappelé
    E-19  Semantic cache multi-tenant isolation (SEC-001) : 2 tenants → 0 leak
    E-20  Semantic cache disabled by default : pas de SEMANTIC-HIT sans opt-in
    E-21  Dev mode (API_KEY="") avec lle_* keys : isolation cache exact maintenue
    E-22  Dev mode + lle_* invalide → 401 (pas de silent bypass — v2.8.4 MF2)
    E-23  Dev mode + lle_* revoked → 401 (pareil que E-22, key revoked path)
    E-24  Cookie admin + Bearer lle_* → scope = lle_, pas admin (v2.8.4 MF3)
    E-25  STRICT_TENANT_MODE=1 + API_KEY="" → boot raise RuntimeError (v2.8.4 MF1)

  PERF (smoke)
    P-01  /health répond en < 200 ms (cold)
    P-02  /v1/models répond en < 500 ms (avec provider)
"""

from __future__ import annotations

import json
import time

import pytest
from aioresponses import aioresponses
from httpx import ASGITransport, AsyncClient

from tests.factories import FAKE_COMPLETION, PROVIDER_A

ADMIN = {"Authorization": "Bearer adminkey"}
CLIENT = {"Authorization": "Bearer testkey"}


# ─────────────────────────────────────────────────────────────────────────────
# Fixture isolation complète
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def _iso(tmp_path, monkeypatch):
    """Isolation complète : tous les modules patchés sur un répertoire temporaire."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "pricing").mkdir()
    # Keys get a monthly budget by default, and a budgeted key is refused a
    # model without a price (402): give the test model a synthetic one.
    from plugboard.billing import current_pricing_month

    (data_dir / "pricing" / f"{current_pricing_month()}.json").write_text(
        json.dumps(
            {"models": {"fake:model-v1": {"input_price": 1.0, "output_price": 2.0}}}
        )
    )

    import plugboard.config as cfg

    pfile = data_dir / "providers.json"
    pfile.write_text(json.dumps({"providers": [PROVIDER_A]}))
    monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
    monkeypatch.setattr(cfg, "PROVIDERS_FILE", pfile)
    monkeypatch.setattr(cfg, "PROVIDERS_LOCK", data_dir / "providers.json.lock")

    import plugboard.apikeys as ak

    keys_file = data_dir / "api_keys.json"
    keys_file.write_text(json.dumps({"keys": []}))
    monkeypatch.setattr(ak, "_DATA_DIR", data_dir)
    monkeypatch.setattr(ak, "KEYS_FILE", keys_file)
    monkeypatch.setattr(ak, "KEYS_LOCK", data_dir / "api_keys.json.lock")

    import plugboard.usage as usage_mod

    monkeypatch.setattr(usage_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(usage_mod, "_DB_PATH", data_dir / "usage.db")

    import plugboard.audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(audit_mod, "_DB_PATH", data_dir / "audit.db")

    import plugboard.cache as cache_mod

    monkeypatch.setattr(cache_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(cache_mod, "_DB_PATH", data_dir / "cache.db")

    import plugboard.monitoring as mon_mod

    monkeypatch.setattr(mon_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(mon_mod, "_DB_PATH", data_dir / "monitoring.db")

    import plugboard.provider_watchdog as wd_mod

    monkeypatch.setattr(wd_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(wd_mod, "_DB_PATH", data_dir / "watchdog.db")

    import plugboard.billing as bill_mod

    monkeypatch.setattr(bill_mod, "_DATA_DIR", data_dir)
    monkeypatch.setattr(bill_mod, "_PRICING_DIR", data_dir / "pricing")

    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 0)

    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey")
    monkeypatch.setattr("plugboard.app._API_KEY", "testkey")

    # Disable the PricingScheduler in E2E tests — its bootstrap fires real
    # HTTP calls to external pricing sources, which both slows the suite
    # down and creates network flakiness on perf assertions (P-01, P-02).
    monkeypatch.setenv("PRICING_SCHEDULER_DISABLED", "1")

    from plugboard.router import invalidate_models_cache

    invalidate_models_cache()

    return data_dir


@pytest.fixture()
async def api(_iso):
    from plugboard.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _month():
    from plugboard.billing import current_pricing_month

    return current_pricing_month()


# ═════════════════════════════════════════════════════════════════════════════
# SÉCURITÉ
# ═════════════════════════════════════════════════════════════════════════════


class TestSecurityHeaders:
    @pytest.mark.asyncio
    async def test_s01_hsts_present(self, api):
        """S-01 — Strict-Transport-Security doit être présent sur toutes les réponses."""
        resp = await api.get("/health")
        assert "strict-transport-security" in resp.headers, (
            "HSTS header manquant — risque de downgrade attack"
        )
        hsts = resp.headers["strict-transport-security"]
        assert "max-age=" in hsts
        assert int(hsts.split("max-age=")[1].split(";")[0]) >= 31536000, (
            "HSTS max-age doit être >= 1 an (31536000 s)"
        )

    @pytest.mark.asyncio
    async def test_s01_hsts_on_json_endpoint(self, api):
        """S-01 — HSTS présent sur les endpoints JSON aussi."""
        resp = await api.get("/v1/models", headers=ADMIN)
        assert "strict-transport-security" in resp.headers

    @pytest.mark.asyncio
    async def test_s02_body_size_limit(self, api, monkeypatch):
        """S-02 — Content-Length > MAX_BODY_BYTES → 413."""
        import plugboard.app as app_mod

        monkeypatch.setattr(app_mod, "_MAX_BODY_BYTES", 100)
        resp = await api.post(
            "/v1/chat/completions",
            headers={**ADMIN, "content-length": "9999999"},
            content=b"x" * 101,
        )
        assert resp.status_code == 413, f"Attendu 413, got {resp.status_code}"
        assert "too large" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_s03_sql_injection_month(self, api):
        """S-03 — Injection SQL dans le paramètre month ne provoque pas de 500."""
        payloads = [
            "2026_01' OR '1'='1",
            "2026_01; DROP TABLE usage;--",
            "2026_01 UNION SELECT * FROM sqlite_master",
            "'; DELETE FROM usage WHERE '1'='1",
        ]
        for payload in payloads:
            resp = await api.get(f"/admin/usage/{payload}", headers=ADMIN)
            assert resp.status_code in (200, 404, 422), (
                f"SQL injection → {resp.status_code} pour payload: {payload!r}"
            )

    @pytest.mark.asyncio
    async def test_s04_path_traversal(self, api):
        """S-04 — Tentatives de path traversal ne fuitent pas de fichiers."""
        paths = [
            "/dashboard/../../etc/passwd",
            "/dashboard/../../../etc/shadow",
            "/dashboard/..%2F..%2Fetc%2Fpasswd",
            "/static/../../../etc/hosts",
        ]
        for path in paths:
            resp = await api.get(path, headers=ADMIN, follow_redirects=False)
            assert resp.status_code not in (200,), (
                f"Path traversal a renvoyé 200 pour {path!r}"
            )

    @pytest.mark.asyncio
    async def test_s05_auth_bypass_empty_bearer(self, api):
        """S-05 — Bearer vide → 401 sur endpoints protégés."""
        for path in ["/v1/models", "/admin/keys", "/providers"]:
            resp = await api.get(path, headers={"Authorization": "Bearer "})
            assert resp.status_code == 401, (
                f"Bearer vide devrait donner 401 sur {path}, got {resp.status_code}"
            )

    @pytest.mark.asyncio
    async def test_s06_no_auth_header(self, api):
        """S-06 — Pas d'Authorization → 401 sur endpoints protégés."""
        protected = [
            ("GET", "/v1/models"),
            ("GET", "/providers"),
            ("GET", "/admin/keys"),
            ("GET", "/admin/audit"),
            ("POST", "/v1/chat/completions"),
        ]
        for method, path in protected:
            resp = await api.request(method, path)
            assert resp.status_code == 401, (
                f"{method} {path} sans auth → attendu 401, got {resp.status_code}"
            )

    @pytest.mark.asyncio
    async def test_s07_xss_in_client_id(self, api):
        """S-07 — XSS payload dans client_id stocké proprement, Content-Type: JSON."""
        xss = '<script>alert("xss")</script>'
        resp = await api.post("/admin/keys", headers=ADMIN, json={"client_id": xss})
        assert resp.status_code == 201
        body = resp.json()
        assert body["client_id"] == xss  # stocké as-is, pas exécuté

        list_resp = await api.get("/admin/keys", headers=ADMIN)
        assert list_resp.status_code == 200
        assert "application/json" in list_resp.headers.get("content-type", ""), (
            "La réponse doit être JSON, pas HTML"
        )

    @pytest.mark.asyncio
    async def test_s08_raw_key_never_in_list(self, api):
        """S-08 — Le plaintext d'une clé ne doit jamais apparaître dans GET /admin/keys."""
        create_resp = await api.post(
            "/admin/keys", headers=ADMIN, json={"client_id": "mask-audit"}
        )
        assert create_resp.status_code == 201
        raw_key = create_resp.json()["key"]
        assert len(raw_key) > 20

        list_resp = await api.get("/admin/keys", headers=ADMIN)
        assert raw_key not in list_resp.text, (
            "La clé brute ne doit jamais apparaître dans GET /admin/keys"
        )

    @pytest.mark.asyncio
    async def test_s09_allowed_models_blocks_forbidden(self, api):
        """S-09 — allowed_models : modèle non listé → 403."""
        create_resp = await api.post(
            "/admin/keys",
            headers=ADMIN,
            json={
                "client_id": "restricted",
                "allowed_models": ["gpt-4o"],
            },
        )
        assert create_resp.status_code == 201
        raw_key = create_resp.json()["key"]
        restricted_auth = {"Authorization": f"Bearer {raw_key}"}

        resp = await api.post(
            "/v1/chat/completions",
            headers=restricted_auth,
            json={
                "model": "test-provider:fake:model-v1",  # pas dans allowed
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert resp.status_code == 403, (
            f"Modèle interdit devrait donner 403, got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_s10_allowed_models_permits_listed(self, api):
        """S-10 — allowed_models : modèle autorisé → 200."""
        create_resp = await api.post(
            "/admin/keys",
            headers=ADMIN,
            json={
                "client_id": "allowed-client",
                "allowed_models": ["fake:model-v1"],
            },
        )
        assert create_resp.status_code == 201
        raw_key = create_resp.json()["key"]
        auth = {"Authorization": f"Bearer {raw_key}"}

        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            resp = await api.post(
                "/v1/chat/completions",
                headers=auth,
                json={
                    "model": "test-provider:fake:model-v1",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_s11_all_security_headers(self, api):
        """S-11 — Tous les headers de sécurité requis sont présents."""
        resp = await api.get("/health")
        required = [
            "x-content-type-options",
            "x-frame-options",
            "x-xss-protection",
            "referrer-policy",
            "permissions-policy",
            "cache-control",
            "x-robots-tag",
            "content-security-policy",
            "strict-transport-security",
        ]
        for h in required:
            assert h in resp.headers, f"Header de sécurité manquant: {h}"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]

    @pytest.mark.asyncio
    async def test_s12_rate_limit_triggers_429(self, monkeypatch, tmp_path):
        """S-12 — Le rate limit global bloque après N requêtes."""
        import plugboard.ratelimit as rl

        monkeypatch.setattr(rl, "_LIMIT", 3)
        rl._buckets.clear()

        import plugboard.config as cfg

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        pfile = data_dir / "providers.json"
        pfile.write_text(json.dumps({"providers": []}))
        monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
        monkeypatch.setattr(cfg, "PROVIDERS_FILE", pfile)
        monkeypatch.setattr(cfg, "PROVIDERS_LOCK", data_dir / "providers.json.lock")
        from plugboard.router import invalidate_models_cache

        invalidate_models_cache()

        monkeypatch.setattr("plugboard.app._API_KEY", "")
        monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")

        from plugboard.app import app

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            statuses = [await c.get("/health") for _ in range(6)]
            codes = [r.status_code for r in statuses]
            assert 429 in codes, f"Rate limit jamais déclenché : {codes}"
            assert codes[:3].count(200) == 3

    @pytest.mark.asyncio
    async def test_s13_admin_cookie_login_flow(self, api):
        """S-13 — Login admin via cookie : POST /auth/admin/login → 204 + cookie → accès /admin/keys."""
        login_resp = await api.post("/auth/admin/login", json={"key": "adminkey"})
        assert login_resp.status_code == 204, (
            f"Login admin devrait retourner 204, got {login_resp.status_code}"
        )
        # httpx suit les cookies de session sur le même client
        cookies = dict(login_resp.cookies)
        assert "plugboard_admin_session" in cookies, "Cookie de session admin absent"

        # Accès admin avec le cookie (sans header Authorization)
        resp = await api.get(
            "/admin/keys",
            cookies={"plugboard_admin_session": cookies["plugboard_admin_session"]},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_s14_invalid_admin_cookie_rejected(self, api):
        """S-14 — Cookie de session invalide → 401."""
        resp = await api.get(
            "/admin/keys",
            cookies={"plugboard_admin_session": "invalid-fake-cookie-token"},
        )
        assert resp.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# E2E FLOWS
# ═════════════════════════════════════════════════════════════════════════════


class TestBillingLifecycle:
    @pytest.mark.asyncio
    async def test_e01_full_billing_lifecycle(self, api, _iso):
        """E-01 — create key → chat → usage logged → billing.cost_usd > 0."""
        import plugboard.billing as bill_mod
        import plugboard.usage as usage_mod

        month = _month()
        # Setup pricing
        bill_mod.save_pricing(
            month,
            {
                "models": {"fake:model-v1": {"input_price": 3.0, "output_price": 15.0}},
            },
        )

        # Create key
        create_resp = await api.post(
            "/admin/keys", headers=ADMIN, json={"client_id": "billing-test"}
        )
        assert create_resp.status_code == 201
        raw_key = create_resp.json()["key"]
        auth = {"Authorization": f"Bearer {raw_key}"}

        # Chat
        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            resp = await api.post(
                "/v1/chat/completions",
                headers=auth,
                json={
                    "model": "test-provider:fake:model-v1",
                    "messages": [{"role": "user", "content": "Hello world"}],
                },
            )
        assert resp.status_code == 200
        body = resp.json()

        # Billing injected in response
        assert "billing" in body, "billing manquant dans la réponse"
        assert body["billing"]["cost_usd"] > 0, "cost_usd doit être > 0"
        assert body["billing"]["prompt_tokens"] > 0

        # Usage logged
        entries = usage_mod.get_usage(month)
        assert len(entries) >= 1
        latest = entries[-1]
        assert latest["model"] == "fake:model-v1"
        assert latest["cost_usd"] > 0

        # X-Cost-USD header
        assert "x-cost-usd" in resp.headers

    @pytest.mark.asyncio
    async def test_e01_usage_me_reflects_billing(self, api, _iso):
        """E-01b — /usage/me retourne les entrées loggées."""
        import plugboard.billing as bill_mod

        month = _month()
        bill_mod.save_pricing(
            month,
            {
                "models": {"fake:model-v1": {"input_price": 1.0, "output_price": 5.0}},
            },
        )

        create_resp = await api.post(
            "/admin/keys", headers=ADMIN, json={"client_id": "usage-me-test"}
        )
        raw_key = create_resp.json()["key"]
        auth = {"Authorization": f"Bearer {raw_key}"}

        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            await api.post(
                "/v1/chat/completions",
                headers=auth,
                json={
                    "model": "test-provider:fake:model-v1",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        resp = await api.get("/usage/me", headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["entries"]) >= 1
        assert body["summary"]["total_cost_usd"] > 0


class TestSpendingLimit:
    @pytest.mark.asyncio
    async def test_e02_spending_limit_blocks_at_cap(self, api, _iso):
        """E-02 — Key avec limite dépassée → 429."""
        import plugboard.usage as usage_mod
        import plugboard.billing as bill_mod

        month = _month()
        bill_mod.save_pricing(
            month,
            {
                "models": {
                    "fake:model-v1": {"input_price": 100.0, "output_price": 100.0}
                },
            },
        )

        create_resp = await api.post(
            "/admin/keys",
            headers=ADMIN,
            json={
                "client_id": "limited-client",
                "monthly_limit_usd": 0.001,  # très basse
            },
        )
        assert create_resp.status_code == 201
        key_id = create_resp.json()["key_id"]
        raw_key = create_resp.json()["key"]
        auth = {"Authorization": f"Bearer {raw_key}"}

        # Injecter une entrée de coût qui dépasse la limite
        usage_mod.append_usage(
            client_id="limited-client",
            model="fake:model-v1",
            provider_id="test-provider",
            prompt_tokens=1000,
            completion_tokens=500,
            cost_usd=0.002,
            pricing_month=month,
            key_id=key_id,
        )

        # La prochaine requête doit être bloquée
        resp = await api.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert resp.status_code == 429, (
            f"Spending limit dépassé devrait bloquer (429), got {resp.status_code}"
        )
        assert "spending limit" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_e03_spending_limit_allows_under_cap(self, api, _iso):
        """E-03 — Usage bien en dessous de la limite → 200."""
        create_resp = await api.post(
            "/admin/keys",
            headers=ADMIN,
            json={
                "client_id": "rich-client",
                "monthly_limit_usd": 1000.0,
            },
        )
        assert create_resp.status_code == 201
        raw_key = create_resp.json()["key"]
        auth = {"Authorization": f"Bearer {raw_key}"}

        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            resp = await api.post(
                "/v1/chat/completions",
                headers=auth,
                json={
                    "model": "test-provider:fake:model-v1",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
        assert resp.status_code == 200


class TestModelAccess:
    @pytest.mark.asyncio
    async def test_e04_model_access_restriction(self, api):
        """E-04 — allowed_models filtre correctement (allowed + blocked)."""
        create_resp = await api.post(
            "/admin/keys",
            headers=ADMIN,
            json={
                "client_id": "filtered-client",
                "allowed_models": ["fake:model-v1"],
            },
        )
        raw_key = create_resp.json()["key"]
        auth = {"Authorization": f"Bearer {raw_key}"}

        # Modèle autorisé → 200
        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            resp_ok = await api.post(
                "/v1/chat/completions",
                headers=auth,
                json={
                    "model": "test-provider:fake:model-v1",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )
        assert resp_ok.status_code == 200

        # Modèle interdit → 403
        resp_blocked = await api.post(
            "/v1/chat/completions",
            headers=auth,
            json={
                "model": "test-provider:gpt-4-forbidden",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert resp_blocked.status_code == 403


class TestKeyRotation:
    @pytest.mark.asyncio
    async def test_e05_key_rotation(self, api):
        """E-05 — Rotation : ancienne clé rejetée, nouvelle acceptée."""
        create_resp = await api.post(
            "/admin/keys", headers=ADMIN, json={"client_id": "rotation-client"}
        )
        assert create_resp.status_code == 201
        old_key = create_resp.json()["key"]
        key_id = create_resp.json()["key_id"]

        # Ancienne clé fonctionne
        resp_before = await api.get(
            "/v1/models", headers={"Authorization": f"Bearer {old_key}"}
        )
        assert resp_before.status_code == 200

        # Rotation
        rotate_resp = await api.post(f"/admin/keys/{key_id}/rotate", headers=ADMIN)
        assert rotate_resp.status_code == 201
        new_key = rotate_resp.json()["key"]
        assert new_key != old_key

        # Ancienne clé rejetée
        resp_old = await api.get(
            "/v1/models", headers={"Authorization": f"Bearer {old_key}"}
        )
        assert resp_old.status_code == 401, (
            "L'ancienne clé doit être rejetée après rotation"
        )

        # Nouvelle clé acceptée
        resp_new = await api.get(
            "/v1/models", headers={"Authorization": f"Bearer {new_key}"}
        )
        assert resp_new.status_code == 200, (
            "La nouvelle clé doit être acceptée après rotation"
        )


class TestCacheBehavior:
    @pytest.mark.asyncio
    async def test_e06_cache_hit(self, api):
        """E-06 — Même requête deux fois → deuxième est X-Cache: HIT."""
        body = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": "Cache test E2E"}],
            "max_tokens": 5,
            "temperature": 0,
        }

        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            r1 = await api.post("/v1/chat/completions", headers=ADMIN, json=body)

        assert r1.status_code == 200
        assert r1.headers.get("x-cache") == "MISS"

        # Deuxième requête — cache doit être HIT (pas de mock, le backend n'est pas appelé)
        r2 = await api.post("/v1/chat/completions", headers=ADMIN, json=body)
        assert r2.status_code == 200
        assert r2.headers.get("x-cache") == "HIT"

    @pytest.mark.asyncio
    async def test_e07_cache_false_bypasses_cache(self, api):
        """E-07 — cache=false → X-Cache: MISS systématique."""
        body = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": "No cache test"}],
            "cache": False,
        }

        for _ in range(2):
            with aioresponses() as mock:
                mock.post(
                    "http://fake-llm.test/v1/chat/completions",
                    payload=FAKE_COMPLETION,
                    status=200,
                )
                resp = await api.post("/v1/chat/completions", headers=ADMIN, json=body)
            assert resp.status_code == 200
            assert resp.headers.get("x-cache") == "MISS", (
                "cache=false doit toujours retourner MISS"
            )


class TestSemanticCacheBehavior:
    """E-17 to E-19 — opt-in semantic cache (MinHash phase 1).

    These tests enable PLUGBOARD_SEMANTIC_CACHE per-class and use
    paraphrases that bypass the exact cache but must hit the semantic
    layer. Threshold is lowered to 0.5 to make the trigram-Jaccard match
    deterministic on short test prompts.
    """

    @pytest.fixture(autouse=True)
    def _enable_semantic(self, monkeypatch, _iso):
        """Activate the semantic cache and isolate its DB to tmp_path."""
        monkeypatch.setenv("PLUGBOARD_SEMANTIC_CACHE", "1")
        import plugboard.semantic_cache as sem_mod

        monkeypatch.setattr(sem_mod, "_DATA_DIR", _iso)
        monkeypatch.setattr(sem_mod, "_DB_PATH", _iso / "semantic_cache.db")
        # Module-level constant was frozen at import; patch in place so
        # paraphrase tests reliably clear the threshold.
        monkeypatch.setattr(sem_mod, "_DEFAULT_THRESHOLD", 0.5)

    @pytest.mark.asyncio
    async def test_e17_semantic_minhash_hit(self, api):
        """E-17 — Paraphrase proche → X-Cache: SEMANTIC-HIT-MINHASH.

        The exact cache key differs (added/missing tokens change the hash),
        but the MinHash signature overlap clears the 0.5 threshold so the
        provider must NOT be re-called on the second request.
        """
        prompt_orig = (
            "What is the difference between Python lists and tuples in terms "
            "of performance and memory usage ?"
        )
        prompt_paraphrase = (
            "What is difference between Python lists and tuples in terms of "
            "performance and memory usage"
        )
        body_orig = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": prompt_orig}],
            "max_tokens": 5,
            "temperature": 0,
        }
        body_para = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": prompt_paraphrase}],
            "max_tokens": 5,
            "temperature": 0,
        }

        # First call hits the provider; response is cached (exact + semantic).
        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            r1 = await api.post("/v1/chat/completions", headers=ADMIN, json=body_orig)
        assert r1.status_code == 200
        assert r1.headers.get("x-cache") == "MISS"

        # Second call: paraphrase. Exact cache miss, semantic HIT expected.
        # No provider mock — if forwarding happens, the URL is unmocked and
        # the request would fail. A 200 here proves the provider was skipped.
        r2 = await api.post("/v1/chat/completions", headers=ADMIN, json=body_para)
        assert r2.status_code == 200, "semantic HIT must avoid the provider call"
        assert r2.headers.get("x-cache") == "SEMANTIC-HIT-MINHASH"
        assert r2.headers.get("x-cache-similarity") is not None

    @pytest.mark.asyncio
    async def test_e18_semantic_unrelated_miss(self, api):
        """E-18 — Prompt unrelated → MISS even with the semantic layer on.

        Guards against false-positive paraphrase matches: trigram overlap
        between two unrelated prompts must stay below threshold so the
        provider IS rappelé.
        """
        body_orig = {
            "model": "test-provider:fake:model-v1",
            "messages": [
                {
                    "role": "user",
                    "content": "How do I reset my password on this service ?",
                }
            ],
            "max_tokens": 5,
        }
        body_other = {
            "model": "test-provider:fake:model-v1",
            "messages": [
                {
                    "role": "user",
                    "content": "Describe the history of the Roman empire please",
                }
            ],
            "max_tokens": 5,
        }

        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            r1 = await api.post("/v1/chat/completions", headers=ADMIN, json=body_orig)
        assert r1.status_code == 200

        # Provider must be re-called — assert with a fresh mock (would fail
        # otherwise on unmocked URL).
        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            r2 = await api.post("/v1/chat/completions", headers=ADMIN, json=body_other)
        assert r2.status_code == 200
        assert r2.headers.get("x-cache") == "MISS"

    @pytest.mark.asyncio
    async def test_e19_semantic_multi_tenant_isolation(self, api):
        """E-19 — SEC-001 end-to-end: tenant A's cache is invisible to tenant B.

        Even with identical prompts and the semantic flag on, B must hit the
        provider and never receive A's cached response. This proves the
        SEC-001 fix is wired through the full HTTP boundary (API key →
        client_id → cache scope), not just at the module level.
        """
        ka = await api.post("/admin/keys", headers=ADMIN, json={"client_id": "alice"})
        assert ka.status_code == 201
        alice = {"Authorization": f"Bearer {ka.json()['key']}"}

        kb = await api.post("/admin/keys", headers=ADMIN, json={"client_id": "bob"})
        assert kb.status_code == 201
        bob = {"Authorization": f"Bearer {kb.json()['key']}"}

        body = {
            "model": "test-provider:fake:model-v1",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Confidential question that only one tenant should "
                        "see in cache please reply"
                    ),
                }
            ],
            "max_tokens": 5,
        }

        # Distinct payloads so we can prove which response each tenant got.
        completion_a = {**FAKE_COMPLETION, "id": "alice-secret-response"}
        completion_b = {**FAKE_COMPLETION, "id": "bob-secret-response"}

        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=completion_a,
                status=200,
            )
            r1 = await api.post("/v1/chat/completions", headers=alice, json=body)
        assert r1.status_code == 200
        assert r1.json()["id"] == "alice-secret-response"

        # Bob fires the SAME prompt. If SEC-001 is broken, bob would receive
        # alice-secret-response without a provider call. Mocking bob's
        # provider response proves both that the call is made AND that bob's
        # tenant gets only its own data.
        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=completion_b,
                status=200,
            )
            r2 = await api.post("/v1/chat/completions", headers=bob, json=body)
        assert r2.status_code == 200
        assert r2.json()["id"] == "bob-secret-response", (
            "SEC-001 violated: bob received alice's cached response across tenants"
        )
        # Header may be MISS (exact + semantic both missed) — must NOT be a
        # SEMANTIC-HIT pointing at alice's row.
        x_cache = r2.headers.get("x-cache") or ""
        assert "SEMANTIC-HIT" not in x_cache, (
            f"SEC-001 violated: bob got SEMANTIC-HIT (X-Cache={x_cache})"
        )


class TestSemanticCacheDisabled:
    """E-20 — Semantic cache stays opt-in: no SEMANTIC-HIT without the flag."""

    @pytest.mark.asyncio
    async def test_e20_semantic_disabled_by_default(self, api, monkeypatch):
        """E-20 — Without PLUGBOARD_SEMANTIC_CACHE, paraphrase is a MISS."""
        monkeypatch.delenv("PLUGBOARD_SEMANTIC_CACHE", raising=False)
        prompt_orig = (
            "What is the difference between Python lists and tuples in terms "
            "of performance and memory usage ?"
        )
        prompt_paraphrase = (
            "What is difference between Python lists and tuples in terms of "
            "performance and memory usage"
        )
        body_orig = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": prompt_orig}],
            "max_tokens": 5,
        }
        body_para = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": prompt_paraphrase}],
            "max_tokens": 5,
        }

        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            r1 = await api.post("/v1/chat/completions", headers=ADMIN, json=body_orig)
        assert r1.status_code == 200

        # Without the flag, the paraphrase must be a clean MISS — provider
        # is rappelé (a fresh mock proves it).
        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=FAKE_COMPLETION,
                status=200,
            )
            r2 = await api.post("/v1/chat/completions", headers=ADMIN, json=body_para)
        assert r2.status_code == 200
        x_cache = r2.headers.get("x-cache") or ""
        assert x_cache == "MISS", (
            f"semantic cache must be off by default, got X-Cache={x_cache}"
        )
        assert "SEMANTIC-HIT" not in x_cache


class TestExactCacheDevModeMultiTenant:
    """E-21 — Dev mode (API_KEY env empty) preserves cache isolation.

    Live regression: when API_KEY="" the legacy _resolve_client returned
    None for any lle_* key, collapsing all tenants to client_id="" → cache
    leak across tenants in dev mode (the production fix in v2.8.2 only
    triggered the SQL-side filter for non-None client_id). v2.8.3 makes
    _resolve_client honour valid lle_* keys even when API_KEY is unset.
    """

    @pytest.fixture(autouse=True)
    def _dev_mode(self, monkeypatch, _iso):
        """Simulate the dev-mode scenario: API_KEY env unset."""
        monkeypatch.setattr("plugboard.app._API_KEY", "")

    @pytest.mark.asyncio
    async def test_e21_dev_mode_preserves_cache_isolation(self, api):
        """API_KEY='' + 2 lle_* keys → bob never sees alice's cached row."""
        ka = await api.post(
            "/admin/keys", headers=ADMIN, json={"client_id": "alice-dev"}
        )
        assert ka.status_code == 201
        alice = {"Authorization": f"Bearer {ka.json()['key']}"}

        kb = await api.post("/admin/keys", headers=ADMIN, json={"client_id": "bob-dev"})
        assert kb.status_code == 201
        bob = {"Authorization": f"Bearer {kb.json()['key']}"}

        body = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": "Confidential dev-mode prompt"}],
            "max_tokens": 5,
        }
        completion_a = {**FAKE_COMPLETION, "id": "alice-dev-resp"}
        completion_b = {**FAKE_COMPLETION, "id": "bob-dev-resp"}

        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=completion_a,
                status=200,
            )
            r1 = await api.post("/v1/chat/completions", headers=alice, json=body)
        assert r1.status_code == 200
        assert r1.json()["id"] == "alice-dev-resp"

        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=completion_b,
                status=200,
            )
            r2 = await api.post("/v1/chat/completions", headers=bob, json=body)
        assert r2.status_code == 200
        assert r2.json()["id"] == "bob-dev-resp", (
            "SEC-001 v2.8.3 violated: bob got alice's cached response in dev mode"
        )

    @pytest.mark.asyncio
    async def test_e22_dev_mode_invalid_lle_returns_401(self, api):
        """E-22 — v2.8.4 MF2: presenting an unknown lle_* must 401, not bypass.

        Without this, a dev who mistypes their key gets `client_id=None` and
        anonymous access — masking the bug AND leaking responses to the
        empty-tenant scope.
        """
        bad = {"Authorization": "Bearer lle_definitely_not_a_real_key_12345"}
        body = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": "x" * 30}],
            "max_tokens": 5,
        }
        resp = await api.post("/v1/chat/completions", headers=bad, json=body)
        assert resp.status_code == 401, (
            f"v2.8.4 MF2 violated: invalid lle_* should 401, got {resp.status_code}"
        )

    @pytest.mark.asyncio
    async def test_e23_dev_mode_revoked_lle_returns_401(self, api):
        """E-23 — Same as E-22 but for a key that existed and was revoked."""
        ka = await api.post(
            "/admin/keys", headers=ADMIN, json={"client_id": "to-revoke"}
        )
        assert ka.status_code == 201
        record = ka.json()
        raw = record["key"]
        key_id = record["key_id"]

        # Revoke the key, then try to use it.
        rev = await api.delete(f"/admin/keys/{key_id}", headers=ADMIN)
        assert rev.status_code in {200, 204}

        body = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": "x" * 30}],
            "max_tokens": 5,
        }
        resp = await api.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {raw}"},
            json=body,
        )
        assert resp.status_code == 401, (
            f"v2.8.4 MF2 violated: revoked lle_* should 401, got {resp.status_code}"
        )


class TestAdminCookieBearerPriority:
    """E-24 — v2.8.4 MF3: Bearer lle_* wins over admin cookie session."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _iso):
        # API_KEY enforced so the prod auth path is exercised.
        pass

    @pytest.mark.asyncio
    async def test_e24_bearer_lle_overrides_admin_cookie(self, api):
        """An admin user testing with a tenant lle_* key must land in the
        tenant scope, not the shared "admin" cache scope."""
        ka = await api.post(
            "/admin/keys", headers=ADMIN, json={"client_id": "alice-priority"}
        )
        assert ka.status_code == 201
        alice_raw = ka.json()["key"]

        # Forge an admin session cookie via the login endpoint.
        login = await api.post("/auth/admin/login", json={"key": "adminkey"})
        assert login.status_code == 204
        admin_session = dict(login.cookies).get("plugboard_admin_session", "")
        assert admin_session

        body = {
            "model": "test-provider:fake:model-v1",
            "messages": [{"role": "user", "content": "Identity test"}],
            "max_tokens": 5,
        }
        completion = {**FAKE_COMPLETION, "id": "alice-scope-resp"}

        # Send BOTH the admin cookie AND a lle_alice bearer.
        with aioresponses() as mock:
            mock.post(
                "http://fake-llm.test/v1/chat/completions",
                payload=completion,
                status=200,
            )
            r = await api.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {alice_raw}"},
                cookies={"plugboard_admin_session": admin_session},
                json=body,
            )
        assert r.status_code == 200
        # Confirm the scope: hitting the cache again with alice's bearer
        # alone must be a HIT (proves the row was written under alice's
        # scope, not admin's). If MF3 were broken, the row would live in
        # the admin scope and alice would re-MISS, re-call provider.
        r2 = await api.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {alice_raw}"},
            json=body,
        )
        assert r2.status_code == 200
        assert r2.headers.get("x-cache") == "HIT", (
            "v2.8.4 MF3 violated: cookie+bearer wrote to admin scope, "
            "not alice — bearer lle_* did not take precedence"
        )


class TestStrictTenantBootGuard:
    """E-25 — v2.8.4 MF1: STRICT_TENANT_MODE + API_KEY="" must fail at boot."""

    def test_e25_strict_tenant_with_empty_api_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Importing app.py with the toxic combo must raise RuntimeError.

        We exercise this by re-importing the module under the bad env so
        the module-level guard fires. After the test we MUST re-import the
        module under sane env to restore sys.modules["plugboard.app"] —
        otherwise downstream tests reload it without `_iso`'s setattr
        overrides and break in confusing ways.
        """
        import importlib
        import sys

        monkeypatch.setenv("PLUGBOARD_STRICT_TENANT", "1")
        monkeypatch.delenv("API_KEY", raising=False)
        # Ensure app is reloaded with the new env.
        sys.modules.pop("plugboard.app", None)
        try:
            with pytest.raises(RuntimeError, match="STRICT_TENANT"):
                importlib.import_module("plugboard.app")
        finally:
            # Restore: drop and re-import under a sane env so other tests
            # find a properly initialised module in sys.modules.
            monkeypatch.delenv("PLUGBOARD_STRICT_TENANT", raising=False)
            monkeypatch.setenv("API_KEY", "testkey")
            sys.modules.pop("plugboard.app", None)
            importlib.import_module("plugboard.app")


class TestPerClientRateLimit:
    @pytest.mark.asyncio
    async def test_e08_per_client_rpm_limit(self, api):
        """E-08 — rpm_limit=2 → 3ème requête → 429."""
        create_resp = await api.post(
            "/admin/keys",
            headers=ADMIN,
            json={
                "client_id": "rpm-client",
                "rpm_limit": 2,
            },
        )
        assert create_resp.status_code == 201
        raw_key = create_resp.json()["key"]
        auth = {"Authorization": f"Bearer {raw_key}"}

        # Clear rate limit buckets
        import plugboard.ratelimit as rl

        rl._client_buckets.clear()

        results = []
        for _ in range(3):
            with aioresponses() as mock:
                mock.post(
                    "http://fake-llm.test/v1/chat/completions",
                    payload=FAKE_COMPLETION,
                    status=200,
                )
                resp = await api.post(
                    "/v1/chat/completions",
                    headers=auth,
                    json={
                        "model": "test-provider:fake:model-v1",
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )
            results.append(resp.status_code)

        assert 429 in results, f"RPM limit jamais déclenché : {results}"
        assert results.count(200) >= 2


class TestOrgUsage:
    @pytest.mark.asyncio
    async def test_e09_org_usage_aggregation(self, api, _iso):
        """E-09 — /admin/orgs/{org}/usage/{month} agrège correctement."""
        import plugboard.usage as usage_mod

        month = _month()

        # Créer 2 clés dans la même org
        for client_id in ["org-a", "org-b"]:
            await api.post(
                "/admin/keys",
                headers=ADMIN,
                json={"client_id": client_id, "org_id": "testorg"},
            )

        # Injecter du usage pour les deux clients
        for client_id in ["org-a", "org-b"]:
            usage_mod.append_usage(
                client_id=client_id,
                model="fake:model-v1",
                provider_id="test",
                prompt_tokens=100,
                completion_tokens=50,
                cost_usd=0.01,
                pricing_month=month,
                key_id="k-test",
            )

        resp = await api.get(f"/admin/orgs/testorg/usage/{month}", headers=ADMIN)
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["total_cost_usd"] >= 0.02
        assert len(body["clients"]) >= 2


class TestAdminCRUD:
    @pytest.mark.asyncio
    async def test_e10_key_crud_lifecycle(self, api):
        """E-10 — Cycle CRUD complet : create → list → revoke → delete."""
        # Create
        resp = await api.post(
            "/admin/keys",
            headers=ADMIN,
            json={"client_id": "crud-client", "label": "CRUD test"},
        )
        assert resp.status_code == 201
        key_id = resp.json()["key_id"]

        # List → présent
        list_resp = await api.get("/admin/keys", headers=ADMIN)
        assert resp.status_code == 201
        key_ids = [k["key_id"] for k in list_resp.json()]
        assert key_id in key_ids

        # Revoke → is_active=False
        rev_resp = await api.post(f"/admin/keys/{key_id}/revoke", headers=ADMIN)
        assert rev_resp.status_code == 200
        assert rev_resp.json()["is_active"] is False

        # Delete → 204
        del_resp = await api.delete(f"/admin/keys/{key_id}", headers=ADMIN)
        assert del_resp.status_code == 204

        # List → absent
        list_resp2 = await api.get("/admin/keys", headers=ADMIN)
        assert key_id not in [k["key_id"] for k in list_resp2.json()]

    @pytest.mark.asyncio
    async def test_e11_audit_trail_completeness(self, api):
        """E-11 — create + revoke + delete sont tous loggués dans l'audit trail."""
        # Create
        resp = await api.post(
            "/admin/keys", headers=ADMIN, json={"client_id": "audit-crud"}
        )
        key_id = resp.json()["key_id"]

        # Revoke
        await api.post(f"/admin/keys/{key_id}/revoke", headers=ADMIN)

        # Delete
        await api.delete(f"/admin/keys/{key_id}", headers=ADMIN)

        audit_resp = await api.get("/admin/audit", headers=ADMIN)
        assert audit_resp.status_code == 200
        actions = {e["action"] for e in audit_resp.json()["entries"]}
        assert "key.create" in actions
        assert "key.revoke" in actions
        assert "key.delete" in actions

    @pytest.mark.asyncio
    async def test_e12_usage_purge(self, api):
        """E-12 — POST /admin/usage/purge répond 200."""
        resp = await api.post("/admin/usage/purge", headers=ADMIN)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_e13_provider_crud(self, api):
        """E-13 — Provider CRUD : create → get → update → delete."""
        new_provider = {
            "name": "Audit Prov",
            "base_url": "http://audit-prov.test",
            "bearer_token": "tok123",
            "is_active": True,
        }

        # Create — get the actual id from the response (slugified from name)
        create_resp = await api.post("/providers", headers=ADMIN, json=new_provider)
        assert create_resp.status_code == 201
        prov_id = create_resp.json()["id"]
        assert prov_id  # must be non-empty

        # Get list → présent
        list_resp = await api.get("/providers", headers=ADMIN)
        ids = [p["id"] for p in list_resp.json()]
        assert prov_id in ids

        # Update
        upd_resp = await api.put(
            f"/providers/{prov_id}",
            headers=ADMIN,
            json={**new_provider, "name": "Audit Prov Updated"},
        )
        assert upd_resp.status_code == 200

        # Delete
        del_resp = await api.delete(f"/providers/{prov_id}", headers=ADMIN)
        assert del_resp.status_code == 204

        # Absent
        list_resp2 = await api.get("/providers", headers=ADMIN)
        assert prov_id not in [p["id"] for p in list_resp2.json()]

    @pytest.mark.asyncio
    async def test_e14_pricing_round_trip(self, api):
        """E-14 — PUT pricing → GET → valeurs identiques."""
        month = "2099_12"
        pricing = {
            "models": {
                "audit:model-v1": {"input_price": 7.5, "output_price": 30.0},
            },
            "note": "audit E2E test",
        }

        put_resp = await api.put(f"/admin/pricing/{month}", headers=ADMIN, json=pricing)
        assert put_resp.status_code == 200

        get_resp = await api.get(f"/admin/pricing/{month}", headers=ADMIN)
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert "audit:model-v1" in body["models"]
        assert body["models"]["audit:model-v1"]["input_price"] == 7.5
        assert body["models"]["audit:model-v1"]["output_price"] == 30.0

    @pytest.mark.asyncio
    async def test_e15_v1_me_endpoint(self, api):
        """E-15 — /v1/me retourne client_id, usage, spending."""
        create_resp = await api.post(
            "/admin/keys",
            headers=ADMIN,
            json={"client_id": "me-client", "monthly_limit_usd": 50.0},
        )
        raw_key = create_resp.json()["key"]
        auth = {"Authorization": f"Bearer {raw_key}"}

        resp = await api.get("/v1/me", headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["client_id"] == "me-client"
        assert "usage" in body
        assert "monthly_limit_usd" in body
        assert body["monthly_limit_usd"] == 50.0
        assert "remaining_usd" in body

    @pytest.mark.asyncio
    async def test_e16_timeout_503_retry_after(self, api):
        """E-16 — Timeout provider → 503 + Retry-After: 10."""
        import asyncio

        with aioresponses() as mock:
            for _ in range(5):
                mock.post(
                    "http://fake-llm.test/v1/chat/completions",
                    exception=asyncio.TimeoutError(),
                )
            resp = await api.post(
                "/v1/chat/completions",
                headers=ADMIN,
                json={
                    "model": "test-provider:fake:model-v1",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert resp.status_code == 503
        assert "Retry-After" in resp.headers
        assert resp.headers["Retry-After"] == "10"
        assert "timeout" in resp.json()["detail"].lower()


# ═════════════════════════════════════════════════════════════════════════════
# PERFORMANCE SMOKE
# ═════════════════════════════════════════════════════════════════════════════


class TestPerfSmoke:
    @pytest.mark.asyncio
    async def test_p01_health_under_200ms(self, api):
        """P-01 — /health doit répondre en moins de 200 ms.

        Mesure le *min* sur 3 appels après warm-up pour absorber le coût
        de la première requête (init pool DB, scheduler asyncio, GC…)
        qui pollue la mesure d'un single shot, surtout en parallèle CI.
        Le but du test est de surveiller la perf stable, pas l'overhead
        cold-start de la suite de tests."""
        # Warm-up : ignore la première mesure.
        await api.get("/health")
        timings_ms: list[float] = []
        for _ in range(3):
            t0 = time.perf_counter()
            resp = await api.get("/health")
            timings_ms.append((time.perf_counter() - t0) * 1000)
            assert resp.status_code == 200
        best_ms = min(timings_ms)
        assert best_ms < 200, (
            f"/health trop lent : best={best_ms:.1f}ms over 3 runs (all={timings_ms})"
        )

    @pytest.mark.asyncio
    async def test_p02_models_under_500ms(self, api):
        """P-02 — /v1/models doit répondre en moins de 500 ms.

        Best-of-3 après warm-up, même rationale que P-01."""
        await api.get("/v1/models", headers=ADMIN)
        timings_ms: list[float] = []
        for _ in range(3):
            t0 = time.perf_counter()
            resp = await api.get("/v1/models", headers=ADMIN)
            timings_ms.append((time.perf_counter() - t0) * 1000)
            assert resp.status_code == 200
        best_ms = min(timings_ms)
        assert best_ms < 500, (
            f"/v1/models trop lent : best={best_ms:.1f}ms over 3 runs (all={timings_ms})"
        )

    @pytest.mark.asyncio
    async def test_p03_admin_keys_list_under_200ms(self, api):
        """P-03 — GET /admin/keys doit répondre en moins de 200 ms."""
        # Créer quelques clés pour simuler une charge réelle
        for i in range(5):
            await api.post(
                "/admin/keys", headers=ADMIN, json={"client_id": f"perf-client-{i}"}
            )

        t0 = time.perf_counter()
        resp = await api.get("/admin/keys", headers=ADMIN)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert resp.status_code == 200
        assert elapsed_ms < 200, f"GET /admin/keys trop lent : {elapsed_ms:.1f} ms"

    @pytest.mark.asyncio
    async def test_p04_request_id_header_always_present(self, api):
        """P-04 — X-Request-ID doit être présent et unique sur toutes les réponses."""
        ids = []
        for _ in range(5):
            resp = await api.get("/health")
            rid = resp.headers.get("x-request-id")
            assert rid, "X-Request-ID manquant"
            assert len(rid) == 16, f"X-Request-ID doit être 16 chars, got {len(rid)}"
            ids.append(rid)
        assert len(set(ids)) == 5, "X-Request-ID doit être unique par requête"
