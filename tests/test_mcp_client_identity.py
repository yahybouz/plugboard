"""MCP update registry: the client comes from the server's key (PB-MCP-01).

The registry tools act for the client of ``PLUGBOARD_API_KEY``, which the
server asks the gateway for (``GET /v1/me``); a ``client_id`` argument never
selects another client. Keys, clients and tokens are synthetic.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from plugboard import mcp_server  # noqa: E402


def _prompt(prompt_id: str, clients: list[str], title: str = "") -> dict:
    return {
        "id": prompt_id,
        "version": "9.9.9",
        "date": "2026-11-12",
        "impact": "IMPORTANT",
        "clients": clients,
        "title": title or f"Step {prompt_id}",
        "summary": f"Summary of {prompt_id}",
        "changes": [{"file": ".env", "action": "replace", "description": "Synthetic"}],
    }


def _write_registry(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "version": "9.9.9",
                "prompts": [
                    _prompt("CP-A", ["alpha"]),
                    _prompt("CP-B", ["beta"], title="Beta secret step"),
                    _prompt("CP-AB", ["alpha", "beta"]),
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _db_rows(db: Path) -> set[tuple[str, str]]:
    if not db.exists():
        return set()
    conn = sqlite3.connect(str(db))
    try:
        return set(conn.execute("SELECT client_id, prompt_id FROM applied_updates"))
    finally:
        conn.close()


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Registry with CP-A (alpha), CP-B (beta), CP-AB (both); private tracking DB."""
    path = _write_registry(tmp_path / "registry.json")
    monkeypatch.setattr(mcp_server, "_REGISTRY_PATH", path)
    monkeypatch.setattr(mcp_server, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "_TRACKING_DB", tmp_path / "client_updates.db")
    return path


def _seed(client_id: str, prompt_id: str) -> None:
    conn = mcp_server._tracking_conn()
    try:
        conn.execute(
            "INSERT INTO applied_updates (client_id, prompt_id, applied_at) "
            "VALUES (?, ?, '2026-11-12T00:00:00Z')",
            (client_id, prompt_id),
        )
        conn.commit()
    finally:
        conn.close()


def _rows() -> set[tuple[str, str]]:
    return _db_rows(Path(mcp_server._TRACKING_DB))


@pytest.fixture
def as_client(monkeypatch):
    """Fake gateway ``/v1/me``: the key resolves to ``state["client_id"]``."""
    state = {"client_id": "alpha", "error": None, "calls": []}

    async def fake_get(path, admin=False):
        state["calls"].append((path, admin))
        if path == "/v1/me":
            if "raise" in state:
                raise state["raise"]
            if "raw" in state:
                return state["raw"]
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


@pytest.mark.parametrize(
    "answer",
    [
        {"raw": ["not", "an", "object"]},
        {"raise": asyncio.TimeoutError()},
        {"raise": ValueError("Expecting value")},
    ],
    ids=["not-an-object", "timeout", "bad-json"],
)
async def test_unexpected_gateway_answer_is_refused(as_client, answer):
    as_client.update(answer)
    with pytest.raises(mcp_server.CallerIdentityError, match="could not identify"):
        await mcp_server._caller_client_id()


async def test_identity_messages_are_exact(as_client, monkeypatch):
    as_client["error"] = "HTTP 502: " + "x" * 300
    with pytest.raises(mcp_server.CallerIdentityError) as exc:
        await mcp_server._caller_client_id()
    assert str(exc.value) == (
        "could not identify the client of PLUGBOARD_API_KEY "
        f"({('HTTP 502: ' + 'x' * 300)[:120]})"
    )
    monkeypatch.setattr(mcp_server, "_API_KEY", "")
    with pytest.raises(mcp_server.CallerIdentityError) as exc:
        await mcp_server._caller_client_id()
    assert str(exc.value) == (
        "this tool needs PLUGBOARD_API_KEY: it acts for that key's client"
    )


@pytest.mark.parametrize("claimed", ["", "   ", "alpha", " alpha "])
async def test_own_or_absent_client_id_is_accepted(as_client, claimed):
    assert await mcp_server._registry_client(claimed) == "alpha"


@pytest.mark.parametrize("claimed", ["beta", "ALPHA"])
async def test_another_client_id_is_refused(as_client, claimed):
    with pytest.raises(mcp_server.CallerIdentityError, match="leave client_id out"):
        await mcp_server._registry_client(claimed)


