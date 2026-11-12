"""MCP update registry: the client comes from the server's key (PB-MCP-01).

The registry tools act for the client of ``PLUGBOARD_API_KEY``, which the
server asks the gateway for (``GET /v1/me``); a ``client_id`` argument never
selects another client. Keys, clients and tokens are synthetic.
"""

from __future__ import annotations

import json
import sqlite3
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
    monkeypatch.setattr(mcp_server, "_TRACKING_SCHEMA_READY", False)
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


@pytest.mark.parametrize(
    "call",
    [
        lambda: mcp_server.check_updates(),
        lambda: mcp_server.list_applied(),
        lambda: mcp_server.get_update_prompt("CP-A"),
        lambda: mcp_server.resource_pending_updates(),
    ],
    ids=["check_updates", "list_applied", "get_update_prompt", "pending"],
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
