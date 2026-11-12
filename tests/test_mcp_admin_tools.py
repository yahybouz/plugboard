"""MCP admin tools (PB-MCP-02): add_provider and refresh_catalog.

stdio only, behind PLUGBOARD_MCP_ADMIN_TOOLS=1 and PLUGBOARD_ADMIN_KEY. The
tools go through the gateway's admin routes, so they get its validation and
its audit. Keys, hosts and providers are synthetic; DNS is never resolved.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging

import pytest
from aiohttp import web

pytest.importorskip("mcp")

from plugboard import mcp_server  # noqa: E402
from plugboard.llm_pricing import UnsafeURL  # noqa: E402

PROVIDER_KEY = "sk-synthetic-provider-key"


@pytest.fixture
def admin_on(monkeypatch):
    """Admin tools enabled over stdio; fresh limits; a clock the test moves."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(mcp_server, "_ADMIN_TOOLS", True)
    monkeypatch.setattr(mcp_server, "_ADMIN_KEY", "adminkey")
    monkeypatch.setattr(mcp_server, "_NETWORK_TRANSPORT", False)
    monkeypatch.setattr(mcp_server, "_create_calls", collections.deque())
    monkeypatch.setattr(mcp_server, "_refresh_last", {})
    monkeypatch.setattr(mcp_server, "_refresh_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_server, "_clock", lambda: clock["now"])
    return clock


@pytest.fixture
async def fake_gateway(monkeypatch):
    """Loopback aiohttp gateway: records the requests, answers {"ok": true}."""
    state = {"headers": {}, "hits": [], "raw": [], "redirect": None, "respond": None}

    async def handle(request):
        state["hits"].append(request.path)
        state["raw"].append((request.method, request.raw_path))
        state["headers"] = dict(request.headers)
        if state["redirect"]:
            raise web.HTTPFound(state["redirect"])
        if state["respond"]:
            return await state["respond"](request)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(mcp_server, "_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(mcp_server, "_HTTP_SESSION", None)
    try:
        yield state
    finally:
        if mcp_server._HTTP_SESSION is not None:
            await mcp_server._HTTP_SESSION.close()
        await runner.cleanup()


# ── Task 1: gate, admin client, limits ──────────────────────────────────────


def test_gate_refuses_network_first(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_NETWORK_TRANSPORT", True)
    monkeypatch.setattr(mcp_server, "_ADMIN_TOOLS", False)
    with pytest.raises(mcp_server.AdminToolError, match="only loaded for stdio"):
        mcp_server._admin_gate("add_provider")


def test_gate_refuses_without_switch(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ADMIN_TOOLS", False)
    with pytest.raises(mcp_server.AdminToolError, match="PLUGBOARD_MCP_ADMIN_TOOLS=1"):
        mcp_server._admin_gate("add_provider")


def test_gate_refuses_without_admin_key(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ADMIN_KEY", "")
    with pytest.raises(
        mcp_server.AdminToolError, match="add_provider needs PLUGBOARD_ADMIN_KEY"
    ):
        mcp_server._admin_gate("add_provider")


def test_gate_passes_with_switch_and_key_over_stdio(admin_on):
    mcp_server._admin_gate("refresh_catalog")


async def test_admin_request_sends_admin_key_and_mcp_surface(admin_on, fake_gateway):
    assert await mcp_server._admin_request("POST", "/providers", {"a": 1}) == (
        200,
        {"ok": True},
    )
    assert fake_gateway["headers"]["Authorization"] == "Bearer adminkey"
    assert fake_gateway["headers"]["X-Plugboard-Surface"] == "mcp"


async def test_admin_request_does_not_follow_redirects(admin_on, fake_gateway):
    fake_gateway["redirect"] = "/elsewhere"
    status, _ = await mcp_server._admin_request("GET", "/providers")
    assert status == 302 and fake_gateway["hits"] == ["/providers"]


async def test_admin_request_unreachable_gateway(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(mcp_server, "_HTTP_SESSION", None)
    try:
        with pytest.raises(
            mcp_server.AdminToolError, match=r"could not reach the gateway \("
        ):
            await mcp_server._admin_request("GET", "/providers")
    finally:
        if mcp_server._HTTP_SESSION is not None:
            await mcp_server._HTTP_SESSION.close()


def test_gateway_error_messages():
    mfa = {"detail": "Admin MFA required: sign in, or send X-Plugboard-Admin-TOTP"}
    assert mcp_server._gateway_error(401, mfa) == (
        "the gateway enforces admin MFA: MCP admin tools cannot sign in, "
        "use the dashboard"
    )
    assert (
        mcp_server._gateway_error(401, {"detail": "Admin key required"})
        == "the gateway refused PLUGBOARD_ADMIN_KEY"
    )
    assert (
        mcp_server._gateway_error(422, {"detail": [{"loc": ["x"]}]})
        == 'HTTP 422: [{"loc": ["x"]}]'
    )
    long = mcp_server._gateway_error(400, {"detail": "x" * 1000})
    assert long == "HTTP 400: " + "x" * 300


def test_scrub_removes_every_copy_of_the_key():
    assert mcp_server._scrub("a sk-k b sk-k", "sk-k") == "a *** b ***"
    assert mcp_server._scrub("abc", "") == "abc"


def test_sixth_creation_in_an_hour_is_refused(admin_on):
    for _ in range(5):
        mcp_server._take_create_slot()
    with pytest.raises(
        mcp_server.AdminToolError,
        match=r"^add_provider limit reached \(5 per hour\): retry in 3600s$",
    ):
        mcp_server._take_create_slot()
    admin_on["now"] += 3601
    mcp_server._take_create_slot()


def test_refresh_slot_is_per_provider_and_mode(admin_on):
    mcp_server._take_refresh_slot("p", dry_run=True)
    mcp_server._take_refresh_slot("p", dry_run=False)
    mcp_server._take_refresh_slot("q", dry_run=False)
    with pytest.raises(
        mcp_server.AdminToolError,
        match=r"^catalog of 'p' was refreshed less than 60 s ago: retry in 60s$",
    ):
        mcp_server._take_refresh_slot("p", dry_run=False)
    admin_on["now"] += 61
    mcp_server._take_refresh_slot("p", dry_run=False)


async def test_admin_request_reads_a_body_sent_in_pieces(admin_on, fake_gateway):
    async def in_pieces(request):
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        await resp.write(b'[{"id": "cli-x", "au')
        await asyncio.sleep(0.05)
        await resp.write(b'th_source": "subprocess:agy"}]')
        await resp.write_eof()
        return resp

    fake_gateway["respond"] = in_pieces
    assert await mcp_server._admin_request("GET", "/providers") == (
        200,
        [{"id": "cli-x", "auth_source": "subprocess:agy"}],
    )


async def test_admin_request_reads_a_large_body(admin_on, fake_gateway):
    providers = [{"id": f"p{i}", "models": [{"id": "m" * 60}] * 5} for i in range(800)]

    async def large(request):
        return web.json_response(providers)

    fake_gateway["respond"] = large
    status, payload = await mcp_server._admin_request("GET", "/providers")
    assert status == 200 and payload == providers


async def test_admin_request_caps_the_body(admin_on, fake_gateway):
    async def huge(request):
        return web.Response(body=b"x" * (mcp_server._ADMIN_RESPONSE_LIMIT + 10))

    fake_gateway["respond"] = huge
    assert await mcp_server._admin_request("GET", "/providers") == (
        200,
        {"detail": "gateway answer too large"},
    )


async def test_admin_request_wraps_a_non_json_body(admin_on, fake_gateway):
    async def text(request):
        return web.Response(status=502, text="Bad Gateway")

    fake_gateway["respond"] = text
    assert await mcp_server._admin_request("GET", "/providers") == (
        502,
        {"detail": "Bad Gateway"},
    )


async def test_admin_request_timeout_may_have_been_applied(
    admin_on, fake_gateway, monkeypatch
):
    async def slow(request):
        await asyncio.sleep(1)
        return web.json_response({})

    fake_gateway["respond"] = slow
    monkeypatch.setattr(mcp_server, "_ADMIN_TIMEOUT", 0.2)
    with pytest.raises(
        mcp_server.AdminToolError, match=r"could not reach the gateway \(TimeoutError\)"
    ) as exc:
        await mcp_server._admin_request("POST", "/providers", {})
    assert exc.value.maybe_applied is True


async def test_refused_connection_was_not_applied(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(mcp_server, "_HTTP_SESSION", None)
    try:
        with pytest.raises(mcp_server.AdminToolError) as exc:
            await mcp_server._admin_request("POST", "/providers", {})
        assert exc.value.maybe_applied is False
    finally:
        if mcp_server._HTTP_SESSION is not None:
            await mcp_server._HTTP_SESSION.close()


def test_gateway_error_reads_the_error_message_shape():
    payload = {"error": {"type": "value_error", "message": "bad value"}}
    assert mcp_server._gateway_error(500, payload) == "HTTP 500: bad value"
    assert mcp_server._gateway_error(500, {"x": 1}) == 'HTTP 500: {"x": 1}'


def test_gateway_error_scrubs_before_cutting():
    key = "sk-" + "s" * 40
    detail = "x" * 290 + key
    text = mcp_server._gateway_error(400, {"detail": detail}, key)
    assert "sk-" not in text and text.endswith("***")


def test_scrub_removes_the_json_escaped_key():
    key = 'sk-"quoted"'
    shown = mcp_server._gateway_error(422, {"detail": [{"input": key}]}, key)
    assert "quoted" not in shown and "***" in shown


def test_old_refresh_entries_are_pruned(admin_on):
    mcp_server._take_refresh_slot("p", dry_run=False)
    admin_on["now"] += 61
    mcp_server._take_refresh_slot("q", dry_run=False)
    assert list(mcp_server._refresh_last) == [("q", False)]


@pytest.mark.parametrize("name", mcp_server._SDK_MESSAGE_LOGGERS)
def test_sdk_message_logging_drops_debug_whatever_the_level(name, caplog):
    lg = logging.getLogger(name)
    old = lg.level
    try:
        lg.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG, logger=name):
            lg.debug("Received message: %s", PROVIDER_KEY)
            lg.info("kept")
        assert [r.getMessage() for r in caplog.records if r.name == name] == ["kept"]
    finally:
        lg.setLevel(old)


def test_sdk_message_logging_is_capped_at_import_once():
    # The module installs the filter when it is imported, switch on or off.
    for name in mcp_server._SDK_MESSAGE_LOGGERS:
        filters = logging.getLogger(name).filters
        assert sum(isinstance(f, mcp_server._DropDebug) for f in filters) == 1
    mcp_server._cap_sdk_message_logging()
    for name in mcp_server._SDK_MESSAGE_LOGGERS:
        filters = logging.getLogger(name).filters
        assert sum(isinstance(f, mcp_server._DropDebug) for f in filters) == 1


# ── Task 3: add_provider ────────────────────────────────────────────────────

ADMIN_HEADERS = {"Authorization": "Bearer adminkey"}


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
def unresolvable(monkeypatch):
    async def _resolve(host, port):
        raise UnsafeURL(f"cannot resolve {host}: synthetic")

    monkeypatch.setattr("plugboard.llm_pricing._resolve_public", _resolve)


@pytest.fixture
def audit_db(monkeypatch, tmp_path):
    from plugboard import audit as audit_mod

    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")


def _relay(monkeypatch):
    """Send the MCP server's admin calls to the real gateway app, in process."""
    import plugboard.ratelimit as rl
    from httpx import ASGITransport, AsyncClient

    from plugboard.app import app

    monkeypatch.setattr(rl, "_LIMIT", 0)
    rl._buckets.clear()
    rl._client_buckets.clear()
    state = {"calls": []}

    async def forward(method, path, body=None):
        state["calls"].append((method, path))
        headers = mcp_server._auth_headers(admin=True, json_body=body is not None)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as c:
            resp = await c.request(method, path, headers=headers, json=body)
        return resp.status_code, resp.json()

    async def direct(body):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as c:
            resp = await c.post("/providers", headers=ADMIN_HEADERS, json=body)
        return resp.status_code, resp.json()

    monkeypatch.setattr(mcp_server, "_admin_request", forward)
    state["direct"] = direct
    return state


@pytest.fixture
def gateway(admin_on, _empty_providers_file, _admin_key, audit_db, monkeypatch):
    """add_provider against the real gateway: no provider yet."""
    return _relay(monkeypatch)


async def test_add_provider_creates_an_inactive_guarded_provider(
    gateway, public, _empty_providers_file
):
    from plugboard import audit as audit_mod
    from plugboard import config

    text = await mcp_server.add_provider(
        "Acme", "https://api.acme.test/v1/", PROVIDER_KEY, default_model="m1"
    )
    assert text == (
        "Provider 'acme' created, inactive, public addresses only "
        "(https://api.acme.test/v1). Run refresh_catalog('acme'), then "
        "activate it in the dashboard (Providers)."
    )
    p = config.get_provider("acme")
    assert p["is_active"] is False and p["egress_guard"] is True
    assert p["default_model"] == "m1"
    assert p["bearer_token"] == PROVIDER_KEY  # reached the gateway intact
    assert PROVIDER_KEY not in _empty_providers_file.read_text()  # encrypted
    [entry] = audit_mod.get_audit_log(action="provider.create")
    assert entry["actor"] == "mcp"
    assert gateway["calls"] == [("POST", "/providers")]


async def test_add_provider_returns_the_gateway_id_on_a_name_clash(gateway, public):
    await mcp_server.add_provider("Acme", "https://api.acme.test/v1", PROVIDER_KEY)
    text = await mcp_server.add_provider(
        "Acme", "https://api.acme.test/v1", PROVIDER_KEY
    )
    assert text.startswith("Provider 'acme-1' created")


@pytest.mark.parametrize(
    "attr,value,expected",
    [
        ("_NETWORK_TRANSPORT", True, "only loaded for stdio"),
        ("_ADMIN_TOOLS", False, "PLUGBOARD_MCP_ADMIN_TOOLS=1"),
        ("_ADMIN_KEY", "", "add_provider needs PLUGBOARD_ADMIN_KEY"),
    ],
)
async def test_add_provider_refused_without_rights_and_without_a_call(
    gateway, monkeypatch, attr, value, expected
):
    monkeypatch.setattr(mcp_server, attr, value)
    text = await mcp_server.add_provider(
        "Acme", "https://api.acme.test/v1", PROVIDER_KEY
    )
    assert text.startswith("Error: ") and expected in text
    assert gateway["calls"] == []
    assert list(mcp_server._create_calls) == []  # a refused call takes no slot


@pytest.mark.parametrize(
    "name,url,resolver,allowed,status",
    [
        ("In", "http://10.0.0.5/v1", "private", "", 422),
        ("Ftp", "ftp://files.test", "public", "", 422),
        ("Gone", "https://gone.test", "unresolvable", "", 503),
        ("Out", "https://api.other.test", "public", "api.acme.test", 400),
        ("OpenClaw", "https://x.test", "public", "", 400),
    ],
)
async def test_add_provider_gives_the_admin_api_refusal(
    gateway, monkeypatch, request, name, url, resolver, allowed, status
):
    from plugboard import config

    request.getfixturevalue(resolver)
    if allowed:
        monkeypatch.setenv("PLUGBOARD_PROVIDER_DOMAINS_ALLOWED", allowed)
    body = {
        "name": name,
        "base_url": url,
        "bearer_token": PROVIDER_KEY,
        "is_active": False,
        "egress_guard": True,
    }
    got_status, payload = await gateway["direct"](body)
    assert got_status == status
    text = await mcp_server.add_provider(name, url, PROVIDER_KEY)
    assert text == "Error: " + mcp_server._gateway_error(status, payload, PROVIDER_KEY)
    assert config.load_providers() == []


async def test_key_is_scrubbed_from_a_refusal(admin_on, monkeypatch):
    async def echo(method, path, body=None):
        return 422, {"detail": f"bad key {body['bearer_token']}"}

    monkeypatch.setattr(mcp_server, "_admin_request", echo)
    text = await mcp_server.add_provider("A", "https://a.test", PROVIDER_KEY)
    assert text == "Error: HTTP 422: bad key ***"


async def test_add_provider_with_enforced_mfa(gateway, monkeypatch):
    monkeypatch.setattr("plugboard.admin_mfa.is_required", lambda: True)
    text = await mcp_server.add_provider("A", "https://a.test", PROVIDER_KEY)
    assert text == (
        "Error: the gateway enforces admin MFA: MCP admin tools cannot sign in, "
        "use the dashboard"
    )


async def test_add_provider_with_a_wrong_admin_key(gateway, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ADMIN_KEY", "wrong-admin-key")
    text = await mcp_server.add_provider("A", "https://a.test", PROVIDER_KEY)
    assert text == "Error: the gateway refused PLUGBOARD_ADMIN_KEY"


async def test_add_provider_gateway_down(admin_on, monkeypatch):
    monkeypatch.setattr(mcp_server, "_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(mcp_server, "_HTTP_SESSION", None)
    try:
        text = await mcp_server.add_provider("A", "https://a.test", PROVIDER_KEY)
    finally:
        if mcp_server._HTTP_SESSION is not None:
            await mcp_server._HTTP_SESSION.close()
    assert text == "Error: could not reach the gateway (ClientConnectorError)"


async def test_add_provider_timeout_says_it_may_exist(admin_on, monkeypatch):
    async def timeout(method, path, body=None):
        raise mcp_server.AdminToolError(
            "could not reach the gateway (TimeoutError)", maybe_applied=True
        )

    monkeypatch.setattr(mcp_server, "_admin_request", timeout)
    text = await mcp_server.add_provider("A", "https://a.test", PROVIDER_KEY)
    assert text == (
        "Error: could not reach the gateway (TimeoutError): the provider may "
        "have been created, check the dashboard before retrying"
    )


async def test_unexpected_success_answer_is_an_error(admin_on, monkeypatch):
    async def odd(method, path, body=None):
        return 201, {"detail": "gateway answer too large"}

    monkeypatch.setattr(mcp_server, "_admin_request", odd)
    text = await mcp_server.add_provider("A", "https://a.test", PROVIDER_KEY)
    assert text == (
        "Error: unexpected gateway answer (HTTP 201): the provider may have "
        "been created, check the dashboard before retrying"
    )


async def test_sixth_add_provider_is_refused_before_the_gateway(gateway, private):
    for i in range(5):
        text = await mcp_server.add_provider(f"P{i}", "http://10.0.0.5", PROVIDER_KEY)
        assert text.startswith("Error: HTTP 422")
    text = await mcp_server.add_provider("P5", "http://10.0.0.5", PROVIDER_KEY)
    assert text == "Error: add_provider limit reached (5 per hour): retry in 3600s"
    assert len(gateway["calls"]) == 5


@pytest.mark.parametrize(
    "answer",
    [
        {"id": "in", "base_url": "http://10.0.0.5/v1", "is_active": False},
        {"id": "in", "base_url": "http://10.0.0.5/v1", "egress_guard": False},
        {"id": "in", "base_url": "http://10.0.0.5/v1", "egress_guard": True},
    ],
)
async def test_unapplied_protection_is_never_claimed(admin_on, monkeypatch, answer):
    async def older_gateway(method, path, body=None):
        return 201, answer

    monkeypatch.setattr(mcp_server, "_admin_request", older_gateway)
    text = await mcp_server.add_provider("In", "http://10.0.0.5/v1", PROVIDER_KEY)
    assert text == (
        "Error: provider 'in' was created, but the gateway did not apply "
        '"inactive, public addresses only" (gateway older than 4.9.0?): '
        "check it in the dashboard"
    )


@pytest.mark.parametrize("status", [500, 502, 504])
async def test_server_error_says_it_may_exist(admin_on, monkeypatch, status):
    async def failed(method, path, body=None):
        return status, {"detail": "boom"}

    monkeypatch.setattr(mcp_server, "_admin_request", failed)
    text = await mcp_server.add_provider("A", "https://a.test", PROVIDER_KEY)
    assert text == (
        f"Error: HTTP {status}: boom: the provider may have been created, "
        "check the dashboard before retrying"
    )


async def test_answer_cut_after_the_status_says_it_may_exist(admin_on, monkeypatch):
    async def cut(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 201 Created\r\nContent-Type: application/json\r\n"
            b'Content-Length: 100\r\n\r\n{"id": "ac'
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(cut, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(mcp_server, "_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(mcp_server, "_HTTP_SESSION", None)
    try:
        text = await mcp_server.add_provider("Acme", "https://a.test", PROVIDER_KEY)
    finally:
        if mcp_server._HTTP_SESSION is not None:
            await mcp_server._HTTP_SESSION.close()
        server.close()
        await server.wait_closed()
    assert text == (
        "Error: the gateway answer was cut off (ClientPayloadError): the provider "
        "may have been created, check the dashboard before retrying"
    )


async def test_a_short_key_does_not_change_the_id(admin_on, monkeypatch):
    async def created(method, path, body=None):
        return 201, {
            "id": "test-provider",
            "base_url": "https://api.test-llm.test/v1",
            "is_active": False,
            "egress_guard": True,
        }

    monkeypatch.setattr(mcp_server, "_admin_request", created)
    text = await mcp_server.add_provider("Test Provider", "https://x.test", "test")
    assert "Provider 'test-provider' created" in text
    assert "refresh_catalog('test-provider')" in text


# ── Task 4: refresh_catalog ─────────────────────────────────────────────────


@pytest.fixture
def gateway_with_providers(
    admin_on, _providers_file, _admin_key, audit_db, monkeypatch
):
    """refresh_catalog against the real gateway: two HTTP providers and a CLI one."""
    from tests.factories import PROVIDER_A, PROVIDER_B

    cli = {
        **PROVIDER_A,
        "id": "cli-x",
        "name": "CLI X",
        "auth_source": "subprocess:agy",
    }
    _providers_file.write_text(json.dumps({"providers": [PROVIDER_A, PROVIDER_B, cli]}))
    return _relay(monkeypatch)


def _discovery(monkeypatch, models, trusted=True):
    state = {"calls": 0}

    async def discover(provider):
        from plugboard import router

        state["calls"] += 1
        router._discovery_trusted.set(trusted)  # as the real discovery does
        return models

    monkeypatch.setattr("plugboard.app.discover_models", discover)
    return state


@pytest.fixture
def found_m1(monkeypatch):
    return _discovery(monkeypatch, [{"id": "m1"}])


@pytest.fixture
def untrusted_discovery(monkeypatch):
    return _discovery(monkeypatch, [{"id": "m1"}], trusted=False)


@pytest.fixture
def blocking_discovery(monkeypatch):
    state = {"started": asyncio.Event(), "release": asyncio.Event()}

    async def discover(provider):
        state["started"].set()
        await state["release"].wait()
        return [{"id": "m1"}]

    monkeypatch.setattr("plugboard.app.discover_models", discover)
    return state


async def test_refresh_writes_the_catalog(gateway_with_providers, found_m1):
    from plugboard import audit as audit_mod
    from plugboard import config

    text = await mcp_server.refresh_catalog("test-provider")
    assert text == "Catalog of 'test-provider': first listing, 1 models: m1"
    assert [m["id"] for m in config.get_provider("test-provider")["models"]] == ["m1"]
    assert gateway_with_providers["calls"] == [
        ("GET", "/providers"),
        ("POST", "/providers/test-provider/discover"),
    ]
    [entry] = audit_mod.get_audit_log(action="provider.discover")
    assert entry["actor"] == "mcp"


async def test_preview_writes_nothing(gateway_with_providers, found_m1):
    from plugboard import config

    text = await mcp_server.refresh_catalog("test-provider", dry_run=True)
    assert text.endswith(" (preview, nothing written)")
    assert not config.get_provider("test-provider").get("models")
    assert gateway_with_providers["calls"][-1] == (
        "POST",
        "/providers/test-provider/discover?dry_run=true",
    )


async def test_changes_are_listed_and_long_lists_cut(
    gateway_with_providers, monkeypatch
):
    from plugboard import config

    config.update_provider("test-provider", {"models": [{"id": "old"}]})
    _discovery(monkeypatch, [{"id": f"m{i:02d}"} for i in range(30)])
    text = await mcp_server.refresh_catalog("test-provider")
    shown = ", ".join(f"m{i:02d}" for i in range(20))
    assert text == (
        f"Catalog of 'test-provider': 30 models; added: {shown} (+10 more); "
        "removed: old"
    )


async def test_unknown_provider_gives_the_admin_api_refusal(gateway_with_providers):
    text = await mcp_server.refresh_catalog("nope")
    assert text == "Error: HTTP 404: Provider 'nope' not found"


async def test_discovery_failure_gives_the_admin_api_refusal(
    gateway_with_providers, monkeypatch
):
    _discovery(monkeypatch, [])
    text = await mcp_server.refresh_catalog("test-provider")
    assert text == ("Error: HTTP 502: Could not discover models from 'test-provider'")


async def test_path_characters_are_encoded(gateway_with_providers):
    text = await mcp_server.refresh_catalog("../admin")
    assert ("POST", "/providers/..%2Fadmin/discover") in gateway_with_providers["calls"]
    assert text.startswith("Error: HTTP 404")


async def test_encoded_path_reaches_the_wire(admin_on, fake_gateway):
    async def answer(request):
        if request.method == "GET":
            return web.json_response([])
        return web.json_response({"detail": "Not Found"}, status=404)

    fake_gateway["respond"] = answer
    await mcp_server.refresh_catalog("a/b?c#d")
    assert fake_gateway["raw"][-1] == ("POST", "/providers/a%2Fb%3Fc%23d/discover")


@pytest.mark.parametrize("pid", ["", " ", ".", ".."])
async def test_ids_a_url_would_rewrite_are_refused(gateway_with_providers, pid):
    text = await mcp_server.refresh_catalog(pid)
    assert text == f"Error: invalid provider_id '{pid}'"
    assert gateway_with_providers["calls"] == []


async def test_cli_provider_is_refused_before_discovery(
    gateway_with_providers, found_m1
):
    text = await mcp_server.refresh_catalog("cli-x")
    assert text == (
        "Error: 'cli-x' is a CLI provider: its refresh runs model probes, "
        "refresh it from the dashboard"
    )
    assert found_m1["calls"] == 0
    assert gateway_with_providers["calls"] == [("GET", "/providers")]
    assert mcp_server._refresh_last == {}  # no slot taken


async def test_second_write_within_a_minute_is_refused(
    gateway_with_providers, found_m1, admin_on
):
    await mcp_server.refresh_catalog("test-provider", dry_run=True)
    await mcp_server.refresh_catalog("test-provider")
    text = await mcp_server.refresh_catalog("test-provider")
    assert text == (
        "Error: catalog of 'test-provider' was refreshed less than 60 s ago: "
        "retry in 60s"
    )
    assert found_m1["calls"] == 2
    admin_on["now"] += 61
    assert (await mcp_server.refresh_catalog("test-provider")).startswith("Catalog")


async def test_one_refresh_at_a_time(gateway_with_providers, blocking_discovery):
    first = asyncio.create_task(mcp_server.refresh_catalog("test-provider"))
    await blocking_discovery["started"].wait()
    busy = await mcp_server.refresh_catalog("other-provider")
    assert busy == "Error: a catalog refresh is already running"
    assert ("other-provider", False) not in mcp_server._refresh_last
    blocking_discovery["release"].set()
    assert (await first).startswith("Catalog of 'test-provider'")


async def test_untrusted_list_says_removals_were_skipped(
    gateway_with_providers, untrusted_discovery
):
    text = await mcp_server.refresh_catalog("test-provider")
    assert text.endswith("; removals skipped: the provider's list is not trusted")


@pytest.mark.parametrize(
    "attr,value,expected",
    [
        ("_NETWORK_TRANSPORT", True, "only loaded for stdio"),
        ("_ADMIN_TOOLS", False, "PLUGBOARD_MCP_ADMIN_TOOLS=1"),
        ("_ADMIN_KEY", "", "refresh_catalog needs PLUGBOARD_ADMIN_KEY"),
    ],
)
async def test_refresh_refused_without_rights_and_without_a_call(
    gateway_with_providers, monkeypatch, attr, value, expected
):
    monkeypatch.setattr(mcp_server, attr, value)
    text = await mcp_server.refresh_catalog("test-provider")
    assert text.startswith("Error: ") and expected in text
    assert gateway_with_providers["calls"] == []


async def test_providers_list_refusal_is_reported(admin_on, monkeypatch):
    async def mfa(method, path, body=None):
        return 401, {"detail": "Admin MFA required: sign in"}

    monkeypatch.setattr(mcp_server, "_admin_request", mfa)
    text = await mcp_server.refresh_catalog("test-provider")
    assert "enforces admin MFA" in text
    assert mcp_server._refresh_last == {}