# ── Task 2: registry reads ───────────────────────────────────────────────────


async def test_check_updates_lists_only_the_key_client(as_client, registry):
    out = await mcp_server.check_updates()
    assert "`CP-A`" in out and "`CP-AB`" in out
    assert "`CP-B`" not in out


async def test_check_updates_refuses_another_client_id(as_client, registry):
    out = await mcp_server.check_updates("beta")
    assert out.startswith("Error:")
    assert "`CP-B`" not in out and "Beta secret step" not in out


async def test_check_updates_accepts_its_own_client_id(as_client, registry):
    assert await mcp_server.check_updates("alpha") == await mcp_server.check_updates()


async def test_list_applied_shows_only_the_key_client(as_client, registry):
    _seed("alpha", "CP-A")
    _seed("beta", "CP-B")
    own = await mcp_server.list_applied()
    assert "CP-A" in own and "CP-B" not in own
    refused = await mcp_server.list_applied("beta")
    assert refused.startswith("Error:") and "CP-B" not in refused


async def test_get_update_prompt_hides_other_clients_updates(as_client, registry):
    other = await mcp_server.get_update_prompt("CP-B")
    missing = await mcp_server.get_update_prompt("CP-NONE")
    assert other == missing.replace("CP-NONE", "CP-B")
    assert "Beta secret step" not in other


async def test_get_update_prompt_names_only_the_caller(as_client, registry):
    out = await mcp_server.get_update_prompt("CP-AB")
    assert "**Client:** alpha" in out and "beta" not in out
    assert 'mark_applied("CP-AB")' in out


async def test_pending_resource_shows_only_the_key_client(as_client, registry):
    out = await mcp_server.resource_pending_updates()
    assert "`CP-A`" in out
    assert "beta" not in out and "`CP-B`" not in out


async def test_pending_resource_skips_applied_updates(as_client, registry):
    _seed("alpha", "CP-A")
    out = await mcp_server.resource_pending_updates()
    assert "`CP-A`" not in out and "`CP-AB`" in out


async def test_pending_resource_reports_a_missing_registry(
    as_client, registry, monkeypatch, tmp_path
):
    monkeypatch.setattr(mcp_server, "_REGISTRY_PATH", tmp_path / "absent.json")
    monkeypatch.setattr(mcp_server, "_LEGACY_REGISTRY_PATH", tmp_path / "old.json")
    out = await mcp_server.resource_pending_updates()
    assert "registry is missing" in out and "all up to date" not in out


async def test_clients_written_as_a_string_targets_nobody(as_client, registry):
    # A typo in the registry ("clients": "alphabet") must not match by substring.
    registry.write_text(
        json.dumps({"version": "9.9.9", "prompts": [_prompt("CP-S", "alphabet")]}),
        encoding="utf-8",
    )
    assert "`CP-S`" not in await mcp_server.check_updates()
    assert "`CP-S`" not in await mcp_server.resource_pending_updates()
    assert (await mcp_server.get_update_prompt("CP-S")).startswith("Error:")
    assert (await mcp_server.mark_applied("CP-S")).startswith("Error:")
    assert _rows() == set()


async def test_duplicate_prompt_id_resolves_to_the_callers_entry(as_client, registry):
    registry.write_text(
        json.dumps(
            {
                "version": "9.9.9",
                "prompts": [
                    _prompt("CP-X", ["beta"], title="Beta X"),
                    _prompt("CP-X", ["alpha"], title="Alpha X"),
                ],
            }
        ),
        encoding="utf-8",
    )
    out = await mcp_server.get_update_prompt("CP-X")
    assert "Alpha X" in out and "Beta X" not in out
    assert "Marked `CP-X`" in await mcp_server.mark_applied("CP-X")
    assert _rows() == {("alpha", "CP-X")}


@pytest.mark.parametrize(
    "call",
    [
        lambda: mcp_server.check_updates(),
        lambda: mcp_server.list_applied(),
        lambda: mcp_server.get_update_prompt("CP-A"),
        lambda: mcp_server.resource_pending_updates(),
        lambda: mcp_server.mark_applied("CP-A"),
    ],
    ids=["check_updates", "list_applied", "get_update_prompt", "pending", "mark"],
)
async def test_refused_identity_reads_nothing(as_client, registry, monkeypatch, call):
    as_client["client_id"] = "admin"

    def forbidden(*_a, **_k):
        pytest.fail("read before the caller was identified")

    monkeypatch.setattr(mcp_server, "_load_registry", forbidden)
    monkeypatch.setattr(mcp_server, "_tracking_conn", forbidden)
    assert (await call()).startswith("Error:")


