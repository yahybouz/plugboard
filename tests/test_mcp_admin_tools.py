"""MCP admin tools (PB-MCP-02): add_provider and refresh_catalog.

stdio only, behind PLUGBOARD_MCP_ADMIN_TOOLS=1 and PLUGBOARD_ADMIN_KEY. The
tools go through the gateway's admin routes, so they get its validation and
its audit. Keys, hosts and providers are synthetic; DNS is never resolved.
"""

from __future__ import annotations

import asyncio
import collections
import logging

import pytest
from aiohttp import web

pytest.importorskip("mcp")

from plugboard import mcp_server  # noqa: E402

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
    state = {"headers": {}, "hits": [], "redirect": None, "respond": None}

    async def handle(request):
        state["hits"].append(request.path)
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
