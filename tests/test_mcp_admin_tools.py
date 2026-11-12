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
    state = {"headers": {}, "hits": [], "redirect": None}

    async def handle(request):
        state["hits"].append(request.path)
        state["headers"] = dict(request.headers)
        if state["redirect"]:
            raise web.HTTPFound(state["redirect"])
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


@pytest.mark.parametrize(
    "before,after",
    [
        (logging.DEBUG, logging.INFO),
        (logging.NOTSET, logging.INFO),
        (logging.WARNING, logging.WARNING),
    ],
)
def test_sdk_message_logging_is_capped(before, after):
    lg = logging.getLogger("mcp.server.lowlevel.server")
    old = lg.level
    try:
        lg.setLevel(before)
        mcp_server._cap_sdk_message_logging()
        assert lg.level == after
    finally:
        lg.setLevel(old)
