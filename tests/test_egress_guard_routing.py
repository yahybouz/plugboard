"""Routing of protected providers through the egress guard (PLU-7).

A protected provider (catalogue, built-in cloud preset, manual with the box
ticked) reaches its base URL through the public-only session: a connection to
a non-public address is refused before any socket is opened. The refusal is
the gateway's decision, not the provider's failure: it hands over to the next
provider, never trips the circuit breaker nor the provider_down alert, and is
reported once per window (``netguard.report_refusal``). An admin gets the
refused host; a client key gets a reference to the gateway log only.
"""

from __future__ import annotations

import asyncio
import json
import socket
from types import SimpleNamespace

import pytest
from aiohttp import web

from plugboard import (
    alerts,
    circuit_breaker,
    config,
    health_probe,
    http_pool,
    metrics,
    model_availability,
    netguard,
    ollama,
    provider_watchdog,
    router,
    semantic_cache,
)
from tests.factories import FAKE_COMPLETION, PROVIDER_A

_CHAT = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def _provider(provider_id: str, port: int, *, protected: bool) -> dict:
    provider = {**PROVIDER_A, "id": provider_id, "base_url": f"http://127.0.0.1:{port}"}
    if protected:
        provider["preset_key"] = f"catalog-{provider_id}"  # a catalogue provider
    return provider


