"""V7 counter-tests for the inherited auth/ops defects (#6 #7 #8 #13).

They complement ``tests/test_inherited_auth_parent.py`` (immutable) with the
edges of the same contract:

* #6  network MCP: token length, wrong token, constant-time comparison,
  Host/Origin allow-lists, stdio unchanged, no admin key in network mode.
* #7  OAuth loaders: a write is seen immediately, a delete only touches its
  own source, a token without ``expires_at`` is cached 5 minutes at most.
* #8  admin: machine TOTP header is single-use, sessions are server-side and
  revocable one by one, any MFA change revokes them all.
* #13 OVH compose: no host home path, CLI homes on the ``cli-home`` volume.

All values are synthetic; no server binds (FastMCP/uvicorn entry points are
stubbed, HTTP apps run in-process through httpx ASGITransport).
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
import secrets
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
SYNTH_MCP_TOKEN = "synthetic-mcp-token-v7-0123456789abcdef"
SYNTH_ADMIN = "synthetic-admin-not-real-v7-123456"
SYNTH_URL = "http://plugboard.invalid:8765"
ADMIN_BEARER = {"Authorization": "Bearer adminkey"}
TOTP_HEADER = "X-Plugboard-Admin-TOTP"
MCP_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "v7", "version": "0"},
    },
}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
LOOPBACK = "127.0.0.1:8766"


# ── #6 MCP network transports ───────────────────────────────────────────────


def _launch(monkeypatch, *flags, token=None, env=None):
    """Run scripts/run_mcp.py main() with every server entry point stubbed.

    Returns (runs, error, fresh plugboard.mcp_server module).
    """
    import plugboard
    import plugboard.mcp_server as original_mcp_server
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    runs: list[tuple[object, str]] = []

    def fake_run(self, transport="stdio", mount_path=None):
        runs.append((self, transport))

    def no_bind(*args, **kwargs):
        raise AssertionError("harness: uvicorn must not be started by the test")

    async def no_bind_async(*args, **kwargs):
        raise AssertionError("harness: uvicorn must not be started by the test")

    monkeypatch.setattr(FastMCP, "run", fake_run)
    monkeypatch.setattr(uvicorn, "run", no_bind)
    monkeypatch.setattr(uvicorn.Server, "serve", no_bind_async)
    for name in (
        MCP_TOKEN_ENV,
        "MCP_HOST",
        "PLUGBOARD_MCP_ALLOWED_HOSTS",
        "PLUGBOARD_MCP_ALLOWED_ORIGINS",
        "LLM_ENABLER_ADMIN_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    if token is not None:
        monkeypatch.setenv(MCP_TOKEN_ENV, token)
    monkeypatch.setenv("PLUGBOARD_URL", SYNTH_URL)
    monkeypatch.setenv("PLUGBOARD_API_KEY", "synthetic-api")
    monkeypatch.setenv("PLUGBOARD_ADMIN_KEY", SYNTH_ADMIN)
    monkeypatch.setenv("MCP_PORT", "8766")
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["run_mcp.py", *flags])
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(plugboard, "mcp_server", original_mcp_server)
    monkeypatch.delitem(sys.modules, "plugboard.mcp_server")

    error: BaseException | None = None
    try:
        spec = importlib.util.spec_from_file_location(
            "_run_mcp_v7_under_test", ROOT / "scripts" / "run_mcp.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.main()
    except (SystemExit, Exception) as exc:
        error = exc
    return runs, error, sys.modules.get("plugboard.mcp_server")


def _started(runs, error, transport):
    assert error is None, f"launch failed: {error!r}"
    assert runs and runs[-1][1] == transport, f"runs={runs!r}"
    return runs[-1][0]


async def _http_posts(instance, *calls):
    """POST an MCP initialize per (headers, host) within one app lifespan."""
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


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("flag", ["--sse", "--http"])
def test_mcp_network_token_shorter_than_32_chars_refuses_to_start(monkeypatch, flag):
    runs, error, _ = _launch(monkeypatch, flag, token="s" * 31)
    assert not runs
    assert isinstance(error, SystemExit) and error.code not in (0, None)


def test_mcp_network_token_of_32_chars_starts(monkeypatch):
    runs, error, _ = _launch(monkeypatch, "--http", token="s" * 32)
    _started(runs, error, "streamable-http")


async def test_mcp_wrong_or_extended_token_is_refused(monkeypatch):
    runs, error, _ = _launch(monkeypatch, "--http", token=SYNTH_MCP_TOKEN)
    instance = _started(runs, error, "streamable-http")
    wrong, extended, truncated = await _http_posts(
        instance,
        (_bearer(SYNTH_MCP_TOKEN[:-1] + "X"), LOOPBACK),
        (_bearer(SYNTH_MCP_TOKEN + "x"), LOOPBACK),
        (_bearer(SYNTH_MCP_TOKEN[:-1]), LOOPBACK),
    )
    assert [wrong.status_code, extended.status_code, truncated.status_code] == [
        401,
        401,
        401,
    ]


async def test_mcp_token_is_compared_in_constant_time(monkeypatch):
    runs, error, _ = _launch(monkeypatch, "--http", token=SYNTH_MCP_TOKEN)
    instance = _started(runs, error, "streamable-http")
    real = hmac.compare_digest
    seen = []

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    (ok,) = await _http_posts(instance, (_bearer(SYNTH_MCP_TOKEN), LOOPBACK))
    assert ok.status_code == 200
    expected = {SYNTH_MCP_TOKEN, SYNTH_MCP_TOKEN.encode()}
    assert any(a in expected and b in expected for a, b in seen), (
        "MCP bearer not checked with hmac.compare_digest"
    )


async def test_mcp_foreign_origin_is_refused(monkeypatch):
    runs, error, _ = _launch(monkeypatch, "--http", token=SYNTH_MCP_TOKEN)
    instance = _started(runs, error, "streamable-http")
    auth = _bearer(SYNTH_MCP_TOKEN)
    (foreign,) = await _http_posts(
        instance, ({**auth, "Origin": "http://evil.example"}, LOOPBACK)
    )
    assert foreign.status_code == 403


async def test_mcp_allowed_hosts_and_origins_are_configurable(monkeypatch):
    runs, error, _ = _launch(
        monkeypatch,
        "--http",
        token=SYNTH_MCP_TOKEN,
        env={
            "PLUGBOARD_MCP_ALLOWED_HOSTS": "mcp.synthetic.test:*",
            "PLUGBOARD_MCP_ALLOWED_ORIGINS": "https://mcp.synthetic.test",
        },
    )
    instance = _started(runs, error, "streamable-http")
    assert instance.settings.transport_security.enable_dns_rebinding_protection
    auth = _bearer(SYNTH_MCP_TOKEN)
    ok, origin_ok, rebound = await _http_posts(
        instance,
        (auth, "mcp.synthetic.test:8766"),
        ({**auth, "Origin": "https://mcp.synthetic.test"}, "mcp.synthetic.test:8766"),
        (auth, "evil.example:8766"),
    )
    assert [ok.status_code, origin_ok.status_code, rebound.status_code] == [
        200,
        200,
        421,
    ]


def test_mcp_explicit_bind_host_keeps_rebinding_protection(monkeypatch):
    runs, error, _ = _launch(
        monkeypatch, "--http", token=SYNTH_MCP_TOKEN, env={"MCP_HOST": "0.0.0.0"}
    )
    instance = _started(runs, error, "streamable-http")
    assert instance.settings.host == "0.0.0.0"
    ts = instance.settings.transport_security
    assert ts is not None and ts.enable_dns_rebinding_protection is True


async def test_mcp_sse_wrong_token_refused(monkeypatch):
    runs, error, _ = _launch(monkeypatch, "--sse", token=SYNTH_MCP_TOKEN)
    instance = _started(runs, error, "sse")
    app = instance.sse_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=f"http://{LOOPBACK}"
    ) as c:
        resp = await c.post(
            instance.settings.message_path + "?session_id=" + "0" * 32,
            headers={"Content-Type": "application/json", **_bearer("wrong")},
            json=MCP_INIT,
        )
    assert resp.status_code == 401


def test_mcp_stdio_without_token_keeps_admin_key(monkeypatch):
    runs, error, mod = _launch(monkeypatch, token=None)
    _started(runs, error, "stdio")
    assert mod._ADMIN_KEY == SYNTH_ADMIN


def test_mcp_stdio_ignores_a_short_token(monkeypatch):
    runs, error, _ = _launch(monkeypatch, token="short")
    _started(runs, error, "stdio")


def test_mcp_network_mode_never_loads_admin_key(monkeypatch):
    runs, error, mod = _launch(
        monkeypatch,
        "--http",
        token=SYNTH_MCP_TOKEN,
        env={"LLM_ENABLER_ADMIN_KEY": SYNTH_ADMIN},
    )
    _started(runs, error, "streamable-http")
    assert mod._ADMIN_KEY == ""
    assert "PLUGBOARD_ADMIN_KEY" not in os.environ
    assert "LLM_ENABLER_ADMIN_KEY" not in os.environ


async def test_mcp_provider_status_refused_explicitly_in_network_mode(monkeypatch):
    runs, error, mod = _launch(monkeypatch, "--http", token=SYNTH_MCP_TOKEN)
    _started(runs, error, "streamable-http")
    with aioresponses() as m:
        text = await mod.provider_status()
        sent = sum(len(calls) for calls in m.requests.values())
    assert sent == 0
    assert "stdio" in text and "PLUGBOARD_ADMIN_KEY" in text


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


async def test_oauth_save_is_read_immediately_by_broker_loader(_oauth_db):
    _save("github", "gh-first-synthetic", time.time() + 3600)
    assert await _token("broker:github") == "gh-first-synthetic"
    _save("github", "gh-second-synthetic", time.time() + 3600)
    assert await _token("broker:github") == "gh-second-synthetic"


async def test_oauth_save_invalidates_derived_copilot_token(_oauth_db):
    endpoint = "https://api.github.com/copilot_internal/v2/token"
    _save("github", "gh-first-synthetic", time.time() + 3600)
    with aioresponses() as m:
        m.get(
            endpoint,
            payload={"token": "copilot-first", "expires_at": time.time() + 1800},
        )
        m.get(
            endpoint,
            payload={"token": "copilot-second", "expires_at": time.time() + 1800},
        )
        assert await _token("broker:github+copilot") == "copilot-first"
        _save("github", "gh-second-synthetic", time.time() + 3600)
        assert await _token("broker:github+copilot") == "copilot-second"


async def test_oauth_delete_of_one_source_keeps_the_others(_oauth_db):
    from plugboard.oauth_store import delete_credential

    _save("github", "gh-kept-synthetic", time.time() + 3600)
    _save("google", "google-gone-synthetic", time.time() + 3600)
    assert await _token("broker:github") == "gh-kept-synthetic"
    assert await _token("broker:google") == "google-gone-synthetic"
    assert delete_credential("google") is True
    assert await _token("broker:google") is None
    assert await _token("broker:github") == "gh-kept-synthetic"


@pytest.mark.parametrize("elapsed, expected", [(299, "kept"), (301, None)])
async def test_oauth_token_without_expiry_is_cached_five_minutes_at_most(
    _oauth_db, monkeypatch, elapsed, expected
):
    from plugboard import cli_auth

    _save("openai", "kept", None)
    assert await _token("broker:openai") == "kept"
    with sqlite3.connect(str(_oauth_db)) as conn:
        conn.execute("DELETE FROM oauth_credentials WHERE source = 'openai'")
    monkeypatch.setattr(cli_auth, "time", _ShiftedTime(elapsed))
    assert await _token("broker:openai") == expected


def test_oauth_disconnect_docstring_matches_behaviour():
    from plugboard.app import oauth_disconnect

    assert "remove related broker providers" not in (oauth_disconnect.__doc__ or "")


# ── #8 admin MFA, sessions, TOTP ────────────────────────────────────────────


@pytest.fixture
def _mfa(tmp_path, monkeypatch):
    from plugboard import admin_mfa, ratelimit

    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    # Wrong and replayed codes count as auth failures: keep them from locking
    # the in-process client address out of later tests.
    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()
    yield admin_mfa
    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()


def _enforce(mfa, monkeypatch):
    secret = mfa.generate_secret()
    mfa.enroll(secret)
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert mfa.is_required()
    return secret


def _cookie(resp):
    raw = resp.headers.get("set-cookie", "")
    marker = "plugboard_admin_session="
    assert marker in raw, f"no session cookie ({resp.status_code})"
    value = raw.split(marker, 1)[1].split(";", 1)[0]
    return {"Cookie": f"{marker}{value}"}


async def _login(client, **body):
    resp = await client.post("/auth/admin/login", json={"key": "adminkey", **body})
    assert resp.status_code == 204, f"login {resp.status_code}"
    client.cookies.clear()
    return _cookie(resp)


async def _get_keys(client, headers):
    client.cookies.clear()
    return (await client.get("/admin/keys", headers=headers)).status_code


async def test_admin_machine_totp_header_valid_then_replayed(
    admin_client, _mfa, monkeypatch
):
    secret = _enforce(_mfa, monkeypatch)
    code = _mfa._generate_code(secret)
    headers = {**ADMIN_BEARER, TOTP_HEADER: code}
    assert await _get_keys(admin_client, headers) == 200
    assert await _get_keys(admin_client, headers) in (401, 403)


async def test_admin_machine_totp_header_wrong_code_refused(
    admin_client, _mfa, monkeypatch
):
    _enforce(_mfa, monkeypatch)
    headers = {**ADMIN_BEARER, TOTP_HEADER: "000000"}
    assert await _get_keys(admin_client, headers) in (401, 403)


async def test_admin_totp_used_at_login_cannot_be_reused_in_header(
    admin_client, _mfa, monkeypatch
):
    secret = _enforce(_mfa, monkeypatch)
    code = _mfa._generate_code(secret)
    await _login(admin_client, totp_code=code)
    headers = {**ADMIN_BEARER, TOTP_HEADER: code}
    assert await _get_keys(admin_client, headers) in (401, 403)


async def test_admin_totp_older_step_refused_after_newer_accepted(
    admin_client, _mfa, monkeypatch
):
    secret = _enforce(_mfa, monkeypatch)
    await _login(admin_client, totp_code=_mfa._generate_code(secret))
    older = _mfa._generate_code(secret, at=time.time() - 30)
    resp = await admin_client.post(
        "/auth/admin/login", json={"key": "adminkey", "totp_code": older}
    )
    assert resp.status_code in (401, 403)


async def test_admin_bearer_alone_cannot_disable_mfa_when_required(
    admin_client, _mfa, monkeypatch
):
    _enforce(_mfa, monkeypatch)
    admin_client.cookies.clear()
    resp = await admin_client.delete("/admin/mfa", headers=ADMIN_BEARER)
    assert resp.status_code in (401, 403)
    assert _mfa.is_enrolled()


async def test_admin_bearer_still_works_on_providers_without_mfa(admin_client, _mfa):
    admin_client.cookies.clear()
    resp = await admin_client.get("/providers", headers=ADMIN_BEARER)
    assert resp.status_code == 200


async def test_admin_dashboard_refuses_bearer_alone_when_mfa_required(
    admin_client, _mfa, monkeypatch
):
    _enforce(_mfa, monkeypatch)
    admin_client.cookies.clear()
    resp = await admin_client.get("/dashboard", headers=ADMIN_BEARER)
    assert resp.status_code in (302, 307)
    assert "/login" in resp.headers.get("location", "")


async def test_admin_two_sessions_are_independent(admin_client, _mfa):
    first = await _login(admin_client)
    second = await _login(admin_client)
    assert first != second
    assert await _get_keys(admin_client, first) == 200
    assert await _get_keys(admin_client, second) == 200


async def test_admin_logout_revokes_only_its_own_session(admin_client, _mfa):
    first = await _login(admin_client)
    second = await _login(admin_client)
    out = await admin_client.post("/auth/admin/logout", headers=first)
    assert out.status_code == 204
    assert await _get_keys(admin_client, first) in (401, 403)
    assert await _get_keys(admin_client, second) == 200


async def test_admin_mfa_reset_revokes_every_session(admin_client, _mfa, monkeypatch):
    secret = _enforce(_mfa, monkeypatch)
    first = await _login(admin_client, totp_code=_mfa._generate_code(secret))
    second = await _login(
        admin_client, totp_code=_mfa._generate_code(secret, at=time.time() + 30)
    )
    assert await _get_keys(admin_client, second) == 200
    admin_client.cookies.clear()
    reset = await admin_client.delete("/admin/mfa", headers=first)
    assert reset.status_code == 204
    assert await _get_keys(admin_client, first) in (401, 403)
    assert await _get_keys(admin_client, second) in (401, 403)


async def test_admin_mfa_enrolment_revokes_every_session(admin_client, _mfa):
    first = await _login(admin_client)
    second = await _login(admin_client)
    secret = _mfa.generate_secret()
    admin_client.cookies.clear()
    setup = await admin_client.post(
        "/admin/mfa/setup",
        headers=first,
        json={"secret": secret, "code": _mfa._generate_code(secret)},
    )
    assert setup.status_code == 204
    assert await _get_keys(admin_client, first) in (401, 403)
    assert await _get_keys(admin_client, second) in (401, 403)


async def test_admin_cli_enrolment_revokes_every_session(admin_client, _mfa):
    session = await _login(admin_client)
    _mfa.enroll(_mfa.generate_secret())
    assert await _get_keys(admin_client, session) in (401, 403)


async def test_admin_session_without_mfa_refused_once_mfa_required(
    admin_client, _mfa, monkeypatch
):
    _mfa.enroll(_mfa.generate_secret())
    session = await _login(admin_client)
    assert await _get_keys(admin_client, session) == 200
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert await _get_keys(admin_client, session) in (401, 403)


async def test_admin_signed_but_unknown_session_refused(admin_client, _mfa):
    issued = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    payload = f"{issued}.{nonce}"
    signature = hmac.new(b"adminkey", payload.encode(), hashlib.sha256).hexdigest()
    forged = {"Cookie": f"plugboard_admin_session={payload}.{signature}"}
    assert await _get_keys(admin_client, forged) in (401, 403)


# ── #13 OVH compose ─────────────────────────────────────────────────────────

COMPOSE = ROOT / "deploy" / "ovh" / "compose.yml"
OVH_README = ROOT / "deploy" / "ovh" / "README.md"


def test_compose_mentions_no_host_home():
    assert "/home/ubuntu" not in COMPOSE.read_text(encoding="utf-8")


def test_compose_cli_homes_live_on_declared_cli_home_volume():
    doc = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    gateway = doc["services"]["gateway"]
    binds = [
        v for v in gateway.get("volumes", []) if isinstance(v, dict) or "/" in v[:1]
    ]
    assert not binds, f"gateway keeps host binds: {binds!r}"
    assert "cli-home:/home/app" in gateway["volumes"]
    assert "cli-home" in (doc.get("volumes") or {})


def test_ovh_readme_documents_migration_without_bearer_bypass():
    text = OVH_README.read_text(encoding="utf-8")
    assert "bypasses TOTP" not in text
    assert "cli-home" in text
    assert "/home/ubuntu/.codex" not in text
