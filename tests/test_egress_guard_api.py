"""The ``egress_guard`` provider field and its API (PLU-7, D-18 c).

A manual provider is protected only when the admin ticks the box
(``egress_guard``); catalogue providers and built-in cloud presets are always
protected (``egress_guard_forced``). Ticking the box requires a public
http(s) ``base_url``: a refusal is audited as ``provider.egress.rejected``.
DNS answers are faked: no test resolves a real name.
"""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest
from aiohttp import web

from plugboard import app as app_mod
from plugboard import audit as audit_mod
from plugboard import config, netguard
from plugboard.llm_pricing import UnsafeURL
from tests.factories import PROVIDER_A, PROVIDER_OLLAMA

AUTH = {"Authorization": "Bearer testkey"}


@pytest.fixture
def private(monkeypatch):
    async def _resolve(host, port):
        raise UnsafeURL(f"{host} resolves to a non-public address (10.0.0.5)")

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _resolve)


@pytest.fixture
def public(monkeypatch):
    async def _resolve(host, port):
        return []

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _resolve)


@pytest.fixture
def no_lookup(monkeypatch):
    async def _resolve(host, port):
        pytest.fail(f"unexpected DNS lookup of {host}")

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _resolve)


@pytest.fixture
def audit_db(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")


@pytest.fixture
def guard_providers(_providers_file):
    """providers.json with one provider per kind of guard."""
    providers = [
        {
            **PROVIDER_A,
            "id": "catalog-x",
            "base_url": "https://api.acme.test/v1",
            "preset_key": "catalog-acme",
        },
        {
            **PROVIDER_A,
            "id": "openai",
            "base_url": "https://api.openai.com/v1",
            "preset_key": "openai",
        },
        {**PROVIDER_A, "id": "manual", "base_url": "https://manual.test/v1"},
        {
            **PROVIDER_A,
            "id": "guarded",
            "base_url": "https://guarded.test/v1",
            "egress_guard": True,
        },
        PROVIDER_OLLAMA,
    ]
    _providers_file.write_text(json.dumps({"providers": providers}))
    return _providers_file


def test_create_provider_stores_egress_guard_only_when_set(_empty_providers_file):
    record = config.create_provider(
        name="A", base_url="https://a.test", bearer_token="k"
    )
    assert "egress_guard" not in record
    guarded = config.create_provider(
        name="B", base_url="https://b.test", bearer_token="k", egress_guard=True
    )
    assert guarded["egress_guard"] is True
    assert config.get_provider("b")["egress_guard"] is True


async def test_post_guarded_provider_needs_public_url(client, private, audit_db):
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={
            "name": "Lan",
            "base_url": "http://10.0.0.5:8000",
            "bearer_token": "k",
            "egress_guard": True,
        },
    )
    assert r.status_code == 422
    assert "non-public" in r.json()["detail"]
    assert config.get_provider("lan") is None
    events = audit_mod.get_audit_log(action="provider.egress.rejected")
    assert len(events) == 1
    assert json.loads(events[0]["details"])["base_url"] == "http://10.0.0.5:8000"


async def test_post_guarded_provider_on_public_url(client, public, audit_db):
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={
            "name": "Pub",
            "base_url": "https://pub.test/v1",
            "bearer_token": "k",
            "egress_guard": True,
        },
    )
    assert r.status_code == 201
    assert r.json()["egress_guard"] is True
    assert r.json()["egress_guard_forced"] is False
    assert config.get_provider("pub")["egress_guard"] is True


async def test_post_unguarded_provider_makes_no_lookup(client, no_lookup, audit_db):
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={"name": "Lan2", "base_url": "http://10.0.0.6", "bearer_token": "k"},
    )
    assert r.status_code == 201
    assert r.json()["egress_guard"] is False
    assert "egress_guard" not in config.get_provider("lan2")


async def test_post_guard_on_non_http_url_is_422(client, no_lookup, audit_db):
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={
            "name": "S",
            "base_url": "subprocess://agy",
            "bearer_token": "k",
            "egress_guard": True,
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "The egress guard needs an http(s) base_url"
    assert config.get_provider("s") is None
    assert audit_mod.get_audit_log(action="provider.egress.rejected")


async def test_post_guarded_provider_unknown_host_is_503(client, monkeypatch, audit_db):
    async def _resolve(host, port):
        raise UnsafeURL(f"cannot resolve {host}")

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _resolve)
    r = await client.post(
        "/providers",
        headers=AUTH,
        json={
            "name": "Gone",
            "base_url": "https://gone.test/v1",
            "bearer_token": "k",
            "egress_guard": True,
        },
    )
    assert r.status_code == 503
    assert r.json()["detail"].startswith("Provider base_url host unreachable:")
    assert config.get_provider("gone") is None
    assert audit_mod.get_audit_log(action="provider.egress.rejected")


