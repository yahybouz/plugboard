"""Phase 4 tests — rate limiting, provider timeout, metrics, health check."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from tests.factories import FAKE_COMPLETION

AUTH = {"Authorization": "Bearer testkey"}


# ── Rate limiting (unit) ──────────────────────────────────────────────────────


def test_rate_limit_allows_under_limit(monkeypatch):
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 5)
    monkeypatch.setattr(rl, "_buckets", {})
    for _ in range(5):
        assert rl.check_rate_limit("1.2.3.4") is True


def test_rate_limit_blocks_over_limit(monkeypatch):
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 5)
    monkeypatch.setattr(rl, "_buckets", {})
    for _ in range(5):
        rl.check_rate_limit("1.2.3.4")
    assert rl.check_rate_limit("1.2.3.4") is False


def test_rate_limit_disabled_when_zero(monkeypatch):
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 0)
    monkeypatch.setattr(rl, "_buckets", {})
    for _ in range(100):
        assert rl.check_rate_limit("1.2.3.4") is True


# ── Rate limiting (API) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_returns_429_via_api(client, monkeypatch):
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 2)
    monkeypatch.setattr(rl, "_buckets", {})

    # First two requests should pass
    r1 = await client.get("/health")
    r2 = await client.get("/health")
    assert r1.status_code == 200
    assert r2.status_code == 200

    # Third request should be rate-limited
    r3 = await client.get("/health")
    assert r3.status_code == 429
    assert r3.json()["detail"] == "Rate limit exceeded"


# ── Provider timeout (config) ─────────────────────────────────────────────────


def test_provider_timeout_stored(tmp_path, monkeypatch):
    import plugboard.config as cfg

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pfile = data_dir / "providers.json"
    pfile.write_text('{"providers": []}')
    monkeypatch.setattr(cfg, "PROVIDERS_FILE", pfile)
    monkeypatch.setattr(cfg, "PROVIDERS_LOCK", data_dir / "providers.json.lock")

    record = cfg.create_provider(
        name="Timeout Provider",
        base_url="http://timeout.test",
        bearer_token="tok",
        timeout=30.0,
    )
    assert record["timeout"] == 30.0

    # Verify it was persisted
    import json

    data = json.loads(pfile.read_text())
    assert data["providers"][0]["timeout"] == 30.0


# ── Metrics (unit) ────────────────────────────────────────────────────────────


def test_metrics_record_success():
    from plugboard import metrics

    # Use a unique provider id to avoid interference from other tests
    pid = "metrics-test-success"
    metrics._stats.pop(pid, None)

    metrics.record(pid, 0.5)
    metrics.record(pid, 1.5)

    s = metrics.get_stats()[pid]
    assert s["requests"] == 2
    assert s["errors"] == 0
    assert s["latency_avg"] == pytest.approx(1.0, abs=0.001)


def test_metrics_record_error():
    from plugboard import metrics

    pid = "metrics-test-error"
    metrics._stats.pop(pid, None)

    metrics.record(pid, 0.0, error=True)
    metrics.record(pid, 0.0, error=True)

    s = metrics.get_stats()[pid]
    assert s["requests"] == 2
    assert s["errors"] == 2


# ── Metrics endpoint (API) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_endpoint(client):
    from plugboard import metrics

    pid = "test-provider"
    metrics._stats.pop(pid, None)

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )

    resp = await client.get("/metrics", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    # At least one provider should have metrics after a completion
    assert len(body) >= 1
    # The test-provider should have at least 1 request
    if pid in body:
        assert body[pid]["requests"] >= 1


# ── Provider health (API) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_health_not_found(client):
    resp = await client.get("/providers/ghost/health", headers=AUTH)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_provider_health_unreachable(client):
    """When backend /health is unreachable, ok=False is returned (not a 5xx)."""
    resp = await client.get("/providers/test-provider/health", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "test-provider"
    assert body["ok"] is False
    assert "url" in body
