"""Tests for the honest health check + watchdog jobs fix (commit fd8aa33).

Covers:
- /providers/{id}/health with _HEALTH_URL_OVERRIDES mapping
- ok = 200..399 (no more "OK 404")
- subprocess provider health (CLI binary + --version)
- /admin/jobs reports provider_watchdog as 'running' (not 'unknown')
"""

from __future__ import annotations

import pytest  # noqa: F401 — pytest fixtures rely on import
from aioresponses import aioresponses


# ── /providers/{id}/health — URL overrides + strict status ────────────────


async def test_health_ollama_uses_root_not_health(admin_client, monkeypatch):
    """Ollama has no /health — we must probe / instead and report ok=true."""
    # Create an ollama-shaped provider
    from plugboard.config import create_provider

    create_provider(
        name="Ollama Test",
        base_url="http://localhost:11434",
        bearer_token="",
        default_model="",
    )
    with aioresponses() as m:
        m.get("http://localhost:11434/", status=200, body="Ollama is running")
        res = await admin_client.get(
            "/providers/ollama-test/health",
            headers={"Authorization": "Bearer adminkey"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["status"] == 200
    assert data["url"].endswith("/")
    assert "/health" not in data["url"]


async def test_health_github_models_uses_catalog(admin_client):
    """GitHub Models has no /health — we must probe /catalog/models."""
    from plugboard.config import create_provider

    create_provider(
        name="GH Models Test",
        base_url="https://models.github.ai/inference",
        bearer_token="fake-token",
        default_model="openai/gpt-4o",
    )
    with aioresponses() as m:
        m.get(
            "https://models.github.ai/catalog/models",
            status=200,
            payload=[{"id": "openai/gpt-4o"}],
        )
        res = await admin_client.get(
            "/providers/gh-models-test/health",
            headers={"Authorization": "Bearer adminkey"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["status"] == 200
    assert data["url"] == "https://models.github.ai/catalog/models"


async def test_health_404_is_not_ok(admin_client):
    """Strict status code: 404 must not be reported as 'ok' (regression test)."""
    from plugboard.config import create_provider

    create_provider(
        name="Custom Test",
        base_url="https://custom.example.com/api",
        bearer_token="tok",
        default_model="",
    )
    with aioresponses() as m:
        m.get("https://custom.example.com/api/health", status=404, body="Not found")
        res = await admin_client.get(
            "/providers/custom-test/health",
            headers={"Authorization": "Bearer adminkey"},
        )
    data = res.json()
    assert data["status"] == 404
    assert data["ok"] is False  # <-- was True before fd8aa33


async def test_health_5xx_is_not_ok(admin_client):
    from plugboard.config import create_provider

    create_provider(
        name="Down Test",
        base_url="https://down.example.com",
        bearer_token="tok",
        default_model="",
    )
    with aioresponses() as m:
        m.get("https://down.example.com/health", status=503)
        res = await admin_client.get(
            "/providers/down-test/health",
            headers={"Authorization": "Bearer adminkey"},
        )
    data = res.json()
    assert data["ok"] is False
    assert data["status"] == 503


async def test_health_connection_refused_is_not_ok(admin_client):
    """Can't reach the host at all → ok=false + error in response."""
    from plugboard.config import create_provider

    create_provider(
        name="Unreachable Test",
        base_url="http://127.0.0.1:1",  # closed port
        bearer_token="",
        default_model="",
    )
    res = await admin_client.get(
        "/providers/unreachable-test/health",
        headers={"Authorization": "Bearer adminkey"},
    )
    data = res.json()
    assert data["ok"] is False
    assert "error" in data


async def test_health_subprocess_provider(admin_client, monkeypatch):
    """Subprocess providers bypass HTTP — they check the binary + --version."""
    from plugboard.config import create_provider
    from plugboard import cli_subprocess

    create_provider(
        name="Gemini Subproc Test",
        base_url="subprocess://gemini",
        bearer_token="",
        default_model="gemini-2.5-flash",
        auth_source="subprocess:gemini",
    )
    monkeypatch.setattr(
        cli_subprocess.shutil,
        "which",
        lambda cmd: "/fake/bin/gemini" if cmd == "gemini" else None,
    )

    async def fake_probe(kind, timeout_s=5.0):
        return {"ok": True, "version": "0.38.2", "error": None}

    monkeypatch.setattr(cli_subprocess, "probe_cli_version", fake_probe)

    res = await admin_client.get(
        "/providers/gemini-subproc-test/health",
        headers={"Authorization": "Bearer adminkey"},
    )
    data = res.json()
    assert data["ok"] is True
    assert data["status"] == "running"
    assert data["version"] == "0.38.2"
    assert data["url"] == "subprocess://gemini"


async def test_health_subprocess_cli_not_installed(admin_client, monkeypatch):
    """Subprocess provider with missing binary should report ok=false."""
    from plugboard.config import create_provider
    from plugboard import cli_subprocess

    create_provider(
        name="Codex Missing Test",
        base_url="subprocess://codex",
        bearer_token="",
        default_model="gpt-5.4",
        auth_source="subprocess:codex",
    )
    monkeypatch.setattr(cli_subprocess.shutil, "which", lambda cmd: None)
    res = await admin_client.get(
        "/providers/codex-missing-test/health",
        headers={"Authorization": "Bearer adminkey"},
    )
    data = res.json()
    assert data["ok"] is False
    assert "error" in data
    assert "codex" in data["error"]


# ── Watchdog status in /admin/jobs ─────────────────────────────────────────


async def test_watchdog_status_reports_running(admin_client, monkeypatch):
    """provider_watchdog should report 'running' (not 'unknown') when the task is alive."""
    from plugboard import provider_watchdog

    # Force get_watchdog_status() to return running=True
    def fake_status():
        return {"running": True, "providers": {}, "uptime_s": 42}

    monkeypatch.setattr(provider_watchdog, "get_watchdog_status", fake_status)

    res = await admin_client.get(
        "/admin/jobs",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    jobs = res.json()
    watchdog = next((j for j in jobs if j["id"] == "provider_watchdog"), None)
    assert watchdog is not None, "provider_watchdog missing from /admin/jobs"
    assert watchdog["last_status"] == "running"
    assert watchdog["next_run"] == "continuous"


async def test_watchdog_status_reports_stopped_when_inactive(admin_client, monkeypatch):
    """If the watchdog is not running, status should surface as 'stopped', not 'unknown'."""
    from plugboard import provider_watchdog

    def fake_status():
        return {"running": False, "providers": {}}

    monkeypatch.setattr(provider_watchdog, "get_watchdog_status", fake_status)

    res = await admin_client.get(
        "/admin/jobs",
        headers={"Authorization": "Bearer adminkey"},
    )
    watchdog = next(j for j in res.json() if j["id"] == "provider_watchdog")
    assert watchdog["last_status"] == "stopped"


async def test_scheduler_jobs_listed_with_a_known_state(admin_client):
    """The price and model refreshes are listed with a state the page can render."""
    res = await admin_client.get(
        "/admin/jobs",
        headers={"Authorization": "Bearer adminkey"},
    )
    jobs = {j["id"]: j for j in res.json()}
    for job_id in ("pricing_refresh", "model_refresh"):
        assert jobs[job_id]["last_status"] in (
            "success", "error", "never_run", "running", "stale", "disabled",
        )