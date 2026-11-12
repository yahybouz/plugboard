"""MCP update registry: the client comes from the server's key (PB-MCP-01).

The registry tools act for the client of ``PLUGBOARD_API_KEY``, which the
server asks the gateway for (``GET /v1/me``); a ``client_id`` argument never
selects another client. Keys, clients and tokens are synthetic.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from plugboard import mcp_server  # noqa: E402


@pytest.fixture
def as_client(monkeypatch):
    """Fake gateway ``/v1/me``: the key resolves to ``state["client_id"]``."""
    state = {"client_id": "alpha", "error": None, "calls": []}

    async def fake_get(path, admin=False):
        state["calls"].append((path, admin))
        if path == "/v1/me":
            if state["error"]:
                return {"error": state["error"]}
            return {"client_id": state["client_id"]}
        return {"error": f"unexpected path {path}"}

    monkeypatch.setattr(mcp_server, "_api_get", fake_get)
    monkeypatch.setattr(mcp_server, "_API_KEY", "pb_synthetic_alpha")
    return state


# ── Task 1: the calling key's client ─────────────────────────────────────────


async def test_identity_comes_from_v1_me_with_the_client_key(as_client):
    assert await mcp_server._caller_client_id() == "alpha"
    assert as_client["calls"] == [("/v1/me", False)]


async def test_no_key_is_refused_without_calling_the_gateway(as_client, monkeypatch):
    monkeypatch.setattr(mcp_server, "_API_KEY", "")
    with pytest.raises(mcp_server.CallerIdentityError, match="needs PLUGBOARD_API_KEY"):
        await mcp_server._caller_client_id()
    assert as_client["calls"] == []


async def test_gateway_error_is_refused(as_client):
    as_client["error"] = "HTTP 401: Invalid API key"
    with pytest.raises(mcp_server.CallerIdentityError, match="HTTP 401"):
        await mcp_server._caller_client_id()


@pytest.mark.parametrize("value", [None, "", 42])
async def test_key_without_client_is_refused(as_client, value):
    as_client["client_id"] = value
    with pytest.raises(
        mcp_server.CallerIdentityError, match="does not identify a client"
    ):
        await mcp_server._caller_client_id()


async def test_admin_identity_is_refused(as_client):
    as_client["client_id"] = "admin"
    with pytest.raises(mcp_server.CallerIdentityError, match="is the admin key"):
        await mcp_server._caller_client_id()


async def test_revoked_key_is_refused_on_the_next_call(as_client):
    assert await mcp_server._caller_client_id() == "alpha"
    as_client["error"] = "HTTP 401: Invalid API key"
    with pytest.raises(mcp_server.CallerIdentityError):
        await mcp_server._caller_client_id()
    assert as_client["calls"] == [("/v1/me", False), ("/v1/me", False)]


@pytest.mark.parametrize("claimed", ["", "   ", "alpha", " alpha "])
async def test_own_or_absent_client_id_is_accepted(as_client, claimed):
    assert await mcp_server._registry_client(claimed) == "alpha"


@pytest.mark.parametrize("claimed", ["beta", "ALPHA"])
async def test_another_client_id_is_refused(as_client, claimed):
    with pytest.raises(mcp_server.CallerIdentityError, match="leave client_id out"):
        await mcp_server._registry_client(claimed)
