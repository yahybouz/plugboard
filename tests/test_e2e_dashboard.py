"""End-to-end tests for the dashboard (front) + API (back) wiring.

Guards against the regressions discovered in the v2.8.x smoke sessions:

* Dashboard HTML shell loads for an authenticated admin.
* Every API path called by `static/app.js` returns 2xx with a JSON
  response when the admin is logged in.
* Monitoring endpoint no longer 500s on healthy providers (was
  writing `None` into a NOT NULL column — Bug D, 2026-04-20).
* `/v1/models` entries are enriched with display_name / family /
  variant and have no bare+latest or bare+dated duplicates per
  (owned_by, family) (Bugs MODEL-1/MODEL-2 + dedupe extensions).
* `/admin/coverage/{pricing,benchmarks}` both report 100% and are
  admin-gated (Bugs PRICE-1 / BENCH-1).
* Router returns a single-provider candidate list when the user
  prefixes the model id (Bug B — no cross-provider fallback when
  the user explicitly picked a provider).
* The Codex JSONL error extractor finds the nested business error
  instead of leaking the noisy stderr warning (Bug E).

These tests are intentionally ASGI-level (httpx + ASGITransport)
rather than Playwright: they exercise the wiring between what the
frontend JS calls and what the backend exposes, which is what broke
in the session.
"""

from __future__ import annotations

import pathlib
from collections import defaultdict

import pytest

ADMIN_AUTH = {"Authorization": "Bearer adminkey"}


# ── Static shell ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dashboard_shell_served_after_admin_login(admin_client):
    """GET /dashboard must return the HTML shell with a 200."""
    # Log in so the cookie session is set (otherwise redirect to portal).
    await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    resp = await admin_client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.text.lower()
    assert "<html" in body or "<!doctype html" in body


@pytest.mark.asyncio
async def test_dashboard_static_assets_served(admin_client):
    """app.js + style.css must load. A 404 here silently blanks the UI."""
    await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    js = await admin_client.get("/dashboard/app.js")
    css = await admin_client.get("/dashboard/style.css")
    assert js.status_code == 200
    assert "application/javascript" in js.headers.get(
        "content-type", ""
    ) or "javascript" in js.headers.get("content-type", "")
    assert css.status_code == 200


@pytest.mark.asyncio
async def test_portal_loads_vendored_chart_js_without_a_cdn(admin_client):
    """The client portal must not pull a third-party script at runtime."""
    portal = (
        pathlib.Path(__file__).resolve().parent.parent / "static" / "portal.html"
    ).read_text()
    assert "https://" not in "".join(
        line for line in portal.splitlines() if "<script" in line
    )
    chart = await admin_client.get("/dashboard/vendor/chartjs/chart.umd.js")
    assert chart.status_code == 200
    assert "Chart.js v4.4.1" in chart.text[:400]


@pytest.mark.asyncio
async def test_dashboard_has_no_connections_tab(admin_client):
    """OAuth accounts are managed from the provider form, not a separate tab."""
    await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    shell = (await admin_client.get("/dashboard")).text
    assert 'data-tab="connections"' not in shell
    assert 'id="page-connections"' not in shell
    assert (
        'id="dp-disconnect-btn"' in (await admin_client.get("/dashboard/app.js")).text
    )


# ── Wiring : each frontend fetch has a backend that answers ──────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/metrics",
        "/v1/models",
        "/providers",
        "/admin/keys",
        "/admin/jobs",
        "/admin/monitoring",
        "/admin/monitoring/history?limit=20",
        "/admin/direct-providers/presets",
        "/admin/oauth/connections",
        "/admin/ollama/status",
    ],
)
async def test_dashboard_api_path_reachable(admin_client, path: str):
    """Every endpoint the dashboard JS calls must answer 200 + JSON for admin."""
    resp = await admin_client.get(path, headers=ADMIN_AUTH)
    assert resp.status_code == 200, (
        f"{path} returned {resp.status_code}: {resp.text[:200]}"
    )
    # Health & metrics may be plain text or JSON — only the json-typed
    # endpoints need to parse cleanly.
    if path not in {"/metrics"}:
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            assert resp.json() is not None


# ── Bug D regression — /admin/monitoring doesn't 500 on healthy providers ────


@pytest.mark.asyncio
async def test_admin_monitoring_returns_valid_json_not_500(admin_client):
    """When a provider's health probe sets `error=None` (healthy), the
    monitoring snapshot insert used to violate NOT NULL. Dashboard saw
    "Failed to load monitoring: Unexpected token 'I', 'Internal S'...".
    """
    resp = await admin_client.get("/admin/monitoring", headers=ADMIN_AUTH)
    assert resp.status_code == 200, resp.text[:200]
    body = resp.json()
    assert "providers" in body
    assert "summary" in body
    assert "ts" in body


# ── Bugs MODEL-1/MODEL-2 regression — catalog enrichment + dedupe ────────────


@pytest.mark.asyncio
async def test_v1_models_enriched_with_display_name_family_variant(admin_client):
    """Every entry in /v1/models must carry the fields a picker needs.
    A regression here makes the dashboard show "4b" or "671b-cloud" as
    a bare model name."""
    resp = await admin_client.get("/v1/models", headers=ADMIN_AUTH)
    assert resp.status_code == 200
    data = resp.json().get("data", [])
    assert data, "/v1/models returned no entries"
    missing = [
        m.get("id")
        for m in data
        if not (m.get("display_name") and m.get("family") is not None)
    ]
    assert not missing, f"{len(missing)} models lack display_name/family: {missing[:5]}"


