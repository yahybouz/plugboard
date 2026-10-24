"""Parent oracles V7b: independent SPEC V7 oracles (deleg_79114069) incl. residuals L1-L4.

Copied from the reviewer file; the 2 "declared reserve confirmed" tests are removed
(they assert an accepted reserve, not a contract). Immutable assertions.

Original: Independent SPEC oracles for Plugboard V7 (#6 MCP, #7 OAuth, #8 admin MFA, #13 compose).

Written by the reviewer, outside the repository. In-memory ASGI only, no port,
no external network, synthetic keys. Mutants are monkeypatch-only.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

W = Path(__file__).resolve().parents[1]
TMP = Path(os.environ.get("SPEC_TMP") or tempfile.mkdtemp(prefix="spec-v7b-"))
ADMIN = "spec-admin-key-synthetic-0123456789abcdef"
API = "spec-api-key-synthetic-0123456789abcdef"
TOKEN = "T" * 16 + "spec-mcp-token-synthetic-xyz"  # 44 chars


# ───────────────────────────── #6 MCP ─────────────────────────────


def _load_run_mcp():
    spec = importlib.util.spec_from_file_location(
        "spec_run_mcp", W / "scripts/run_mcp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mcp_env(monkeypatch):
    """Fresh plugboard.mcp_server for each test; FastMCP.run recorded, never served."""
    from mcp.server.fastmcp import FastMCP

    monkeypatch.delitem(sys.modules, "plugboard.mcp_server", raising=False)
    runs = []

    def fake_run(self, transport="stdio", mount_path=None):
        ms = sys.modules["plugboard.mcp_server"]
        runs.append(
            {
                "transport": transport,
                "host": self.settings.host,
                "ts": self.settings.transport_security,
                "admin_mod": ms._ADMIN_KEY,
                "admin_env": os.environ.get("PLUGBOARD_ADMIN_KEY"),
                "legacy_env": os.environ.get("LLM_ENABLER_ADMIN_KEY"),
                "network": ms._NETWORK_TRANSPORT,
            }
        )

    monkeypatch.setattr(FastMCP, "run", fake_run)
    for k in (
        "PLUGBOARD_MCP_TOKEN",
        "MCP_HOST",
        "PLUGBOARD_MCP_ALLOWED_HOSTS",
        "PLUGBOARD_MCP_ALLOWED_ORIGINS",
        "PLUGBOARD_ADMIN_KEY",
        "LLM_ENABLER_ADMIN_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    yield runs
    sys.modules.pop("plugboard.mcp_server", None)


def _main(monkeypatch, *flags):
    monkeypatch.setattr(sys, "argv", ["run_mcp.py", *flags])
    mod = _load_run_mcp()
    try:
        mod.main()
        return None
    except SystemExit as exc:
        return exc.code


@pytest.mark.parametrize("flag", ["--sse", "--http"])
@pytest.mark.parametrize("token", [None, "", "x" * 31, " " * 40])
def test_6_missing_or_short_token_exits_nonzero_before_run(
    mcp_env, monkeypatch, flag, token
):
    if token is not None:
        monkeypatch.setenv("PLUGBOARD_MCP_TOKEN", token)
    code = _main(monkeypatch, flag)
    assert code not in (None, 0)
    assert mcp_env == []


def test_6_token_of_32_starts_on_loopback_with_rebinding_guard(mcp_env, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_MCP_TOKEN", "k" * 32)
    assert _main(monkeypatch, "--sse") is None
    run = mcp_env[0]
    assert run["transport"] == "sse" and run["host"] == "127.0.0.1"
    ts = run["ts"]
    assert ts.enable_dns_rebinding_protection is True
    assert all(
        h.rsplit(":", 1)[0] in {"127.0.0.1", "localhost", "[::1]"}
        for h in ts.allowed_hosts
    )
    assert all(
        o.rsplit(":", 1)[0] in {"http://127.0.0.1", "http://localhost", "http://[::1]"}
        for o in ts.allowed_origins
    )


def test_6_network_mode_admin_key_never_loaded(mcp_env, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("PLUGBOARD_ADMIN_KEY", ADMIN)
    monkeypatch.setenv("LLM_ENABLER_ADMIN_KEY", ADMIN)
    assert _main(monkeypatch, "--http") is None
    run = mcp_env[0]
    assert (
        run["admin_mod"] == ""
        and run["admin_env"] is None
        and run["legacy_env"] is None
    )


def test_6_stdio_unchanged_keeps_admin_key_and_needs_no_token(mcp_env, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_ADMIN_KEY", ADMIN)
    assert _main(monkeypatch) is None
    run = mcp_env[0]
    assert (
        run["transport"] == "stdio"
        and run["admin_mod"] == ADMIN
        and run["network"] is False
    )


async def test_6_provider_status_network_sends_nothing(mcp_env, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("PLUGBOARD_ADMIN_KEY", ADMIN)
    _main(monkeypatch, "--sse")
    ms = sys.modules["plugboard.mcp_server"]
    sent = []

    async def spy(path, admin=False):
        sent.append((path, admin, ms._auth_headers(admin=admin)))
        return {}

    monkeypatch.setattr(ms, "_api_get", spy)
    out = await ms.provider_status()
    assert "not available" in out and sent == []
    assert "Authorization" not in ms._auth_headers(admin=True)


def test_6_dotenv_reload_defeats_the_admin_key_pop(monkeypatch):
    """Copy of the tree in scratch with a SYNTHETIC .env: does run_mcp's pop survive
    plugboard/__init__.py's load_dotenv()? (contract: never loaded in network mode)"""
    from mcp.server.fastmcp import FastMCP

    tree = TMP / "dotenv-tree"
    shutil.rmtree(tree, ignore_errors=True)
    shutil.copytree(
        W / "plugboard",
        tree / "plugboard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(W / "scripts/run_mcp.py", tree / "scripts/run_mcp.py")
    (tree / ".env").write_text(f"PLUGBOARD_ADMIN_KEY={ADMIN}\n")
    saved = {
        k: v
        for k, v in sys.modules.items()
        if k == "plugboard" or k.startswith("plugboard.")
    }
    for k in saved:
        del sys.modules[k]
    monkeypatch.delenv("PLUGBOARD_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("PLUGBOARD_ADMIN_KEY", raising=False)
    monkeypatch.setenv("PLUGBOARD_MCP_TOKEN", TOKEN)
    monkeypatch.setattr(sys, "argv", ["run_mcp.py", "--sse"])
    seen = {}

    def fake_run(self, transport="stdio", mount_path=None):
        ms = sys.modules["plugboard.mcp_server"]
        seen.update(
            env=os.environ.get("PLUGBOARD_ADMIN_KEY"),
            mod=ms._ADMIN_KEY,
            file=ms.__file__,
        )

    monkeypatch.setattr(FastMCP, "run", fake_run)
    old_path = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            "spec_run_mcp_copy", tree / "scripts/run_mcp.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()
    finally:
        for k in [
            k for k in sys.modules if k == "plugboard" or k.startswith("plugboard.")
        ]:
            del sys.modules[k]
        sys.modules.update(saved)
        sys.path[:] = old_path
        os.environ.pop("PLUGBOARD_ADMIN_KEY", None)
    (TMP / "dotenv-oracle.json").write_text(
        json.dumps(
            {
                "env_admin_key_present_at_run": seen.get("env") == ADMIN,
                "module_admin_key_present_at_run": bool(seen.get("mod")),
                "module_file": seen.get("file"),
            },
            indent=1,
        )
    )
    assert str(tree) in seen["file"]
    assert seen["mod"] == ""  # never sent: module copy is cleared
    # Contract wording "jamais chargée": the process env must not hold it either.
    assert seen["env"] is None, "PLUGBOARD_ADMIN_KEY re-loaded from .env after the pop"


@pytest.fixture
def mcp_net(monkeypatch):
    monkeypatch.delitem(sys.modules, "plugboard.mcp_server", raising=False)
    import plugboard.mcp_server as ms

    ms.configure_network_transport(TOKEN)
    yield ms
    sys.modules.pop("plugboard.mcp_server", None)


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "spec", "version": "0"},
    },
}
ACC = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