# ── Task 3: mark_applied ─────────────────────────────────────────────────────


async def _call(name: str, args: dict) -> str:
    """Call a tool the way an MCP client does (arguments by name)."""
    result = await mcp_server.mcp.call_tool(name, args)
    content = result[0] if isinstance(result, tuple) else result
    return content[0].text


async def test_mark_applied_records_for_the_key_client(as_client, registry):
    await mcp_server.mark_applied("CP-AB")
    assert _rows() == {("alpha", "CP-AB")}


async def test_mark_applied_refuses_another_client_id(as_client, registry):
    out = await mcp_server.mark_applied("CP-AB", client_id="beta")
    assert out.startswith("Error:")
    assert _rows() == set()


async def test_mark_applied_on_another_clients_update_is_not_found(as_client, registry):
    out = await mcp_server.mark_applied("CP-B")
    assert out == "Error: prompt 'CP-B' not found in registry"
    assert _rows() == set()


async def test_mark_applied_writes_nothing_when_the_gateway_is_down(
    as_client, registry
):
    as_client["error"] = "HTTP client error: Cannot connect to host 127.0.0.1:9"
    assert (await mcp_server.mark_applied("CP-A")).startswith("Error:")
    assert _rows() == set()


async def test_old_by_name_call_with_own_client_id_still_works(as_client, registry):
    out = await _call(
        "mark_applied", {"client_id": "alpha", "prompt_id": "CP-A", "notes": "n"}
    )
    assert "Marked `CP-A`" in out and _rows() == {("alpha", "CP-A")}
    refused = await _call("mark_applied", {"client_id": "beta", "prompt_id": "CP-AB"})
    assert refused.startswith("Error:") and _rows() == {("alpha", "CP-A")}


# ── Task 4: inflight_requests ────────────────────────────────────────────────


async def test_inflight_refuses_the_admin_identity(as_client):
    as_client["client_id"] = "admin"
    assert (await mcp_server.inflight_requests()).startswith("Error:")
    assert as_client["calls"] == [("/v1/me", False)]


# ── Task 5: stdio, streamable HTTP and SSE ───────────────────────────────────

ROOT = Path(__file__).resolve().parents[1]
ALPHA_KEY = "pb_synthetic_alpha"
TOKEN = "T" * 16 + "synthetic-mcp-identity-token"  # 44 characters


@pytest.fixture
async def fake_gateway():
    """Gateway stand-in on a loopback port: only ``ALPHA_KEY`` is a client."""
    from aiohttp import web

    async def me(request):
        if request.headers.get("Authorization") == f"Bearer {ALPHA_KEY}":
            return web.json_response({"client_id": "alpha"})
        return web.json_response({"detail": "Invalid API key"}, status=401)

    app = web.Application()
    app.router.add_get("/v1/me", me)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.fixture
def net_data(tmp_path):
    """Data directory: the registry, and beta's CP-B already marked applied."""
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    _write_registry(data / "registry.json")
    conn = sqlite3.connect(str(data / "client_updates.db"))
    try:
        conn.execute(mcp_server._TRACKING_CREATE)
        conn.execute(
            "INSERT INTO applied_updates (client_id, prompt_id, applied_at) "
            "VALUES ('beta', 'CP-B', '2026-11-12T00:00:00Z')"
        )
        conn.commit()
    finally:
        conn.close()
    return data


@pytest.fixture
async def net_server(monkeypatch, fake_gateway, net_data):
    """A fresh server module set up for SSE/HTTP, acting with ``ALPHA_KEY``."""
    import plugboard

    # Restored on teardown, so later tests get back the module they imported.
    monkeypatch.setattr(plugboard, "mcp_server", mcp_server, raising=False)
    monkeypatch.delitem(sys.modules, "plugboard.mcp_server", raising=False)
    import plugboard.mcp_server as ms

    ms.configure_network_transport(TOKEN)
    monkeypatch.setattr(ms, "_BASE_URL", fake_gateway)
    monkeypatch.setattr(ms, "_API_KEY", ALPHA_KEY)
    monkeypatch.setattr(ms, "_HTTP_SESSION", None)
    monkeypatch.setattr(ms, "_REGISTRY_PATH", net_data / "registry.json")
    monkeypatch.setattr(ms, "_DATA_DIR", net_data)
    monkeypatch.setattr(ms, "_TRACKING_DB", net_data / "client_updates.db")
    try:
        yield ms
    finally:
        if ms._HTTP_SESSION is not None:
            await ms._HTTP_SESSION.close()
        sys.modules.pop("plugboard.mcp_server", None)