async def test_get_providers_exposes_effective_guard(client, guard_providers):
    r = await client.get("/providers", headers=AUTH)
    got = {p["id"]: (p["egress_guard"], p["egress_guard_forced"]) for p in r.json()}
    assert got == {
        "catalog-x": (True, True),
        "openai": (True, True),
        "manual": (False, False),
        "guarded": (True, False),
        "ollama": (False, False),
    }


# --- PUT /providers/{id}: editing a protected provider (D-19) ---------------


@pytest.fixture
def preset_key():
    """Preset of ``forced_provider``; a test overrides it by parametrizing."""
    return "catalog-acme"


@pytest.fixture
def forced_provider(_providers_file, preset_key):
    """A provider whose guard is imposed (catalogue or built-in cloud preset)."""
    providers = json.loads(_providers_file.read_text())["providers"]
    providers.append(
        {
            **PROVIDER_A,
            "id": "acme",
            "name": "Acme",
            "base_url": "https://api.acme.test/v1",
            "preset_key": preset_key,
        }
    )
    _providers_file.write_text(json.dumps({"providers": providers}))
    return "acme"


@pytest.fixture
def ollama_provider(_providers_file):
    providers = json.loads(_providers_file.read_text())["providers"]
    providers.append(PROVIDER_OLLAMA)
    _providers_file.write_text(json.dumps({"providers": providers}))
    return "ollama"


@pytest.fixture
def system_resolver(monkeypatch):
    """Answer ``localhost`` like the system resolver (127.0.0.1 and ::1).

    The real ``_resolve_public`` and its address check run; only the system
    lookup is faked, so no test sends a DNS query.
    """
    import socket

    def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if host != "localhost":
            raise socket.gaierror(socket.EAI_NONAME, f"unexpected lookup of {host}")
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)


@pytest.mark.parametrize("preset_key", ["catalog-acme", "openai"])
async def test_put_internal_base_url_on_forced_provider_is_422(
    client, private, forced_provider, preset_key, audit_db
):
    r = await client.put(
        f"/providers/{forced_provider}",
        headers=AUTH,
        json={"base_url": "http://10.0.0.5"},
    )
    assert r.status_code == 422
    assert "non-public" in r.json()["detail"]
    assert config.get_provider(forced_provider)["base_url"] == (
        "https://api.acme.test/v1"
    )
    events = audit_mod.get_audit_log(action="provider.egress.rejected")
    assert len(events) == 1
    assert events[0]["target_id"] == forced_provider
    assert json.loads(events[0]["details"])["base_url"] == "http://10.0.0.5"


