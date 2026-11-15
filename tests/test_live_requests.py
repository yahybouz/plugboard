"""PLU-13 — a running chat call is visible on every surface (API, MCP, IHM).

A slow call and a hung call look the same from outside. These tests make a
call slow on purpose, look at it while it runs, then at its outcome.
"""

from __future__ import annotations

import asyncio

import pytest

from plugboard import live_requests as live


@pytest.fixture(autouse=True)
def _clean():
    live.reset()
    yield
    live.reset()


# ── module ──────────────────────────────────────────────────────────────────


def test_lifecycle_active_then_recent():
    live.begin("r1", "/v1/chat/completions", "mcp")
    live.update("r1", client_id="c1", model="p:m", provider="p")
    snap = live.snapshot(admin=True)
    assert snap["active_count"] == 1
    e = snap["active"][0]
    assert (e["id"], e["surface"], e["model"], e["phase"]) == (
        "r1",
        "mcp",
        "p:m",
        "running",
    )
    assert e["slow"] is False and e["provider"] == "p"

    live.set_status("r1", 200)
    live.finish("r1")
    snap = live.snapshot(admin=True)
    assert snap["active_count"] == 0
    assert snap["recent"][0]["outcome"] == "ok"
    assert snap["recent"][0]["phase"] == "done"


def test_status_set_after_finish_is_kept():
    """The middleware sees the status after the dependency has finished."""
    live.begin("r2", "/v1/chat/completions")
    live.finish("r2")
    live.set_status("r2", 502)
    assert live.snapshot(admin=True)["recent"][0]["outcome"] == "error"


def test_phase_change_resets_phase_timer():
    live.begin("r3", "/v1/chat/completions")
    live.update("r3", phase="cli_queue")
    live.update("r3", phase="cli_running", pid=4242)
    e = live.snapshot(admin=True)["active"][0]
    assert e["phase"] == "cli_running" and e["pid"] == 4242
    assert e["phase_elapsed_s"] <= e["elapsed_s"]


def test_slow_flag_and_stale_entries(monkeypatch):
    live.begin("slow", "/v1/chat/completions")
    live._active["slow"]["started_at"] -= live.SLOW_AFTER_S + 5
    assert live.snapshot(admin=True)["active"][0]["slow"] is True

    live._active["slow"]["started_at"] -= live._STALE_AFTER_S
    snap = live.snapshot(admin=True)
    assert snap["active_count"] == 0
    assert snap["recent"][0]["status"] == 499  # never answered: reported, not "running"


def test_non_admin_sees_only_own_calls_without_routing():
    live.begin("a", "/v1/chat/completions")
    live.update("a", client_id="alice", model="p:m", provider="secret-provider", pid=7)
    live.begin("b", "/v1/chat/completions")
    live.update("b", client_id="bob", model="p:m")
    snap = live.snapshot(client_id="alice")
    assert [e["id"] for e in snap["active"]] == ["a"]
    assert "provider" not in snap["active"][0] and "pid" not in snap["active"][0]
    assert live.snapshot(client_id=None)["active_count"] == 0
    assert live.snapshot(admin=True)["active_count"] == 2


def test_surface_is_a_closed_set():
    assert live.normalize_surface("MCP") == "mcp"
    assert live.normalize_surface("playground") == "playground"
    assert live.normalize_surface("<script>") == "api"
    assert live.normalize_surface(None) == "api"


def test_unknown_request_is_ignored():
    live.update("nope", phase="cli_running")
    live.finish("nope")
    live.begin("-", "/x")
    assert live.snapshot(admin=True)["active_count"] == 0


# ── API: a call made slow on purpose ─────────────────────────────────────────


async def _slow_chat(client, gate: asyncio.Event, surface: str | None, status=200):
    from aioresponses import aioresponses

    from tests.factories import FAKE_COMPLETION

    async def provider_answer(url, **kwargs):
        await gate.wait()
        from aioresponses import CallbackResult

        return CallbackResult(status=status, payload=FAKE_COMPLETION)

    headers = {"Authorization": "Bearer testkey"}
    if surface:
        headers[live.SURFACE_HEADER] = surface
    with aioresponses() as mock:
        mock.post("http://fake-llm.test/v1/chat/completions", callback=provider_answer)
        return await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 5,
                "cache": False,
            },
        )