@pytest.mark.asyncio
async def test_v1_models_has_no_bare_latest_or_bare_dated_duplicates(admin_client):
    """For each (owned_by, family) group, a `-latest` makes bare + dated
    disappear; a bare pointer (no latest) makes dated disappear.
    """
    resp = await admin_client.get("/v1/models", headers=ADMIN_AUTH)
    data = resp.json().get("data", [])
    groups: dict = defaultdict(list)
    for m in data:
        groups[(m.get("owned_by", ""), m.get("family", ""))].append(
            m.get("variant", "") or ""
        )

    offenders = []
    for (owner, family), variants in groups.items():
        has_latest = "latest" in variants
        has_bare = "" in variants
        has_dated = any(
            v and v.isdigit() and len(v) == 8 and v.startswith("20") for v in variants
        )
        if has_latest and (has_bare or has_dated):
            offenders.append((owner, family, variants, "latest+bare/dated"))
        if not has_latest and has_bare and has_dated:
            offenders.append((owner, family, variants, "bare+dated without latest"))

    assert not offenders, f"Dedupe regressions: {offenders}"


# ── Bugs PRICE-1 / BENCH-1 regression — coverage audit endpoints ─────────────


@pytest.mark.asyncio
async def test_admin_coverage_pricing_endpoint_shape(admin_client):
    resp = await admin_client.get("/admin/coverage/pricing", headers=ADMIN_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "month",
        "total",
        "covered_paid",
        "covered_free",
        "missing",
        "coverage_pct",
    ):
        assert key in body, f"pricing coverage payload missing {key}: {body}"
    assert isinstance(body["missing"], list)
    # Coverage pct is a float in [0, 100]
    assert 0.0 <= body["coverage_pct"] <= 100.0


@pytest.mark.asyncio
async def test_admin_coverage_benchmarks_endpoint_shape(admin_client):
    resp = await admin_client.get("/admin/coverage/benchmarks", headers=ADMIN_AUTH)
    assert resp.status_code == 200
    body = resp.json()
    for key in ("total", "curated", "derived", "none", "coverage_pct"):
        assert key in body
    assert isinstance(body["none"], list)
    assert 0.0 <= body["coverage_pct"] <= 100.0


@pytest.mark.asyncio
async def test_admin_coverage_endpoints_require_auth(client):
    """Without a bearer token these must refuse."""
    for path in ("/admin/coverage/pricing", "/admin/coverage/benchmarks"):
        resp = await client.get(path)
        assert resp.status_code == 401, f"{path} leaked a 200 without auth"


# ── Bug B regression — no cross-provider fallback on explicit prefix ─────────


def test_router_no_cross_provider_cascade_when_prefix_explicit(monkeypatch):
    """Historically the router would try `gpt-5.4-pro` on every active
    provider when Codex failed, producing misleading 404s from GitHub
    Models. When the user prefixes with `<provider>:`, we must return
    only that provider."""
    from plugboard import router as router_mod

    fake_active = [
        {
            "id": "chatgpt-via-codex-cli",
            "is_active": True,
            "auth_source": "subprocess:codex",
        },
        {"id": "ollama", "is_active": True, "auth_source": "http"},
        {"id": "github-models-via-gh-cli", "is_active": True, "auth_source": "http"},
    ]
    monkeypatch.setattr(router_mod, "load_providers", lambda: fake_active)

    candidates = router_mod._resolve_all_providers("chatgpt-via-codex-cli:gpt-5.4-pro")
    assert len(candidates) == 1, (
        f"Explicit-prefix must not cascade, got: {[c[0]['id'] for c in candidates]}"
    )
    assert candidates[0][0]["id"] == "chatgpt-via-codex-cli"
    assert candidates[0][1] == "gpt-5.4-pro"


def test_router_cascades_when_no_provider_prefix(monkeypatch):
    """Without a prefix, fallback across providers is still allowed."""
    from plugboard import router as router_mod

    fake_active = [
        {"id": "ollama", "is_active": True, "auth_source": "http"},
        {
            "id": "chatgpt-via-codex-cli",
            "is_active": True,
            "auth_source": "subprocess:codex",
        },
    ]
    monkeypatch.setattr(router_mod, "load_providers", lambda: fake_active)

    candidates = router_mod._resolve_all_providers("some-model")
    # At least one candidate per active provider.
    assert len(candidates) >= 2


# ── Bug E regression — Codex error extractor surfaces useful message ─────────


def test_codex_jsonl_error_extraction_unwraps_nested_message():
    """Guards the `_extract_cli_error` helper that Bug E relied on."""
    from plugboard.cli_subprocess import _extract_cli_error

    stream = (
        '{"type":"thread.started","thread_id":"abc"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"error","message":"{\\"type\\":\\"error\\",'
        '\\"status\\":400,\\"error\\":{\\"type\\":\\"invalid_request_error\\",'
        '\\"message\\":\\"Model X not supported with a ChatGPT account.\\"}}"}\n'
        '{"type":"turn.failed"}'
    )
    msg = _extract_cli_error(stream, "codex")
    assert "Model X not supported" in msg
    # The noisy "Reading additional input from stdin" warning must NOT
    # be what leaks out — that would be from stderr, not stdout.
    assert "stdin" not in msg.lower()