@pytest.fixture
def closed_port() -> int:
    """A local port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def upstream():
    """A local server counting ``POST /v1/chat/completions``."""
    state = SimpleNamespace(calls=0, port=0)

    async def completions(request: web.Request) -> web.Response:
        state.calls += 1
        return web.json_response(FAKE_COMPLETION)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.port = site.port
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest.fixture
def acme(upstream) -> dict:
    """A protected (catalogue) provider whose base URL is the local upstream."""
    return _provider("acme", upstream.port, protected=True)


@pytest.fixture
def plain(upstream) -> dict:
    """An unprotected provider on the same local upstream."""
    return _provider("plain", upstream.port, protected=False)


@pytest.fixture
def client_key(tmp_path, monkeypatch):
    """Authorization header of a plain client key (not the admin)."""
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    rec = ak.create_key("egress-guard-client", "test")
    return {"Authorization": f"Bearer {rec['key']}"}


@pytest.fixture
def recorders(monkeypatch):
    """Record breaker failures, provider_down counts and refusal reports.

    The process-wide counters a forward touches are isolated too.
    """
    rec = SimpleNamespace(cb_failures=[], provider_failures=[], reports=[])

    async def provider_failure(provider_id: str) -> None:
        rec.provider_failures.append(provider_id)

    def report(provider_id: str, host: str, source: str) -> bool:
        rec.reports.append((provider_id, host, source))
        return True

    monkeypatch.setattr(circuit_breaker, "record_failure", rec.cb_failures.append)
    monkeypatch.setattr(router, "record_provider_failure", provider_failure)
    monkeypatch.setattr(netguard, "report_refusal", report)
    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    monkeypatch.setattr(metrics, "_stats", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    return rec


@pytest.fixture
def protected_unreachable(_providers_file, closed_port):
    """providers.json = [acme, protected, on a closed port of 127.0.0.1]."""
    _providers_file.write_text(
        json.dumps({"providers": [_provider("acme", closed_port, protected=True)]})
    )
    router.invalidate_models_cache()
    return closed_port


async def test_forward_falls_back_after_refusal(
    upstream, acme, plain, monkeypatch, recorders
):
    monkeypatch.setattr(
        router, "_resolve_all_providers", lambda m: [(acme, "m"), (plain, "m")]
    )
    result = await router.forward(_CHAT)
    assert result == FAKE_COMPLETION and upstream.calls == 1
    assert router.served_by()[0]["id"] == "plain"
    assert recorders.cb_failures == [] and recorders.provider_failures == []
    assert recorders.reports == [("acme", "127.0.0.1", "forward")]


async def test_refusal_gives_502_admin_text_and_client_reference(
    client, client_key, protected_unreachable, recorders
):
    chat = {**_CHAT, "model": "acme:fake:model-v1", "cache": False}
    admin = await client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer testkey"}, json=chat
    )
    assert admin.status_code == 502, admin.text
    assert "egress refused: 127.0.0.1" in admin.json()["detail"]
    user = await client.post("/v1/chat/completions", headers=client_key, json=chat)
    body = user.json()
    assert user.status_code == 502, user.text
    assert "egress" not in body["detail"] and "127.0.0.1" not in body["detail"]
    assert body["error_ref"] in body["detail"]
    assert recorders.cb_failures == [] and recorders.provider_failures == []
    assert [r[:2] for r in recorders.reports] == [("acme", "127.0.0.1")] * 2


async def test_stream_refusal_gives_502_without_breaker(
    client, client_key, protected_unreachable, recorders
):
    chat = {**_CHAT, "model": "acme:fake:model-v1", "cache": False, "stream": True}
    resp = await client.post("/v1/chat/completions", headers=client_key, json=chat)
    assert resp.status_code == 502, resp.text
    assert "127.0.0.1" not in resp.text and "egress" not in resp.text
    assert recorders.cb_failures == [] and recorders.provider_failures == []
    assert recorders.reports == [("acme", "127.0.0.1", "stream")]


async def test_discovery_paths_use_session_for(acme, monkeypatch):
    seen = []

    class _Called(Exception):
        pass

    async def fake(provider):
        seen.append(provider["id"])
        raise _Called

    monkeypatch.setattr(http_pool, "session_for", fake)
    with pytest.raises(_Called):
        await router.discover_models(acme)
    entries, reason = await model_availability.discover_openai_models(acme)
    assert entries is None and reason.startswith("GET /models failed")
    assert seen == ["acme", "acme"]


async def test_probe_http_public_only_refuses_loopback(closed_port):
    url = f"http://127.0.0.1:{closed_port}/health"
    r = await health_probe._probe_http(url, {}, public_only=True)
    assert r["ok"] is False and r["error"] == "egress_refused"
    assert r["host"] == "127.0.0.1" and r["url"] == url and "latency_ms" in r


async def test_probe_provider_passes_public_only_only_when_protected(
    monkeypatch, recorders
):
    calls = []

    async def http(url, headers, **kw):
        calls.append(kw)
        return {"ok": False, "error": "egress_refused", "host": "h"}

    monkeypatch.setattr(health_probe, "_probe_http", http)
    await health_probe.probe_provider(
        {"id": "acme", "preset_key": "catalog-acme", "base_url": "https://a.test"}
    )
    await health_probe.probe_provider({"id": "plain", "base_url": "https://p.test"})
    assert calls == [{"public_only": True}, {}]
    assert recorders.reports == [("acme", "h", "health")]


async def test_shallow_health_public_only_refuses_loopback(closed_port):
    r = await provider_watchdog._shallow_health(
        f"http://127.0.0.1:{closed_port}", public_only=True
    )
    assert r["ok"] is False and r["error"] == "egress_refused"
    assert r["host"] == "127.0.0.1"


async def test_sovereign_health_public_only_refuses_loopback(closed_port):
    r = await provider_watchdog._sovereign_health(
        f"http://127.0.0.1:{closed_port}", public_only=True
    )
    assert r["error"] == "egress_refused" and r["host"] == "127.0.0.1"


async def test_deep_health_public_only_refuses_loopback(closed_port):
    r = await provider_watchdog._deep_health(
        f"http://127.0.0.1:{closed_port}", "", "m", public_only=True
    )
    assert r["error"] == "egress_refused" and r["host"] == "127.0.0.1"


async def test_ping_provider_guards_only_protected(monkeypatch, closed_port):
    seen = []

    async def shallow(base_url, *, public_only=False):
        seen.append(public_only)
        return {"ok": False, "error": "down", "latency_ms": 0.0}

    providers = {
        "acme": _provider("acme", closed_port, protected=True),
        "plain": _provider("plain", closed_port, protected=False),
    }
    monkeypatch.setattr(provider_watchdog, "get_provider", providers.get)
    monkeypatch.setattr(provider_watchdog, "_shallow_health", shallow)
    await provider_watchdog.ping_provider("acme")
    await provider_watchdog.ping_provider("plain")
    assert seen == [True, False]


async def test_watchdog_skips_restart_on_egress_refusal(
    monkeypatch, tmp_path, closed_port, recorders
):
    wd = provider_watchdog
    monkeypatch.setattr(wd, "_DB_PATH", tmp_path / "watchdog.db")
    monkeypatch.setattr(wd, "_states", {})
    monkeypatch.setattr(wd, "_running", False)
    monkeypatch.setattr(wd, "_CHECK_INTERVAL", 0)
    monkeypatch.setattr(wd, "_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(
        wd, "load_providers", lambda: [_provider("acme", closed_port, protected=True)]
    )

    async def ping(pid):
        wd._running = False  # one pass of the loop
        return {"ok": False, "error": "egress_refused", "host": "h", "check": "shallow"}

    async def provider_down(pid):
        pytest.fail("an egress refusal must not raise provider_down")

    def restart(pid):
        pytest.fail("an egress refusal must not restart the provider")

    monkeypatch.setattr(wd, "ping_provider", ping)
    monkeypatch.setattr(wd, "restart_provider", restart)
    monkeypatch.setattr(alerts, "record_provider_failure", provider_down)
    await wd._watchdog_loop()
    assert recorders.reports == [("acme", "h", "watchdog")]
    assert wd._states["acme"].consecutive_failures == 1


async def test_embed_refusal_is_reported(monkeypatch, closed_port, recorders):
    acme = _provider("acme", closed_port, protected=True)
    monkeypatch.setattr(semantic_cache, "embed_is_enabled", lambda: True)
    monkeypatch.setenv("PLUGBOARD_EMBED_PROVIDER_ID", "acme")
    monkeypatch.setenv("PLUGBOARD_EMBED_MODEL", "e")
    monkeypatch.setattr(config, "get_provider", {"acme": acme}.get)
    assert await semantic_cache._embed("hello") is None
    assert recorders.reports == [("acme", "127.0.0.1", "embed")]


@pytest.fixture
async def ollama_upstream(monkeypatch):
    """A local Ollama counting ``POST /api/generate``, named ``ollama.test``.

    Simulated DNS: ``ollama.test`` answers the loopback address (a public name
    later pointed inside, DNS rebinding); any other name fails, so the test
    never reaches a real resolver.
    """
    state = SimpleNamespace(calls=0, port=0)

    async def generate(request: web.Request) -> web.Response:
        state.calls += 1
        return web.json_response({"response": "OK"})

    async def getaddrinfo(host, port, **kw):
        if host != "ollama.test":
            raise OSError(f"no DNS in tests: {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    async def installed():
        return [{"name": "llama3:latest"}]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(ollama, "list_models", installed)
    app = web.Application()
    app.router.add_post("/api/generate", generate)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.port = site.port
    try:
        yield state
    finally:
        await runner.cleanup()


def _ollama_lab(port: int, *, ticked: bool) -> dict:
    """A manual sovereign provider the connectivity probe treats as Ollama."""
    provider = {
        "id": "ollama-lab",
        "name": "ollama-lab",
        "provider_type": "sovereign",
        "base_url": f"http://ollama.test:{port}",
        "models": [{"id": "llama3:latest"}],
    }
    if ticked:
        provider["egress_guard"] = True
    return provider


async def test_ollama_probe_guards_only_protected(ollama_upstream, recorders):
    probe = model_availability._probe_ollama_provider
    ticked = _ollama_lab(ollama_upstream.port, ticked=True)
    assert netguard.is_protected(ticked)
    assert model_availability._is_ollama_provider_dict(ticked)

    refused = await probe(ticked, kinds={"ollama"}, now_str="t", timeout_s=5)
    entry = refused["ollama-lab:llama3:latest"]
    assert entry["connected"] is False and entry["reason"] == "egress_refused"
    assert ollama_upstream.calls == 0
    assert recorders.reports == [("ollama-lab", "ollama.test", "probe")]

    plain = _ollama_lab(ollama_upstream.port, ticked=False)
    reached = await probe(plain, kinds={"ollama"}, now_str="t", timeout_s=5)
    assert reached["ollama-lab:llama3:latest"]["connected"] is True
    assert ollama_upstream.calls == 1
