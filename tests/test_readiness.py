"""GET /health/ready: whether this instance can serve requests (PLU-31).

Blocking checks: the usage database takes a write lock, every active CLI
provider has its binary, client auth is on (or the instance is in dev mode).
Ollama and the background tasks are reported but never block. The route is
public; its ``checks`` are shown to a valid key only (PLU-75, see
tests/test_readiness_visibility.py): generic states, never a provider id, a
path or an error text.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from plugboard import cli_subprocess, config, provider_watchdog, readiness, usage
from tests.factories import PROVIDER_A

CLI_PROVIDER = {
    "id": "cli-missing-7f3",
    "name": "CLI missing",
    "base_url": "subprocess://gemini",
    "bearer_token": "",
    "auth_source": "subprocess:gemini",
    "default_model": "gemini-2.5-pro",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}

OLLAMA_PROVIDER = {
    "id": "local-llm-7f3",
    "name": "Local models",
    "base_url": "http://localhost:11434",
    "bearer_token": "",
    "default_model": "llama3",
    "is_active": True,
    "provider_type": "sovereign",
    "created_at": "2026-01-01T00:00:00+00:00",
}

# The admin key of the `client` fixture: /health/ready shows its checks to a
# valid key only (PLU-75).
KEY = {"Authorization": "Bearer testkey"}

MISSING_CLI = {
    "ok": False,
    "error": "'gemini' not found on PATH. Install it and retry.",
}


def _watchdog(running=True, interval=30, providers=None):
    return {
        "running": running,
        "check_interval_s": interval,
        "providers": providers or {},
    }


def _state(healthy=True, ago=10.0):
    return {"is_healthy": healthy, "last_check_ago_s": ago}


def _job(running=False, disabled=False):
    return SimpleNamespace(status_dict={"running": running, "disabled": disabled})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Own usage database, a healthy watchdog, and an empty result cache."""
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    monkeypatch.setattr(provider_watchdog, "get_watchdog_status", lambda: _watchdog())
    readiness.reset_cache()
    yield
    readiness.reset_cache()


def _write_providers(pfile, providers):
    pfile.write_text(json.dumps({"providers": providers}))


# ── checks, one by one ──────────────────────────────────────────────────────


def test_cli_state_fails_when_an_active_provider_misses_its_binary(monkeypatch):
    monkeypatch.setattr(cli_subprocess, "check_cli_installed", lambda kind: MISSING_CLI)
    assert readiness.cli_state([PROVIDER_A, CLI_PROVIDER]) == "fail"


def test_cli_state_ignores_an_inactive_cli_provider(monkeypatch):
    monkeypatch.setattr(cli_subprocess, "check_cli_installed", lambda kind: MISSING_CLI)
    inactive = {**CLI_PROVIDER, "is_active": False}
    assert readiness.cli_state([PROVIDER_A, inactive]) == "none"


def test_cli_state_ok_when_every_binary_is_present(monkeypatch):
    seen = []

    def installed(kind):
        seen.append(kind)
        return {"ok": True, "binary": f"/usr/bin/{kind}", "kind": kind}

    monkeypatch.setattr(cli_subprocess, "check_cli_installed", installed)
    codex = {**CLI_PROVIDER, "id": "codex-7f3", "auth_source": "subprocess:codex"}
    assert readiness.cli_state([PROVIDER_A, CLI_PROVIDER, codex]) == "ok"
    assert sorted(seen) == ["codex", "gemini"]


@pytest.mark.parametrize(
    "api_key, dev_mode, expected",
    [("secret", False, "ok"), ("", True, "dev"), ("", False, "fail")],
)
def test_auth_state(api_key, dev_mode, expected):
    assert readiness.auth_state(api_key, dev_mode) == expected


@pytest.mark.parametrize(
    "providers, watchdog, expected",
    [
        ([PROVIDER_A], _watchdog(), "not_configured"),
        (
            [OLLAMA_PROVIDER],
            _watchdog(providers={"local-llm-7f3": _state(True)}),
            "ok",
        ),
        (
            [OLLAMA_PROVIDER],
            _watchdog(providers={"local-llm-7f3": _state(False)}),
            "unreachable",
        ),
        (
            [OLLAMA_PROVIDER],
            _watchdog(interval=30, providers={"local-llm-7f3": _state(True, 200)}),
            "unknown",
        ),
        (
            [OLLAMA_PROVIDER],
            _watchdog(running=False, providers={"local-llm-7f3": _state(True)}),
            "unknown",
        ),
    ],
    ids=["not-configured", "healthy", "down", "stale-check", "watchdog-stopped"],
)
def test_ollama_state(providers, watchdog, expected):
    assert readiness.ollama_state(providers, watchdog) == expected


def test_background_state_ok_when_watchdog_and_jobs_run():
    state = SimpleNamespace(
        availability_job=_job(running=True), retention_job=_job(disabled=True)
    )
    assert readiness.background_state(state, _watchdog()) == "ok"