async def _wait_active(client, n=1, tries=100):
    for _ in range(tries):
        r = await client.get(
            "/admin/inflight", headers={"Authorization": "Bearer testkey"}
        )
        if (
            r.status_code == 200
            and r.json()["active_count"] >= n
            and all(e["model"] for e in r.json()["active"])
        ):
            return r.json()
        await asyncio.sleep(0.02)
    raise AssertionError("call never showed as in flight")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["playground", "mcp", None])
async def test_api_slow_call_is_visible_then_reported(client, surface):
    gate = asyncio.Event()
    call = asyncio.create_task(_slow_chat(client, gate, surface))
    try:
        snap = await _wait_active(client)
        e = snap["active"][0]
        assert e["model"] == "test-provider:fake:model-v1"
        assert e["provider"] == "test-provider"
        assert e["surface"] == (surface or "api")
        assert e["phase"] == "running" and e["elapsed_s"] >= 0
    finally:
        gate.set()
    resp = await call
    assert resp.status_code == 200

    snap = (
        await client.get("/admin/inflight", headers={"Authorization": "Bearer testkey"})
    ).json()
    assert snap["active_count"] == 0
    assert snap["recent"][0]["outcome"] == "ok" and snap["recent"][0]["status"] == 200


@pytest.mark.asyncio
async def test_api_failed_call_is_reported_as_error(client):
    gate = asyncio.Event()
    gate.set()
    resp = await _slow_chat(client, gate, "api", status=500)
    assert resp.status_code >= 400
    snap = (
        await client.get("/admin/inflight", headers={"Authorization": "Bearer testkey"})
    ).json()
    assert snap["active_count"] == 0
    assert snap["recent"][0]["outcome"] == "error"


@pytest.mark.asyncio
async def test_api_rejected_overload_is_listed_as_error(client, monkeypatch):
    import plugboard.app as app_mod

    monkeypatch.setattr(app_mod, "_INFLIGHT_CHAT_MAX", 1)
    monkeypatch.setattr(app_mod, "_INFLIGHT_WAIT_S", 0.0)
    monkeypatch.setattr(app_mod, "_inflight_chat_sem", asyncio.Semaphore(1))
    await app_mod._get_inflight_sem().acquire()
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer testkey"},
            json={
                "model": "test-provider:fake:model-v1",
                "messages": [],
                "max_tokens": 1,
            },
        )
    finally:
        app_mod._get_inflight_sem().release()
    assert resp.status_code == 503
    snap = (
        await client.get("/admin/inflight", headers={"Authorization": "Bearer testkey"})
    ).json()
    assert (
        snap["recent"][0]["outcome"] == "error" and snap["recent"][0]["status"] == 503
    )


@pytest.mark.asyncio
async def test_inflight_endpoints_require_auth(client):
    assert (await client.get("/admin/inflight")).status_code in (401, 403)
    assert (await client.get("/v1/inflight")).status_code in (401, 403)


@pytest.mark.asyncio
async def test_client_key_endpoint_shows_own_calls_without_routing(client, monkeypatch):
    # A real tenant key (the fixture key is also the admin key).
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "adminkey-live")
    made = await client.post(
        "/admin/keys",
        headers={"Authorization": "Bearer adminkey-live"},
        json={"client_id": "tenant-a"},
    )
    assert made.status_code in (200, 201), made.text
    tenant_key = made.json()["key"]

    live.begin("mine", "/v1/chat/completions", "mcp")
    live.update(
        "mine", client_id="tenant-a", model="p:m", provider="hidden-provider", pid=99
    )
    live.begin("theirs", "/v1/chat/completions")
    live.update(
        "theirs", client_id="tenant-b", model="p:other", provider="hidden-provider"
    )

    r = await client.get(
        "/v1/inflight", headers={"Authorization": f"Bearer {tenant_key}"}
    )
    assert r.status_code == 200
    active = r.json()["active"]
    assert [e["id"] for e in active] == ["mine"]
    assert "hidden-provider" not in r.text and "tenant-b" not in r.text
    assert "pid" not in active[0] and "provider" not in active[0]

    # The tenant key cannot use the admin view.
    denied = await client.get(
        "/admin/inflight", headers={"Authorization": f"Bearer {tenant_key}"}
    )
    assert denied.status_code in (401, 403)


