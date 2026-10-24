"""Parent RED counter-proofs for inherited auth defects (#6 MCP, #7 OAuth
loader cache, #8 admin MFA/session/TOTP, #13 OVH compose CLI homes).

Parent contract (names chosen here, to be honoured by the fix):

* #6  ``PLUGBOARD_MCP_TOKEN`` is the dedicated MCP bearer. ``scripts/run_mcp.py``
  with ``--sse``/``--http`` and no such token must refuse to start
  (SystemExit or explicit exception, no server run). With the token: host
  127.0.0.1, DNS rebinding protection on, ``Authorization: Bearer <token>``
  required on the FastMCP HTTP app, and ``PLUGBOARD_ADMIN_KEY`` never sent
  upstream. stdio keeps starting as today.
* #7  any write to the OAuth store (``delete_credential``/``save_credential``,
  hence ``DELETE /admin/oauth/{source}``) invalidates the cached loaders of that
  source, including ``broker:github+copilot``; a token without ``expires_at``
  is not served from cache after its row disappeared and 24 h elapsed.
* #8  with ``PLUGBOARD_ADMIN_MFA_REQUIRED=1`` + enrolled secret, the raw admin
  bearer alone is refused (401/403) on admin routes; a session cookie replayed
  after ``POST /auth/admin/logout`` is refused; a TOTP code is single-use.
* #13 the OVH ``gateway`` service mounts no host-native ``.codex``/``.claude``/
  ``.gemini`` home; CLI homes come from a dedicated named volume.

All values are synthetic. No server binds: FastMCP.run and uvicorn are
monkeypatched; HTTP apps are exercised in-process through httpx ASGITransport.
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
import sys
import time
from pathlib import Path

import anyio
import pytest
import yaml
from aioresponses import aioresponses
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]
MCP_TOKEN_ENV = "PLUGBOARD_MCP_TOKEN"
SYNTH_MCP_TOKEN = "synthetic-mcp-token-0123456789abcdef"
SYNTH_ADMIN = "synthetic-admin-not-real-123456"
SYNTH_API = "synthetic-api"
SYNTH_URL = "http://plugboard.invalid:8765"
ADMIN_BEARER = {"Authorization": "Bearer adminkey"}
MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "parent-red", "version": "0"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


# ── #6 MCP network transports ───────────────────────────────────────────────


def _launch_run_mcp(monkeypatch, *flags, token=None):
    """Execute scripts/run_mcp.py main() with every server entry point stubbed.

    Returns (runs, error, fresh_mcp_server_module). ``runs`` lists
    (FastMCP instance, transport) for each start attempt.
    """
    import plugboard
    import plugboard.mcp_server as original_mcp_server
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    runs: list[tuple[object, str]] = []

    def fake_run(self, transport="stdio", mount_path=None):
        runs.append((self, transport))

    async def fake_stdio(self):
        runs.append((self, "stdio"))

    async def fake_sse(self, mount_path=None):
        runs.append((self, "sse"))

    async def fake_http(self):
        runs.append((self, "streamable-http"))

    def no_bind(*args, **kwargs):
        raise AssertionError("harness: uvicorn must not be started by the test")

    async def no_bind_async(*args, **kwargs):
        raise AssertionError("harness: uvicorn must not be started by the test")

    monkeypatch.setattr(FastMCP, "run", fake_run)
    monkeypatch.setattr(FastMCP, "run_stdio_async", fake_stdio)
    monkeypatch.setattr(FastMCP, "run_sse_async", fake_sse)
    monkeypatch.setattr(FastMCP, "run_streamable_http_async", fake_http)
    monkeypatch.setattr(uvicorn, "run", no_bind)
    monkeypatch.setattr(uvicorn.Server, "serve", no_bind_async)

    monkeypatch.delenv(MCP_TOKEN_ENV, raising=False)
    if token is not None:
        monkeypatch.setenv(MCP_TOKEN_ENV, token)
    monkeypatch.setenv("PLUGBOARD_URL", SYNTH_URL)
    monkeypatch.setenv("PLUGBOARD_API_KEY", SYNTH_API)
    monkeypatch.setenv("PLUGBOARD_ADMIN_KEY", SYNTH_ADMIN)
    monkeypatch.setenv("MCP_PORT", "8766")
    monkeypatch.delenv("MCP_HOST", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_mcp.py", *flags])
    monkeypatch.setattr(sys, "path", list(sys.path))
    # Fresh import of plugboard.mcp_server, as in a real launch; the original
    # module object is restored at teardown.
    monkeypatch.setattr(plugboard, "mcp_server", original_mcp_server)
    monkeypatch.delitem(sys.modules, "plugboard.mcp_server")

    error: BaseException | None = None
    try:
        spec = importlib.util.spec_from_file_location(
            "_run_mcp_parent_under_test", ROOT / "scripts" / "run_mcp.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.main()
    except (SystemExit, Exception) as exc:  # explicit refusal is the contract
        error = exc
    return runs, error, sys.modules.get("plugboard.mcp_server")


def _single_run(runs, error, expected_transport):
    assert error is None, f"harness: launch with token failed: {error!r}"
    assert runs, "harness: no MCP start recorded"
    instance, transport = runs[-1]
    assert transport == expected_transport, f"harness: transport {transport!r}"
    return instance


async def _mcp_http_posts(instance, *calls):
    """POST an MCP initialize per (headers, host) within ONE app lifespan."""
    app = instance.streamable_http_app()
    out = []
    with anyio.fail_after(15):
        async with app.router.lifespan_context(app):
            for headers, host in calls:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url=f"http://{host}"
                ) as c:
                    out.append(
                        await c.post(
                            instance.settings.streamable_http_path,
                            headers={**MCP_HEADERS, **headers},
                            json=MCP_INIT,
                        )
                    )
    return out


LOOPBACK = "127.0.0.1:8766"


@pytest.mark.parametrize("flag", ["--sse", "--http"])
def test_mcp_network_transport_without_dedicated_token_refuses_to_start(
    monkeypatch, flag
):
    runs, error, _ = _launch_run_mcp(monkeypatch, flag, token=None)
    assert not runs and error is not None, (
        f"DEFECT #6: run_mcp.py {flag} started FastMCP ({[t for _, t in runs]}) "
        f"without {MCP_TOKEN_ENV}: unauthenticated network MCP server"
    )
    if isinstance(error, SystemExit):
        assert error.code not in (0, None), "refusal must exit non-zero"


async def test_mcp_http_with_token_requires_bearer(monkeypatch):
    runs, error, _ = _launch_run_mcp(monkeypatch, "--http", token=SYNTH_MCP_TOKEN)
    instance = _single_run(runs, error, "streamable-http")
    anonymous, wrong = await _mcp_http_posts(
        instance, ({}, LOOPBACK), ({"Authorization": "Bearer wrong-token"}, LOOPBACK)
    )
    assert anonymous.status_code == 401, (
        f"DEFECT #6: --http with {MCP_TOKEN_ENV} serves an anonymous caller "
        f"(HTTP {anonymous.status_code}, expected 401)"
    )
    assert wrong.status_code == 401, (
        f"DEFECT #6: wrong MCP bearer accepted (HTTP {wrong.status_code})"
    )


async def test_mcp_sse_with_token_requires_bearer(monkeypatch):
    runs, error, _ = _launch_run_mcp(monkeypatch, "--sse", token=SYNTH_MCP_TOKEN)
    instance = _single_run(runs, error, "sse")
    app = instance.sse_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:8766"
    ) as c:
        resp = await c.post(
            instance.settings.message_path + "?session_id=" + "0" * 32,
            headers={"Content-Type": "application/json"},
            json=MCP_INIT,
        )
    assert resp.status_code == 401, (
        f"DEFECT #6: --sse message endpoint reachable without MCP bearer "
        f"(HTTP {resp.status_code}, expected 401)"
    )


async def test_mcp_network_transport_never_sends_admin_key(monkeypatch):
    runs, error, mod = _launch_run_mcp(monkeypatch, "--http", token=SYNTH_MCP_TOKEN)
    if error is not None and not runs:
        return  # refusing to start with an admin key present also honours #6
    assert mod is not None, "harness: plugboard.mcp_server not imported by main()"
    seen: list[str] = []
    with aioresponses() as m:
        m.get(
            re.compile(r"http://plugboard\.invalid.*"),
            payload={"running": True, "providers": {}},
            repeat=True,
        )
        try:
            await mod.provider_status()
        finally:
            session = getattr(mod, "_HTTP_SESSION", None)
            if session is not None and not session.closed:
                await session.close()
        for calls in m.requests.values():
            for call in calls:
                seen.append(str((call.kwargs or {}).get("headers") or {}))
    leaked = sum(SYNTH_ADMIN in h for h in seen)
    assert leaked == 0, (
        "DEFECT #6: network-transport MCP forwards PLUGBOARD_ADMIN_KEY upstream "
        f"(provider_status sent {leaked} admin-bearer request(s))"
    )


async def test_mcp_http_with_token_witness_loopback_rebinding_and_valid_bearer(
    monkeypatch,
):
    runs, error, _ = _launch_run_mcp(monkeypatch, "--http", token=SYNTH_MCP_TOKEN)
    instance = _single_run(runs, error, "streamable-http")
    assert instance.settings.host == "127.0.0.1"
    ts = instance.settings.transport_security
    assert ts is not None and ts.enable_dns_rebinding_protection is True
    auth = {"Authorization": f"Bearer {SYNTH_MCP_TOKEN}"}
    ok, rebound = await _mcp_http_posts(
        instance, (auth, LOOPBACK), (auth, "evil.example:8766")
    )
    assert ok.status_code == 200, f"valid MCP bearer refused: {ok.status_code}"
    assert rebound.status_code == 421, f"rebinding Host accepted: {rebound.status_code}"


def test_mcp_stdio_witness_still_starts_without_token(monkeypatch):
    runs, error, _ = _launch_run_mcp(monkeypatch, token=None)
    assert error is None, f"stdio launch failed: {error!r}"
    assert [t for _, t in runs] == ["stdio"]


# ── #7 OAuth loader cache vs credential store ───────────────────────────────


@pytest.fixture
def _oauth_db(tmp_path, monkeypatch):
    from plugboard import cli_auth, oauth_store

    data = tmp_path / "oauth-data"
    data.mkdir()
    db = data / "credentials.db"
    monkeypatch.setattr(oauth_store, "_DATA_DIR", data)
    monkeypatch.setattr(oauth_store, "_DB_PATH", db)
    cli_auth.reset_loaders()
    yield db
    cli_auth.reset_loaders()


def _save(source, token, expires_at):
    from plugboard.oauth_store import OAuthCredential, save_credential

    save_credential(
        OAuthCredential(
            source=source,
            account="synthetic@example.invalid",
            access_token=token,
            expires_at=expires_at,
        )
    )


async def _token(source):
    from plugboard.cli_auth import get_loader

    bundle = await get_loader(source).get_token()
    return bundle.access_token if bundle else None


class _ShiftedTime:
    def __init__(self, offset):
        self._offset = offset

    def time(self):
        return time.time() + self._offset

    def monotonic(self):
        return time.monotonic() + self._offset

    def __getattr__(self, name):
        return getattr(time, name)


async def test_oauth_delete_endpoint_invalidates_broker_loader(
    admin_client, _oauth_db, monkeypatch
):
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    _save("github", "gh-old-synthetic", time.time() + 3600)
    assert await _token("broker:github") == "gh-old-synthetic"
    resp = await admin_client.delete("/admin/oauth/github", headers=ADMIN_BEARER)
    assert resp.status_code == 204, f"harness: delete returned {resp.status_code}"
    served = await _token("broker:github")
    assert served is None, (
        "DEFECT #7: broker:github loader still serves the revoked token after "
        f"DELETE /admin/oauth/github (served={served!r})"
    )


async def test_oauth_delete_endpoint_invalidates_copilot_loader(
    admin_client, _oauth_db, monkeypatch
):
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    _save("github", "gh-old-synthetic", time.time() + 3600)
    endpoint = "https://api.github.com/copilot_internal/v2/token"
    with aioresponses() as m:
        m.get(
            endpoint,
            payload={
                "token": "copilot-old-synthetic",
                "expires_at": time.time() + 1800,
            },
            repeat=True,
        )
        assert await _token("broker:github+copilot") == "copilot-old-synthetic"
        resp = await admin_client.delete("/admin/oauth/github", headers=ADMIN_BEARER)
        assert resp.status_code == 204, f"harness: delete returned {resp.status_code}"
        served = await _token("broker:github+copilot")
    assert served is None, (
        "DEFECT #7: broker:github+copilot still serves the Copilot token derived "
        f"from a deleted GitHub credential (served={served!r})"
    )


async def test_oauth_save_new_credential_replaces_cached_token(_oauth_db):
    _save("google", "google-old-synthetic", time.time() + 3600)
    assert await _token("broker:google") == "google-old-synthetic"
    _save("google", "google-new-synthetic", time.time() + 3600)
    served = await _token("broker:google")
    assert served == "google-new-synthetic", (
        "DEFECT #7: broker:google loader keeps serving the replaced token after "
        f"save_credential (served={served!r})"
    )


async def test_oauth_token_without_expiry_is_not_cached_forever(_oauth_db, monkeypatch):
    from plugboard import cli_auth

    _save("openai", "openai-noexp-synthetic", None)
    assert await _token("broker:openai") == "openai-noexp-synthetic"
    # Out-of-band removal (another process / manual revocation), then 24 h pass.
    with sqlite3.connect(str(_oauth_db)) as conn:
        conn.execute("DELETE FROM oauth_credentials WHERE source = 'openai'")
    monkeypatch.setattr(cli_auth, "time", _ShiftedTime(86400))
    served = await _token("broker:openai")
    assert served is None, (
        "DEFECT #7: token with expires_at=None served from cache 24 h after its "
        f"store row vanished (served={served!r})"
    )


async def test_oauth_witness_valid_token_not_deleted_stays_served(
    admin_client, _oauth_db, monkeypatch
):
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    _save("github", "gh-kept-synthetic", time.time() + 3600)
    _save("google", "google-other-synthetic", time.time() + 3600)
    assert await _token("broker:github") == "gh-kept-synthetic"
    resp = await admin_client.delete("/admin/oauth/google", headers=ADMIN_BEARER)
    assert resp.status_code == 204
    assert await _token("broker:github") == "gh-kept-synthetic"
    assert await _token("broker:github") == "gh-kept-synthetic"


# ── #8 admin bearer vs MFA, stateless sessions, TOTP replay ─────────────────


@pytest.fixture
def _mfa(tmp_path, monkeypatch):
    from plugboard import admin_mfa

    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    return admin_mfa


def _enforce_mfa(mfa, monkeypatch):
    secret = mfa.generate_secret()
    mfa.enroll(secret)
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert mfa.is_required(), "harness: MFA not enforced"
    return secret


def _session_cookie(resp):
    m = re.search(
        r"plugboard_admin_session=([^;]+)", resp.headers.get("set-cookie", "")
    )
    assert m, f"harness: no session cookie in login response ({resp.status_code})"
    return {"Cookie": f"plugboard_admin_session={m.group(1)}"}


@pytest.mark.parametrize("path", ["/admin/keys", "/providers"])
async def test_admin_bearer_alone_refused_when_mfa_required(
    admin_client, _mfa, monkeypatch, path
):
    _enforce_mfa(_mfa, monkeypatch)
    admin_client.cookies.clear()
    resp = await admin_client.get(path, headers=ADMIN_BEARER)
    assert resp.status_code in (401, 403), (
        f"DEFECT #8: GET {path} with raw admin bearer and no TOTP accepted "
        f"while MFA is required (HTTP {resp.status_code})"
    )


async def test_admin_session_replayed_after_logout_refused(admin_client, _mfa):
    login = await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    assert login.status_code == 204, f"harness: login {login.status_code}"
    cookie = _session_cookie(login)
    admin_client.cookies.clear()
    before = await admin_client.get("/admin/keys", headers=cookie)
    assert before.status_code == 200, f"harness: session refused {before.status_code}"
    out = await admin_client.post("/auth/admin/logout", headers=cookie)
    assert out.status_code == 204
    admin_client.cookies.clear()
    after = await admin_client.get("/admin/keys", headers=cookie)
    assert after.status_code in (401, 403), (
        "DEFECT #8: admin session cookie still valid after logout "
        f"(HTTP {after.status_code})"
    )


async def test_admin_totp_code_is_single_use(admin_client, _mfa, monkeypatch):
    secret = _enforce_mfa(_mfa, monkeypatch)
    code = _mfa._generate_code(secret)
    first = await admin_client.post(
        "/auth/admin/login", json={"key": "adminkey", "totp_code": code}
    )
    assert first.status_code == 204, f"harness: first TOTP login {first.status_code}"
    admin_client.cookies.clear()
    replay = await admin_client.post(
        "/auth/admin/login", json={"key": "adminkey", "totp_code": code}
    )
    assert replay.status_code in (401, 403), (
        f"DEFECT #8: same TOTP code accepted twice (replay HTTP {replay.status_code})"
    )


async def test_admin_witness_bearer_accepted_without_mfa(admin_client, _mfa):
    resp = await admin_client.get("/admin/keys", headers=ADMIN_BEARER)
    assert resp.status_code == 200


async def test_admin_witness_session_valid_before_logout(admin_client, _mfa):
    login = await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    assert login.status_code == 204
    cookie = _session_cookie(login)
    admin_client.cookies.clear()
    resp = await admin_client.get("/admin/keys", headers=cookie)
    assert resp.status_code == 200


async def test_admin_witness_mfa_session_grants_admin(admin_client, _mfa, monkeypatch):
    secret = _enforce_mfa(_mfa, monkeypatch)
    login = await admin_client.post(
        "/auth/admin/login",
        json={"key": "adminkey", "totp_code": _mfa._generate_code(secret)},
    )
    assert login.status_code == 204
    cookie = _session_cookie(login)
    admin_client.cookies.clear()
    resp = await admin_client.get("/admin/keys", headers=cookie)
    assert resp.status_code == 200


# ── #13 OVH compose: no host-native CLI homes in the gateway ────────────────

COMPOSE = ROOT / "deploy" / "ovh" / "compose.yml"
NATIVE_HOMES = {".codex", ".claude", ".gemini"}


def _gateway_mounts():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    gateway = (doc.get("services") or {}).get("gateway")
    named = set((doc.get("volumes") or {}).keys())
    mounts = []
    for vol in (gateway or {}).get("volumes") or []:
        if isinstance(vol, str):
            parts = vol.split(":")
            source, target = (parts[0], parts[1]) if len(parts) > 1 else ("", parts[0])
            kind = (
                "volume"
                if source and not source.startswith(("/", ".", "~", "$"))
                else ("bind" if source else "anonymous")
            )
        else:
            source = str(vol.get("source", ""))
            target = str(vol.get("target", ""))
            kind = str(vol.get("type", "volume"))
        mounts.append({"type": kind, "source": source, "target": target.rstrip("/")})
    return gateway, mounts, named


def test_compose_gateway_mounts_no_host_native_cli_home():
    _, mounts, _ = _gateway_mounts()
    offending = [
        f"{m['source']} -> {m['target']}"
        for m in mounts
        if m["type"] == "bind" and NATIVE_HOMES.intersection(Path(m["source"]).parts)
    ]
    assert not offending, (
        "DEFECT #13: gateway bind-mounts host-native CLI homes (read-write): "
        + ", ".join(offending)
    )


@pytest.mark.parametrize("home", sorted(NATIVE_HOMES - {".gemini"}))
def test_compose_cli_home_served_by_dedicated_named_volume(home):
    _, mounts, named = _gateway_mounts()
    wanted = f"/home/app/{home}"
    covering = [
        m
        for m in mounts
        if wanted == m["target"] or wanted.startswith(m["target"] + "/")
    ]
    assert covering, f"harness: no mount covers {wanted}"
    effective = max(covering, key=lambda m: len(m["target"]))
    assert effective["type"] == "volume" and effective["source"] in named, (
        f"DEFECT #13: {wanted} is served by {effective['type']} "
        f"{effective['source']!r}, not a dedicated named volume"
    )


def test_compose_witness_gateway_exists_with_volumes():
    gateway, mounts, named = _gateway_mounts()
    assert gateway is not None
    assert mounts
    assert any(m["target"] == "/app/data" and m["source"] in named for m in mounts)