CLAIM_REFUSED = (
    "Error: client_id 'beta' is not the client of this key ('alpha'): "
    "leave client_id out"
)
NOT_FOUND_CP_B = "Error: prompt 'CP-B' not found in registry"


async def _scenario(call, db: Path) -> None:
    """As alpha, whatever client_id is sent, beta's data stays out of reach."""
    assert await call("check_updates", {"client_id": "beta"}) == CLAIM_REFUSED
    own = await call("check_updates", {})
    assert "`CP-A`" in own and "`CP-B`" not in own
    assert await call("get_update_prompt", {"prompt_id": "CP-B"}) == NOT_FOUND_CP_B
    assert await call("mark_applied", {"prompt_id": "CP-B"}) == NOT_FOUND_CP_B
    claimed = await call("mark_applied", {"client_id": "beta", "prompt_id": "CP-AB"})
    assert claimed == CLAIM_REFUSED
    assert await call("list_applied", {"client_id": "beta"}) == CLAIM_REFUSED
    assert _db_rows(db) == {("beta", "CP-B")}


async def test_stdio_caller_cannot_reach_another_client(
    fake_gateway, net_data, tmp_path
):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "TMPDIR": os.environ.get("TMPDIR", str(tmp_path)),
        "PLUGBOARD_URL": fake_gateway,
        "PLUGBOARD_API_KEY": ALPHA_KEY,
        "PLUGBOARD_DATA": str(net_data),
        "PLUGBOARD_REGISTRY_PATH": str(net_data / "registry.json"),
        "PLUGBOARD_SKIP_DOTENV": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "run_mcp.py")],
        env=env,
        cwd=str(ROOT),
    )
    with open(tmp_path / "mcp-stderr.log", "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 60)

                async def call(name, args):
                    result = await asyncio.wait_for(session.call_tool(name, args), 30)
                    return result.content[0].text

                await _scenario(call, net_data / "client_updates.db")


async def test_http_caller_cannot_reach_another_client(net_server, net_data):
    from httpx import ASGITransport, AsyncClient

    ms = net_server
    app = ms.mcp.streamable_http_app()
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "identity-test", "version": "0"},
        },
    }
    async with ms.mcp.session_manager.run():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8766"
        ) as client:
            r = await asyncio.wait_for(
                client.post("/mcp", json=init, headers=headers), 10
            )
            assert r.status_code == 200
            headers["mcp-session-id"] = r.headers["mcp-session-id"]
            headers["mcp-protocol-version"] = "2025-06-18"
            note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            await asyncio.wait_for(client.post("/mcp", json=note, headers=headers), 10)
            ids = iter(range(2, 100))

            async def call(name, args):
                body = {
                    "jsonrpc": "2.0",
                    "id": next(ids),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                }
                r = await asyncio.wait_for(
                    client.post("/mcp", json=body, headers=headers), 30
                )
                assert r.status_code == 200, r.text
                message = next(
                    (
                        json.loads(line[len("data:") :])
                        for line in r.text.splitlines()
                        if line.startswith("data:")
                    ),
                    None,
                )
                assert message is not None, r.text
                return message["result"]["content"][0]["text"]

            await _scenario(call, net_data / "client_updates.db")


async def test_sse_caller_cannot_reach_another_client(net_server, net_data):
    import uvicorn
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    ms = net_server
    server = uvicorn.Server(
        uvicorn.Config(
            ms.mcp.sse_app(),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
            timeout_graceful_shutdown=2,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started, "the SSE server did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        async with sse_client(
            f"http://127.0.0.1:{port}/sse",
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 30)

                async def call(name, args):
                    result = await asyncio.wait_for(session.call_tool(name, args), 30)
                    return result.content[0].text

                await _scenario(call, net_data / "client_updates.db")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