# ── MCP ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_tool_shows_running_and_finished_calls(monkeypatch):
    pytest.importorskip("mcp")
    from plugboard import mcp_server

    seen = []

    async def fake_get(path, admin=False):
        seen.append((path, admin))
        if path == "/v1/me":
            return {"client_id": "c1"}
        return {
            "active_count": 1,
            "active": [
                {
                    "model": "claude-via-claude-cli-1:claude-haiku-4-5",
                    "surface": "mcp",
                    "elapsed_s": 41.0,
                    "phase": "cli_running",
                    "slow": False,
                }
            ],
            "recent": [
                {
                    "model": "p:m",
                    "surface": "api",
                    "outcome": "error",
                    "status": 502,
                    "elapsed_s": 1.2,
                }
            ],
        }

    monkeypatch.setattr(mcp_server, "_api_get", fake_get)
    monkeypatch.setattr(mcp_server, "_API_KEY", "pb_synthetic_client")
    out = await mcp_server.inflight_requests()
    # The key's client first (an admin key is refused), then its calls;
    # always with the client key, never the admin key.
    assert seen == [("/v1/me", False), ("/v1/inflight", False)]
    assert "In flight:** 1" in out and "41.0s, cli_running" in out
    assert "error 502" in out


@pytest.mark.asyncio
async def test_mcp_requests_carry_the_mcp_surface_header(monkeypatch):
    pytest.importorskip("mcp")
    from plugboard import mcp_server

    monkeypatch.setattr(mcp_server, "_API_KEY", "k")
    monkeypatch.setattr(mcp_server, "_ADMIN_KEY", "a")
    assert mcp_server._auth_headers()[live.SURFACE_HEADER] == "mcp"


# ── CLI phases (a CLI call is the slow one) ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("agy_without_cage")  # fake agy outside bwrap
async def test_cli_call_reports_queue_run_and_parse_phases(monkeypatch, tmp_path):
    import json

    from plugboard import cli_subprocess
    from plugboard.logging_config import _request_id_var

    monkeypatch.setattr(cli_subprocess, "_binary_path_cache", {})
    monkeypatch.setattr(cli_subprocess.shutil, "which", lambda c: f"/fake/bin/{c}")
    monkeypatch.setenv("PLUGBOARD_SUBPROCESS_CWD", str(tmp_path))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "g"))

    gate = asyncio.Event()
    started = asyncio.Event()
    out = json.dumps({"response": "OK", "stats": {}}).encode()

    class _Proc:
        pid = 31337
        returncode = 0

        async def communicate(self, _stdin=None):
            started.set()
            await gate.wait()
            return out, b""

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(cli_subprocess.asyncio, "create_subprocess_exec", fake_exec)

    rid = "cli-rid"
    token = _request_id_var.set(rid)
    live.begin(rid, "/v1/chat/completions", "mcp")
    try:
        task = asyncio.create_task(
            cli_subprocess.complete(
                "agy", {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
            )
        )
        await asyncio.wait_for(started.wait(), 5)
        e = live.snapshot(admin=True)["active"][0]
        assert e["phase"] == "cli_running" and e["pid"] == 31337
        gate.set()
        try:
            await task
        except Exception:
            pass  # the fake answer's format is not the point; the phase is
        assert live.snapshot(admin=True)["active"][0]["phase"] in {
            "cli_parse",
            "cli_running",
        }
    finally:
        _request_id_var.reset(token)
        live.finish(rid)


def test_dashboard_polling_stays_under_the_per_address_rate_limit():
    """The gateway allows 60 requests a minute per address, for every route.

    Idle polling plus a running Playground call (ticker) must leave room for
    the dashboard's own calls, or the dashboard gets 429.
    """
    import re
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent / "static" / "live_requests.js"
    ).read_text()
    idle = int(re.search(r"var POLL_MS = (\d+);", src).group(1))
    busy = int(re.search(r"var BUSY_POLL_MS = (\d+);", src).group(1))
    per_minute = 60_000 / idle + 60_000 / busy
    assert per_minute <= 30, per_minute
