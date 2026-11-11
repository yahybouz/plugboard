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

import json
import socket
from types import SimpleNamespace

import pytest
from aiohttp import web

from plugboard import alerts, circuit_breaker, metrics, netguard, router
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