async def test_6_streamable_http_bearer_host_origin(mcp_net):
    ms = mcp_net
    app = ms.mcp.streamable_http_app()
    res = {}
    async with ms.mcp.session_manager.run():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1:8766"
        ) as c:

            async def post(name, **h):
                r = await asyncio.wait_for(
                    c.post("/mcp", json=INIT, headers={**ACC, **h}), 10
                )
                res[name] = r.status_code

            await post("none")
            await post("wrong_same_len", Authorization="Bearer " + "U" + TOKEN[1:])
            await post("prefix", Authorization="Bearer " + TOKEN[:-1])
            await post("extended", Authorization="Bearer " + TOKEN + "x")
            await post("lower_scheme", Authorization="bearer " + TOKEN)
            await post("ok", Authorization="Bearer " + TOKEN)
            await post(
                "foreign_host", Authorization="Bearer " + TOKEN, Host="evil.example"
            )
            await post(
                "foreign_origin",
                Authorization="Bearer " + TOKEN,
                Origin="http://evil.example",
            )
    (TMP / "mcp-http-statuses.json").write_text(json.dumps(res, indent=1))
    for k in ("none", "wrong_same_len", "prefix", "extended"):
        assert res[k] == 401, (k, res)
    assert res["ok"] == 200
    assert res["foreign_host"] in (400, 403, 421)
    assert res["foreign_origin"] in (400, 403)