async def test_put_unchanged_base_url_makes_no_lookup(
    client, no_lookup, forced_provider, audit_db
):
    r = await client.put(
        f"/providers/{forced_provider}",
        headers=AUTH,
        json={
            "base_url": "https://api.acme.test/v1/",
            "name": "Renamed",
            "egress_guard": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["egress_guard"] is True
    stored = config.get_provider(forced_provider)
    assert stored["name"] == "Renamed"
    assert "egress_guard" not in stored


async def test_put_cannot_turn_off_forced_guard(
    client, no_lookup, forced_provider, audit_db
):
    r = await client.put(
        f"/providers/{forced_provider}",
        headers=AUTH,
        json={"egress_guard": False},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "The egress guard is always on for catalog and built-in cloud presets"
    )
    assert "egress_guard" not in config.get_provider(forced_provider)


async def test_put_checking_guard_on_local_ollama_is_422(
    client, system_resolver, ollama_provider, audit_db
):
    r = await client.put(
        f"/providers/{ollama_provider}", headers=AUTH, json={"egress_guard": True}
    )
    assert r.status_code == 422
    assert "non-public" in r.json()["detail"]
    assert "egress_guard" not in config.get_provider(ollama_provider)
    assert audit_mod.get_audit_log(action="provider.egress.rejected")


async def test_put_manual_guard_toggle(client, public, monkeypatch, audit_db):
    r = await client.put(
        "/providers/test-provider", headers=AUTH, json={"egress_guard": True}
    )
    assert r.status_code == 200
    assert r.json()["egress_guard"] is True
    assert r.json()["egress_guard_forced"] is False
    assert config.get_provider("test-provider")["egress_guard"] is True

    r = await client.put(
        "/providers/test-provider", headers=AUTH, json={"egress_guard": False}
    )
    assert r.status_code == 200
    assert config.get_provider("test-provider")["egress_guard"] is False

    async def _fail(host, port):
        pytest.fail("no lookup once the guard is off")

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _fail)
    r = await client.put(
        "/providers/test-provider", headers=AUTH, json={"base_url": "http://10.0.0.5"}
    )
    assert r.status_code == 200
    assert config.get_provider("test-provider")["base_url"] == "http://10.0.0.5"


async def test_put_guarded_manual_provider_to_internal_url_is_422(
    client, private, audit_db, _providers_file
):
    providers = json.loads(_providers_file.read_text())["providers"]
    providers[0]["egress_guard"] = True
    _providers_file.write_text(json.dumps({"providers": providers}))
    r = await client.put(
        "/providers/test-provider", headers=AUTH, json={"base_url": "http://10.0.0.5"}
    )
    assert r.status_code == 422
    assert config.get_provider("test-provider")["base_url"] == "http://fake-llm.test"


async def test_put_unknown_provider_is_404_without_lookup(client, no_lookup):
    r = await client.put("/providers/nope", headers=AUTH, json={"egress_guard": True})
    assert r.status_code == 404


# --- Connection test of a protected preset ----------------------------------


@pytest.fixture
def closed_port() -> int:
    """A local port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def redirecting_server():
    """A local server whose answers redirect to an internal path.

    ``/v1``: ``GET /models`` answers 302 to ``/internal/models``.
    ``/ok/v1``: ``GET /models`` lists a model, ``POST /chat/completions``
    answers 302 to ``/internal/chat``. Every request to ``/internal/...`` is
    counted in ``internal_calls``.
    """
    state = SimpleNamespace(internal_calls=0, port=0)

    async def redirect_models(request: web.Request) -> web.Response:
        raise web.HTTPFound("/internal/models")

    async def redirect_chat(request: web.Request) -> web.Response:
        raise web.HTTPFound("/internal/chat")

    async def models(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m"}]})

    async def internal(request: web.Request) -> web.Response:
        state.internal_calls += 1
        return web.json_response({"data": [{"id": "internal"}]})

    app = web.Application()
    app.router.add_get("/v1/models", redirect_models)
    app.router.add_get("/ok/v1/models", models)
    app.router.add_post("/ok/v1/chat/completions", redirect_chat)
    app.router.add_route("*", "/internal/{tail:.*}", internal)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.port = site.port
    try:
        yield state
    finally:
        await runner.cleanup()


async def test_protected_connection_test_does_not_follow_redirects(
    redirecting_server, monkeypatch
):
    monkeypatch.setattr(netguard, "ip_is_public", lambda address: True)
    r = await app_mod._test_openai_endpoint(
        f"http://127.0.0.1:{redirecting_server.port}/v1", "k", False, public_only=True
    )
    assert r["status"] == "not_openai_compatible"
    assert "HTTP 302" in r["message"]
    assert redirecting_server.internal_calls == 0


async def test_protected_test_token_does_not_follow_redirects(
    redirecting_server, monkeypatch
):
    monkeypatch.setattr(netguard, "ip_is_public", lambda address: True)
    r = await app_mod._test_openai_endpoint(
        f"http://127.0.0.1:{redirecting_server.port}/ok/v1",
        "k",
        True,
        public_only=True,
    )
    assert r["status"] == "ok"
    assert r["completion_status"] == 302 and r["completion_ok"] is False
    assert redirecting_server.internal_calls == 0


async def test_protected_connection_test_reports_egress_refused(closed_port):
    r = await app_mod._test_openai_endpoint(
        f"http://127.0.0.1:{closed_port}/v1", "k", False, public_only=True
    )
    assert r["status"] == "egress_refused"
    assert r["message"] == "Refused: 127.0.0.1 resolves to a non-public address."
    assert r["docs"].endswith("#test-the-connection")


async def test_direct_test_is_public_only_for_presets(client, monkeypatch):
    seen = []

    async def fake(*args, **kwargs):
        seen.append(kwargs.get("public_only"))
        return {"status": "ok"}

    # By name: the module the client serves, which a test that re-imports
    # plugboard.app (test_audit_e2e) makes another object than app_mod.
    monkeypatch.setattr("plugboard.app._test_openai_endpoint", fake)
    await client.post(
        "/admin/direct-providers/test",
        headers=AUTH,
        json={"preset_key": "openai", "api_key": "k"},
    )
    await client.post(
        "/admin/direct-providers/test",
        headers=AUTH,
        json={"base_url": "https://custom.test/v1", "api_key": "k"},
    )
    assert seen == [True, False]