def test_background_state_degraded_without_the_jobs():
    assert readiness.background_state(SimpleNamespace(), _watchdog()) == "degraded"


def test_usage_db_state_ok_on_a_new_database():
    assert readiness.usage_db_state() == "ok"


def test_usage_db_state_fails_when_the_parent_is_a_file(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setattr(usage, "_DB_PATH", blocker / "usage.db")
    assert readiness.usage_db_state() == "fail"


# ── the route ───────────────────────────────────────────────────────────────


async def test_ollama_unreachable_does_not_block(client, _providers_file, monkeypatch):
    _write_providers(_providers_file, [PROVIDER_A, OLLAMA_PROVIDER])
    monkeypatch.setattr(
        provider_watchdog,
        "get_watchdog_status",
        lambda: _watchdog(providers={"local-llm-7f3": _state(False)}),
    )
    resp = await client.get("/health/ready", headers=KEY)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"]["ollama"] == "unreachable"
    assert (await client.get("/health")).status_code == 200


async def test_missing_cli_gives_503(client, _providers_file, monkeypatch):
    _write_providers(_providers_file, [PROVIDER_A, CLI_PROVIDER])
    monkeypatch.setattr(cli_subprocess, "check_cli_installed", lambda kind: MISSING_CLI)
    resp = await client.get("/health/ready", headers=KEY)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"]["cli"] == "fail"


async def test_unwritable_usage_db_gives_503(client, monkeypatch):
    def unwritable():
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(usage, "_get_conn", unwritable)
    resp = await client.get("/health/ready", headers=KEY)
    assert resp.status_code == 503, resp.text
    assert resp.json()["checks"]["usage_db"] == "fail"


async def test_auth_off_gives_503_except_in_dev_mode(client, monkeypatch):
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    monkeypatch.setattr("plugboard.app._DEV_MODE", False)
    resp = await client.get("/health/ready", headers=KEY)
    assert resp.status_code == 503, resp.text
    assert resp.json()["checks"]["auth"] == "fail"

    readiness.reset_cache()
    monkeypatch.setattr("plugboard.app._DEV_MODE", True)
    resp = await client.get("/health/ready", headers=KEY)
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"]["auth"] == "dev"


async def test_body_names_no_provider_and_no_error_text(
    client, _providers_file, monkeypatch
):
    _write_providers(_providers_file, [PROVIDER_A, CLI_PROVIDER, OLLAMA_PROVIDER])
    monkeypatch.setattr(cli_subprocess, "check_cli_installed", lambda kind: MISSING_CLI)
    monkeypatch.setattr(
        provider_watchdog,
        "get_watchdog_status",
        lambda: _watchdog(providers={"local-llm-7f3": _state(False)}),
    )
    resp = await client.get("/health/ready", headers=KEY)
    assert resp.status_code == 503
    text = resp.text
    for leak in (
        "test-provider",
        "cli-missing-7f3",
        "local-llm-7f3",
        "gemini",
        "11434",
        "not found on PATH",
    ):
        assert leak not in text, leak
    body = resp.json()
    assert set(body) == {"ready", "version", "checks"}
    assert set(body["checks"]) == {"usage_db", "cli", "auth", "ollama", "background"}


async def test_result_is_cached_for_five_seconds(client, monkeypatch):
    calls = []
    real = readiness.usage_db_state

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(readiness, "usage_db_state", counting)
    for _ in range(3):
        assert (await client.get("/health/ready")).status_code == 200
    assert len(calls) == 1
    assert readiness.CACHE_TTL_S == 5.0

    readiness.reset_cache()
    assert (await client.get("/health/ready")).status_code == 200
    assert len(calls) == 2


async def test_concurrent_calls_share_one_evaluation(_providers_file, monkeypatch):
    calls = []

    def slow_ok():
        calls.append(1)
        time.sleep(0.05)
        return "ok"

    monkeypatch.setattr(readiness, "usage_db_state", slow_ok)
    results = await asyncio.gather(
        *(
            readiness.evaluate(SimpleNamespace(), api_key="k", dev_mode=False)
            for _ in range(10)
        )
    )
    assert len(calls) == 1
    assert all(r == results[0] for r in results)
    assert results[0]["ready"] is True


async def test_a_check_that_raises_gives_503_not_500(client, monkeypatch):
    def broken():
        raise RuntimeError("boom /srv/secret/providers.json")

    monkeypatch.setattr(config, "load_providers", broken)
    resp = await client.get("/health/ready", headers=KEY)
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["checks"]["cli"] == "fail"
    assert body["checks"]["ollama"] == "unknown"
    assert "boom" not in resp.text
    assert "/srv/secret" not in resp.text


def test_contract_503_of_ready_is_the_ready_body():
    from plugboard import api_contract

    paths = api_contract.build_openapi()["paths"]
    ready = paths["/health/ready"]["get"]["responses"]
    assert ready["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Ready"
    }
    assert paths["/health/ready"]["get"]["security"] == []
    other = paths["/v1/preflight"]["post"]["responses"]["503"]
    assert other["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