async def test_6_sse_app_bearer_and_host(mcp_net):
    ms = mcp_net
    app = ms.mcp.sse_app()
    res = {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1:8766"
    ) as c:
        p = "/messages/?session_id=" + "0" * 32
        res["post_none"] = (await c.post(p, json={})).status_code
        res["post_wrong"] = (
            await c.post(p, json={}, headers={"Authorization": "Bearer " + "x" * 44})
        ).status_code
        res["get_sse_none"] = (await asyncio.wait_for(c.get("/sse"), 5)).status_code
        res["get_sse_wrong"] = (
            await asyncio.wait_for(
                c.get("/sse", headers={"Authorization": "Bearer nope"}), 5
            )
        ).status_code
        res["post_ok"] = (
            await c.post(p, json={}, headers={"Authorization": "Bearer " + TOKEN})
        ).status_code
        res["post_ok_foreign_host"] = (
            await c.post(
                p,
                json={},
                headers={"Authorization": "Bearer " + TOKEN, "Host": "evil.example"},
            )
        ).status_code
    # GET /sse with a foreign Host: mcp sends the refusal then raises; capture it raw.
    sent = []

    async def receive():
        await asyncio.sleep(5)
        return {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/sse",
        "raw_path": b"/sse",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"evil.example"),
            (b"authorization", ("Bearer " + TOKEN).encode()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8766),
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), 5)
    except ValueError:
        pass
    starts = [m["status"] for m in sent if m["type"] == "http.response.start"]
    res["get_sse_ok_foreign_host"] = starts[0] if starts else None
    (TMP / "mcp-sse-statuses.json").write_text(json.dumps(res, indent=1))
    assert (
        res["post_none"]
        == res["post_wrong"]
        == res["get_sse_none"]
        == res["get_sse_wrong"]
        == 401
    )
    assert res["post_ok"] != 401
    assert res["post_ok_foreign_host"] in (400, 403, 421)
    assert res["get_sse_ok_foreign_host"] in (400, 403, 421)


async def test_6_constant_time_compare_used_and_closed_by_default(mcp_net, monkeypatch):
    ms = mcp_net
    calls = []
    real = ms.hmac.compare_digest

    def spy(a, b):
        calls.append((type(a).__name__, len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(ms.hmac, "compare_digest", spy)
    assert await ms._TOKEN_VERIFIER.verify_token(TOKEN) is not None
    assert await ms._TOKEN_VERIFIER.verify_token("x" * 44) is None
    assert calls and calls[0][0] == "bytes"
    fresh = ms._StaticBearerVerifier()
    assert (
        await fresh.verify_token("") is None and await fresh.verify_token(TOKEN) is None
    )


async def test_6_mutant_equality_compare_is_detected_by_spy(mcp_net, monkeypatch):
    """Mutant: verifier uses '=='. The spy-based oracle must notice (no compare_digest call)."""
    ms = mcp_net
    calls = []
    monkeypatch.setattr(
        ms.hmac, "compare_digest", lambda a, b: calls.append(1) or a == b
    )

    async def mutant(self, token):
        return (
            ms.AccessToken(token="t", client_id="c", scopes=[])
            if token.encode() == self._token
            else None
        )

    monkeypatch.setattr(ms._StaticBearerVerifier, "verify_token", mutant)
    await ms._TOKEN_VERIFIER.verify_token(TOKEN)
    assert calls == []  # oracle of test_6_constant_time... would fail on this mutant


# ───────────────────────────── #7 OAuth ─────────────────────────────


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    import plugboard.cli_auth as ca
    import plugboard.oauth_store as st

    monkeypatch.setattr(st, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(st, "_DB_PATH", tmp_path / "credentials.db")
    ca.reset_loaders()
    yield ca, st
    ca.reset_loaders()


def _prime(ca, sources):
    from plugboard.cli_auth import TokenBundle

    loaders = {}
    for s in sources:
        ld = ca.get_loader(s)
        ld._store(TokenBundle(access_token="old-" + s, expires_at=time.time() + 3600))
        loaders[s] = ld
    return loaders


SRC = [
    "broker:github",
    "broker:github+copilot",
    "broker:google",
    "broker:openai",
    "gh",
    "gh+copilot",
    "codex",
]


@pytest.mark.parametrize("op", ["save", "delete"])
def test_7_write_invalidates_source_and_derived_only(oauth, op):
    ca, st = oauth
    ld = _prime(ca, SRC)
    if op == "save":
        st.save_credential(
            st.OAuthCredential(source="github", account="b", access_token="new")
        )
    else:
        st.delete_credential("github")
    dropped = sorted(s for s, loader in ld.items() if loader._cache is None)
    assert dropped == ["broker:github", "broker:github+copilot"]


def test_7_delete_google_keeps_github(oauth):
    ca, st = oauth
    ld = _prime(ca, SRC)
    st.delete_credential("google")
    assert sorted(s for s, loader in ld.items() if loader._cache is None) == [
        "broker:google"
    ]


async def test_7_save_then_read_serves_new_account(oauth):
    ca, st = oauth
    st.save_credential(
        st.OAuthCredential(source="github", account="a", access_token="tok-a")
    )
    assert (await ca.get_loader("broker:github").get_token()).access_token == "tok-a"
    st.save_credential(
        st.OAuthCredential(source="github", account="b", access_token="tok-b")
    )
    assert (await ca.get_loader("broker:github").get_token()).access_token == "tok-b"
    st.delete_credential("github")
    assert await ca.get_loader("broker:github").get_token() is None


async def test_7_mutant_no_invalidation_is_killed(oauth, monkeypatch):
    ca, st = oauth
    monkeypatch.setattr(st, "_invalidate_loaders", lambda s: None)
    st.save_credential(
        st.OAuthCredential(source="github", account="a", access_token="tok-a")
    )
    await ca.get_loader("broker:github").get_token()
    st.delete_credential("github")
    assert (
        await ca.get_loader("broker:github").get_token()
    ) is not None  # stale: oracle above kills it


async def test_7_no_expiry_ttl_at_most_5_min(oauth, monkeypatch):
    ca, _ = oauth
    from plugboard.cli_auth import CLIAuthLoader, TokenBundle

    assert ca.NO_EXPIRY_CACHE_TTL <= 300
    fetches = []

    class L(CLIAuthLoader):
        def _fetch_token(self):
            fetches.append(1)
            return TokenBundle(access_token=f"t{len(fetches)}", expires_at=None)

    clock = [1000.0]
    monkeypatch.setattr(ca.time, "monotonic", lambda: clock[0])
    ld = L()
    assert (await ld.get_token()).access_token == "t1"
    clock[0] += 299
    assert (await ld.get_token()).access_token == "t1"
    clock[0] += 1
    assert (await ld.get_token()).access_token == "t2"


async def test_7_race_copilot_exchange_inflight_during_delete(oauth, monkeypatch):
    """Residual: a disconnect landing during the Copilot exchange leaves a derived token cached."""
    ca, st = oauth
    st.save_credential(
        st.OAuthCredential(source="github", account="a", access_token="gho")
    )

    class Resp:
        status = 200

        async def json(self):
            return {"token": "copilot-derived", "expires_at": time.time() + 1800}

    class Get:
        async def __aenter__(self):
            st.delete_credential("github")  # Disconnect while the exchange is in flight
            return Resp()

        async def __aexit__(self, *a):
            return False

    class Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, *a, **k):
            return Get()

    monkeypatch.setattr(ca.aiohttp, "ClientSession", lambda *a, **k: Sess())
    ld = ca.get_loader("broker:github+copilot")
    await ld.get_token()
    assert st.get_credential("github") is None
    after = await ld.get_token()
    (TMP / "oauth-race.json").write_text(
        json.dumps({"served_after_delete": bool(after)})
    )
    assert after is None, (
        "derived Copilot token still served after the source was deleted"
    )


# ───────────────────────────── #8 admin MFA ─────────────────────────────


class Clock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


@pytest.fixture
async def adm(tmp_path, monkeypatch):
    import plugboard.admin_mfa as mfa
    import plugboard.app as appmod
    import plugboard.config as cfg
    import plugboard.ratelimit as rl

    data = tmp_path / "data"
    data.mkdir()
    (data / "providers.json").write_text(json.dumps({"providers": []}))
    monkeypatch.setenv("PLUGBOARD_DATA", str(data))
    monkeypatch.setattr(cfg, "PROVIDERS_FILE", data / "providers.json")
    monkeypatch.setattr(cfg, "PROVIDERS_LOCK", data / "providers.json.lock")
    monkeypatch.setattr(mfa, "_MFA_FILE", data / "admin_mfa.json")
    monkeypatch.setattr(appmod, "_ADMIN_KEY", ADMIN)
    monkeypatch.setattr(appmod, "_API_KEY", API)
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    monkeypatch.setattr(rl, "_LIMIT", 0)
    rl._buckets.clear()
    rl._client_buckets.clear()
    rl._auth_buckets.clear()
    rl._lockouts.clear()
    clock = Clock(1_800_000_000.0)
    monkeypatch.setattr(mfa, "time", types.SimpleNamespace(time=clock.time))
    async with AsyncClient(
        transport=ASGITransport(app=appmod.app), base_url="https://test"
    ) as c:
        yield types.SimpleNamespace(
            c=c, mfa=mfa, app=appmod, clock=clock, mp=monkeypatch, data=data
        )
    rl._auth_buckets.clear()
    rl._lockouts.clear()


B = {"Authorization": "Bearer " + ADMIN}


def _enforce(a):
    secret = a.mfa.generate_secret()
    a.mfa.enroll(secret)
    a.mp.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert a.mfa.is_required()
    return secret


def _code(a, secret, step=0):
    return a.mfa._generate_code(secret, at=a.clock.t + 30 * step)


def _admin_routes(appmod):
    out = []
    for r in appmod.app.routes:
        dep = getattr(r, "dependant", None)
        if dep is None:
            continue
        calls = {d.call for d in dep.dependencies}
        out.append((r.path, sorted(r.methods or []), appmod._require_admin in calls))
    return out


def test_8_every_admin_like_route_requires_admin(adm):
    rows = _admin_routes(adm.app)
    pref = (
        "/admin",
        "/providers",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/health/detailed",
    )
    missing = [p for p, m, g in rows if p.startswith(pref) and not g]
    (TMP / "admin-routes.json").write_text(json.dumps(rows, indent=1))
    assert missing == []


async def test_8_bearer_alone_refused_on_all_admin_routes(adm):
    _enforce(adm)
    bad = {}
    for path, methods, guarded in _admin_routes(adm.app):
        if not guarded:
            continue
        url = re.sub(r"\{[^}]+\}", "x", path)
        for m in methods:
            if m == "HEAD":
                continue
            r = await adm.c.request(m, url, headers=B)
            if r.status_code != 401:
                bad[f"{m} {path}"] = r.status_code
    assert bad == {}
    assert adm.mfa.is_enrolled()  # DELETE /admin/mfa did not run


async def test_8_dashboard_html_refuses_bearer_alone(adm):
    secret = _enforce(adm)
    for p in ("/dashboard", "/dashboard/index.html"):
        r = await adm.c.get(p, headers=B)
        assert r.status_code in (302, 307) and "/login" in r.headers["location"]
    r = await adm.c.get(
        "/dashboard/index.html",
        headers={**B, "X-Plugboard-Admin-TOTP": _code(adm, secret)},
    )
    assert r.status_code in (200, 404)


async def test_8_totp_header_single_use_case_insensitive_and_scheme(adm):
    secret = _enforce(adm)
    c0 = _code(adm, secret)
    assert (
        await adm.c.get("/admin/keys", headers={**B, "x-plugboard-admin-totp": c0})
    ).status_code == 200
    assert (
        await adm.c.get("/admin/keys", headers={**B, "X-PLUGBOARD-ADMIN-TOTP": c0})
    ).status_code == 401
    lower = {
        "Authorization": "bearer " + ADMIN,
        "X-Plugboard-Admin-TOTP": _code(adm, secret, 1),
    }
    assert (await adm.c.get("/admin/keys", headers=lower)).status_code == 401
    # wrong key + valid code
    r = await adm.c.get(
        "/admin/keys",
        headers={
            "Authorization": "Bearer nope",
            "X-Plugboard-Admin-TOTP": _code(adm, secret, 1),
        },
    )
    assert r.status_code == 401


async def test_8_window_plus_minus_one(adm):
    secret = _enforce(adm)

    def g(s):
        return adm.c.get(
            "/admin/keys",
            headers={**B, "X-Plugboard-Admin-TOTP": _code(adm, secret, s)},
        )

    assert (await g(+2)).status_code == 401
    assert (await g(+1)).status_code == 200
    assert (await g(0)).status_code == 401  # older than last accepted
    assert (await g(-1)).status_code == 401
    adm.clock.t += 60
    assert (await g(0)).status_code == 200


async def test_8_replay_and_wrong_codes_count_into_lockout(adm):
    secret = _enforce(adm)
    c0 = _code(adm, secret)
    assert (
        await adm.c.get("/admin/keys", headers={**B, "X-Plugboard-Admin-TOTP": c0})
    ).status_code == 200
    statuses = [
        (
            await adm.c.get("/admin/keys", headers={**B, "X-Plugboard-Admin-TOTP": c0})
        ).status_code
        for _ in range(5)
    ]
    assert statuses[:4] == [401] * 4
    adm.clock.t += 60
    fresh = _code(adm, secret)
    r = await adm.c.get("/admin/keys", headers={**B, "X-Plugboard-Admin-TOTP": fresh})
    assert r.status_code == 429  # locked, even with a valid fresh code


async def test_8_store_write_failure_refuses(adm):
    secret = _enforce(adm)

    def boom(data):
        raise OSError("disk full")

    adm.mp.setattr(adm.mfa, "_write", boom)
    r = await adm.c.get(
        "/admin/keys", headers={**B, "X-Plugboard-Admin-TOTP": _code(adm, secret)}
    )
    assert r.status_code == 401


async def _login(a, secret=None, step=0):
    body = {"key": ADMIN}
    if secret:
        body["totp_code"] = _code(a, secret, step)
    r = await a.c.post("/auth/admin/login", json=body)
    assert r.status_code in (200, 204), r.text
    return r.cookies.get("plugboard_admin_session") or re.search(
        r"plugboard_admin_session=([^;]+)", r.headers["set-cookie"]
    ).group(1)


async def _keys(a, cookie):
    a.c.cookies.clear()
    r = await a.c.get("/admin/keys", cookies={"plugboard_admin_session": cookie})
    a.c.cookies.clear()
    return r.status_code


async def test_8_sessions_logout_own_only_then_reset_revokes_all(adm):
    secret = _enforce(adm)
    s1 = await _login(adm, secret, 0)
    adm.clock.t += 30
    s2 = await _login(adm, secret, 0)
    adm.clock.t += 30
    s3 = await _login(adm, secret, 0)
    assert [await _keys(adm, s) for s in (s1, s2, s3)] == [200, 200, 200]
    adm.c.cookies.clear()
    r = await adm.c.post("/auth/admin/logout", cookies={"plugboard_admin_session": s1})
    assert r.status_code == 204
    assert [await _keys(adm, s) for s in (s1, s2, s3)] == [401, 200, 200]
    # reset = disable + enroll through the API with session s2
    adm.c.cookies.clear()
    r = await adm.c.delete("/admin/mfa", cookies={"plugboard_admin_session": s2})
    assert r.status_code == 204
    assert [await _keys(adm, s) for s in (s2, s3)] == [401, 401]


async def test_8_enroll_revokes_all(adm):
    secret = _enforce(adm)
    s = await _login(adm, secret)
    adm.mfa.enroll(adm.mfa.generate_secret())
    assert await _keys(adm, s) == 401


async def test_8_session_without_totp_refused_once_required(adm):
    secret = adm.mfa.generate_secret()
    adm.mfa.enroll(secret)  # flag off -> not required
    s = await _login(adm)
    assert await _keys(adm, s) == 200
    adm.mp.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert await _keys(adm, s) == 401


async def test_8_old_cookie_formats_refused(adm):
    import hashlib
    import hmac as _h

    _enforce(adm)
    issued = str(int(time.time()))
    payload = f"{issued}.legacynonce"
    sig = _h.new(ADMIN.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert (
        await _keys(adm, f"{payload}.{sig}") == 401
    )  # V6-style signed but unregistered
    adm.c.cookies.clear()
    r = await adm.c.get("/admin/keys", cookies={"llm_admin_key": ADMIN})
    assert r.status_code == 401


async def test_8_unreadable_session_store_fails_closed(adm):
    secret = _enforce(adm)
    s = await _login(adm, secret)
    (adm.data / "admin_sessions.json").write_text("{not json")
    assert await _keys(adm, s) == 401


async def test_8_no_mfa_bearer_unchanged(adm):
    for p in ("/admin/keys", "/providers", "/metrics", "/admin/mfa/status"):
        assert (await adm.c.get(p, headers=B)).status_code == 200, p


async def test_8_setup_code_is_not_recorded_as_used(adm):
    """Info: the code accepted by /admin/mfa/setup is not consumed (no last_step)."""
    secret = adm.mfa.generate_secret()
    code = _code(adm, secret)
    r = await adm.c.post(
        "/admin/mfa/setup", headers=B, json={"secret": secret, "code": code}
    )
    assert r.status_code == 204
    adm.mp.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    r = await adm.c.get("/admin/keys", headers={**B, "X-Plugboard-Admin-TOTP": code})
    (TMP / "setup-code-reuse.json").write_text(
        json.dumps({"reuse_status": r.status_code})
    )
    assert r.status_code == 401, "setup code accepted again after enrolment"


async def test_8_revoke_all_write_failure_on_rotation(adm):
    """Edge: rotation while the session store cannot be written."""
    import plugboard.admin_sessions as ss

    secret = _enforce(adm)
    s = await _login(adm, secret)

    def boom(sessions):
        raise OSError("read-only fs")

    adm.mp.setattr(ss, "_save", boom)
    raised = False
    try:
        adm.mfa.enroll(adm.mfa.generate_secret())
    except OSError:
        raised = True
    adm.mp.undo  # noqa: B018 (keep patch)
    status = await _keys(adm, s)
    (TMP / "rotation-write-failure.json").write_text(
        json.dumps({"enroll_raised": raised, "old_session_status": status})
    )
    assert status == 401, (
        "session opened under the old secret survives a failed revoke_all"
    )


# ───────────────────────────── #13 compose ─────────────────────────────


def test_13_compose_no_host_home_bind_and_named_volume():
    import yaml

    doc = yaml.safe_load((W / "deploy/ovh/compose.yml").read_text())
    for name, svc in doc["services"].items():
        for v in svc.get("volumes", []):
            src = v.get("source") if isinstance(v, dict) else v.split(":")[0]
            kind = (
                v.get("type")
                if isinstance(v, dict)
                else ("bind" if src.startswith(("/", ".", "~", "$")) else "volume")
            )
            assert kind != "bind", (name, v)
            assert not re.search(
                r"/home/|~|\$\{?HOME|\.codex|\.claude|\.gemini", str(src)
            ), (name, v)
            if kind == "volume":
                assert src in (doc.get("volumes") or {}), (name, src)
    assert "cli-home" in doc["volumes"]
    raw = (W / "deploy/ovh/compose.yml").read_text()
    assert "/home/ubuntu" not in raw


def test_13_ovh_readme_has_no_secret_and_no_bypass_claim():
    txt = (W / "deploy/ovh/README.md").read_text()
    pats = [
        r"sk-[A-Za-z0-9_-]{16,}",
        r"gh[pousr]_[A-Za-z0-9]{20,}",
        r"\bpb_[A-Za-z0-9_-]{16,}",
        r"\blle_[A-Za-z0-9_-]{16,}",
        r"AKIA[0-9A-Z]{16}",
        r"-----BEGIN",
        r"otpauth://totp/[^\s`)]*secret=",
        r"(ADMIN_KEY|API_KEY|ENCRYPTION_KEY|TOKEN|PASSWORD)\s*=\s*[A-Za-z0-9+/_=-]{12,}",
        r"\b[A-Z2-7]{32}\b",
    ]
    hits = [p for p in pats if re.search(p, txt)]
    assert hits == []
    assert not re.search(r"bypass(es)? TOTP", txt, re.I)
