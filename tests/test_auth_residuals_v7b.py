"""V7b counter-tests for the SPEC V7 residuals L1-L4.

They complement ``tests/test_spec_v7_residuals_parent.py`` (immutable):

* L1  network MCP: the admin key never sits in ``os.environ`` at ``run``, even
  when ``.env`` holds it (new or legacy name); stdio still loads it.
* L2  ``broker:github+copilot``: a save or delete of the GitHub credential
  during the Copilot exchange never leaves the derived token cached.
* L3  ``/admin/mfa/setup``: the code accepted at enrolment is consumed.
* L4  an MFA rotation, enrolment or disable refuses every session opened under
  the previous secret, even when the session store cannot be written.

All values are synthetic; no server binds, no external network (the Copilot
exchange and FastMCP.run are stubbed, HTTP runs in-process via ASGITransport).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYNTH_ADMIN = "synthetic-admin-not-real-v7b-0123456789"
SYNTH_LEGACY_ADMIN = "synthetic-legacy-admin-not-real-v7b-0123"
SYNTH_API = "synthetic-api-not-real-v7b-0123456789"
SYNTH_MCP_TOKEN = "synthetic-mcp-token-v7b-0123456789abcdef"
ADMIN_BEARER = {"Authorization": "Bearer adminkey"}
TOTP_HEADER = "X-Plugboard-Admin-TOTP"
LEGACY = "LLM_" + "ENABLER_"


# ── L1 network MCP: .env never brings the admin key back ────────────────────


def _run_mcp_copy(tmp_path, monkeypatch, *flags):
    """Run main() of a scratch copy of the tree whose .env holds admin keys.

    Returns what FastMCP.run saw: the admin key in os.environ (new and legacy
    names), the server module's copy, and the API key loaded from the same .env.
    """
    from mcp.server.fastmcp import FastMCP

    tree = tmp_path / "tree"
    shutil.copytree(
        ROOT / "plugboard",
        tree / "plugboard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tree / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts" / "run_mcp.py", tree / "scripts" / "run_mcp.py")
    (tree / ".env").write_text(
        f"PLUGBOARD_ADMIN_KEY={SYNTH_ADMIN}\n"
        f"{LEGACY}ADMIN_KEY={SYNTH_LEGACY_ADMIN}\n"
        f"PLUGBOARD_API_KEY={SYNTH_API}\n"
        f"PLUGBOARD_MCP_TOKEN={SYNTH_MCP_TOKEN}\n"
    )
    for name in (
        "PLUGBOARD_SKIP_DOTENV",
        LEGACY + "SKIP_DOTENV",
        "PLUGBOARD_ADMIN_KEY",
        LEGACY + "ADMIN_KEY",
        "PLUGBOARD_API_KEY",
        LEGACY + "API_KEY",
        "PLUGBOARD_MCP_TOKEN",
        LEGACY + "MCP_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    # load_dotenv() searches from the cwd when __main__ has no __file__
    # (python -c), else from plugboard/: both start inside the copy.
    monkeypatch.chdir(tree)
    monkeypatch.setattr(sys, "argv", ["run_mcp.py", *flags])
    seen = {}

    def fake_run(self, transport="stdio", mount_path=None):
        server = sys.modules["plugboard.mcp_server"]
        seen.update(
            transport=transport,
            env_admin=os.environ.get("PLUGBOARD_ADMIN_KEY"),
            env_legacy_admin=os.environ.get(LEGACY + "ADMIN_KEY"),
            module_admin=server._ADMIN_KEY,
            module_file=server.__file__,
            env_api=os.environ.get("PLUGBOARD_API_KEY"),
        )

    monkeypatch.setattr(FastMCP, "run", fake_run)
    saved_modules = {
        k: v
        for k, v in sys.modules.items()
        if k == "plugboard" or k.startswith("plugboard.")
    }
    for name in saved_modules:
        del sys.modules[name]
    saved_path = list(sys.path)
    saved_env = dict(os.environ)
    try:
        spec = importlib.util.spec_from_file_location(
            "v7b_run_mcp_copy", tree / "scripts" / "run_mcp.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.main()
    finally:
        for name in [
            k for k in sys.modules if k == "plugboard" or k.startswith("plugboard.")
        ]:
            del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path
        os.environ.clear()
        os.environ.update(saved_env)
    assert str(tree) in seen["module_file"]
    return seen


@pytest.mark.parametrize("flag", ["--sse", "--http"])
def test_network_mode_admin_key_from_dotenv_absent_at_run(tmp_path, monkeypatch, flag):
    seen = _run_mcp_copy(tmp_path, monkeypatch, flag)
    assert seen["transport"] in ("sse", "streamable-http")
    assert seen["env_admin"] is None
    assert seen["env_legacy_admin"] is None
    assert seen["module_admin"] == ""
    # The rest of .env still applies: only the admin key is withheld.
    assert seen["env_api"] == SYNTH_API


def test_stdio_still_loads_admin_key_from_dotenv(tmp_path, monkeypatch):
    seen = _run_mcp_copy(tmp_path, monkeypatch)
    assert seen["transport"] == "stdio"
    assert seen["env_admin"] == SYNTH_ADMIN
    assert seen["module_admin"] == SYNTH_ADMIN


# ── L2 Copilot exchange racing a credential write ───────────────────────────


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    import plugboard.cli_auth as cli_auth
    import plugboard.oauth_store as store

    monkeypatch.setattr(store, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "credentials.db")
    cli_auth.reset_loaders()
    yield cli_auth, store
    cli_auth.reset_loaders()


def _fake_copilot(monkeypatch, cli_auth, during=None):
    """Stub the Copilot exchange: the derived token names the GitHub token used.

    ``during`` runs while the exchange is in flight (once). Returns the list of
    GitHub tokens sent to the exchange endpoint.
    """
    sent = []

    class Resp:
        status = 200

        def __init__(self, github_token):
            self.github_token = github_token

        async def json(self):
            return {
                "token": "copilot-from-" + self.github_token,
                "expires_at": time.time() + 1800,
                "refresh_in": 1500,
            }

        async def text(self):
            return ""

    class Get:
        def __init__(self, headers):
            self.github_token = headers["Authorization"].removeprefix("token ")

        async def __aenter__(self):
            sent.append(self.github_token)
            if during and len(sent) == 1:
                during()
            return Resp(self.github_token)

        async def __aexit__(self, *exc):
            return False

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, headers, **kwargs):
            return Get(headers)

    monkeypatch.setattr(cli_auth.aiohttp, "ClientSession", lambda *a, **k: Session())
    return sent


def _save_github(store, account, token, **extra):
    store.save_credential(
        store.OAuthCredential(
            source="github", account=account, access_token=token, **extra
        )
    )


async def test_copilot_save_during_exchange_is_not_cached(oauth, monkeypatch):
    cli_auth, store = oauth
    _save_github(store, "a", "gho-a")
    sent = _fake_copilot(
        monkeypatch, cli_auth, during=lambda: _save_github(store, "b", "gho-b")
    )
    loader = cli_auth.get_loader("broker:github+copilot")
    assert await loader.get_token() is None
    after = await loader.get_token()
    assert sent == ["gho-a", "gho-b"]
    assert after.access_token == "copilot-from-gho-b"
    assert after.account == "b"


async def test_copilot_delete_during_exchange_returns_nothing(oauth, monkeypatch):
    cli_auth, store = oauth
    _save_github(store, "a", "gho-a")
    sent = _fake_copilot(
        monkeypatch, cli_auth, during=lambda: store.delete_credential("github")
    )
    loader = cli_auth.get_loader("broker:github+copilot")
    assert await loader.get_token() is None
    assert await loader.get_token() is None
    assert sent == ["gho-a"]


async def test_copilot_exchange_without_write_is_cached(oauth, monkeypatch):
    cli_auth, store = oauth
    _save_github(store, "a", "gho-a")
    sent = _fake_copilot(monkeypatch, cli_auth)
    loader = cli_auth.get_loader("broker:github+copilot")
    first = await loader.get_token()
    second = await loader.get_token()
    assert first.access_token == second.access_token == "copilot-from-gho-a"
    assert sent == ["gho-a"]


async def test_copilot_exchange_after_refresh_is_cached(oauth, monkeypatch):
    """The refresh writes the store itself: that write must not void the exchange."""
    cli_auth, store = oauth
    import plugboard.oauth_manager as manager

    _save_github(
        store, "a", "gho-expired", refresh_token="ghr-a", expires_at=time.time() - 10
    )

    async def fake_refresh(source):
        assert source == "github"
        _save_github(
            store,
            "a",
            "gho-fresh",
            refresh_token="ghr-a2",
            expires_at=time.time() + 3600,
        )
        return store.get_credential("github")

    monkeypatch.setattr(manager, "refresh", fake_refresh)
    sent = _fake_copilot(monkeypatch, cli_auth)
    loader = cli_auth.get_loader("broker:github+copilot")
    first = await loader.get_token()
    second = await loader.get_token()
    assert first.access_token == second.access_token == "copilot-from-gho-fresh"
    assert sent == ["gho-fresh"]


# ── L3 / L4 admin MFA and sessions ──────────────────────────────────────────


@pytest.fixture
def mfa(tmp_path, monkeypatch):
    """admin_mfa on a private file with a fixed clock; sessions in their own dir."""
    from plugboard import admin_mfa, admin_sessions, ratelimit

    mfa_dir = tmp_path / "mfa"
    mfa_dir.mkdir()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    clock = types.SimpleNamespace(t=1_800_000_000.0)
    monkeypatch.setattr(admin_mfa, "_MFA_FILE", mfa_dir / "admin_mfa.json")
    monkeypatch.setattr(admin_mfa, "time", types.SimpleNamespace(time=lambda: clock.t))
    monkeypatch.setattr(
        admin_sessions, "_path", lambda: sessions_dir / "admin_sessions.json"
    )
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()
    yield types.SimpleNamespace(
        mod=admin_mfa, clock=clock, mfa_dir=mfa_dir, sessions_dir=sessions_dir
    )
    for d in (mfa_dir, sessions_dir):
        d.chmod(0o700)
    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()


def _code(m, secret, step=0):
    return m.mod._generate_code(secret, at=m.clock.t + 30 * step)


def _enforce(m, monkeypatch):
    secret = m.mod.generate_secret()
    m.mod.enroll(secret)
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert m.mod.is_required()
    return secret


async def _login(client, **body):
    client.cookies.clear()
    resp = await client.post("/auth/admin/login", json={"key": "adminkey", **body})
    assert resp.status_code == 204, f"login {resp.status_code}"
    raw = resp.headers.get("set-cookie", "")
    client.cookies.clear()
    value = raw.split("plugboard_admin_session=", 1)[1].split(";", 1)[0]
    return {"Cookie": f"plugboard_admin_session={value}"}


async def _keys(client, headers):
    client.cookies.clear()
    return (await client.get("/admin/keys", headers=headers)).status_code


async def _setup(client, m, secret, step=0):
    client.cookies.clear()
    return await client.post(
        "/admin/mfa/setup",
        headers=ADMIN_BEARER,
        json={"secret": secret, "code": _code(m, secret, step)},
    )


async def test_setup_code_cannot_open_a_session(admin_client, mfa, monkeypatch):
    secret = mfa.mod.generate_secret()
    assert (await _setup(admin_client, mfa, secret)).status_code == 204
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    resp = await admin_client.post(
        "/auth/admin/login",
        json={"key": "adminkey", "totp_code": _code(mfa, secret)},
    )
    assert resp.status_code == 401


async def test_setup_code_refused_by_verify_helper(admin_client, mfa):
    secret = mfa.mod.generate_secret()
    assert (await _setup(admin_client, mfa, secret)).status_code == 204
    resp = await admin_client.post(
        "/admin/mfa/verify", headers=ADMIN_BEARER, json={"code": _code(mfa, secret)}
    )
    assert resp.status_code == 400


async def test_next_code_after_setup_is_accepted(admin_client, mfa, monkeypatch):
    secret = mfa.mod.generate_secret()
    assert (await _setup(admin_client, mfa, secret)).status_code == 204
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    session = await _login(admin_client, totp_code=_code(mfa, secret, 1))
    assert await _keys(admin_client, session) == 200


async def test_disable_with_read_only_session_store_refuses_old_session(
    admin_client, mfa, monkeypatch
):
    secret = _enforce(mfa, monkeypatch)
    session = await _login(admin_client, totp_code=_code(mfa, secret))
    assert await _keys(admin_client, session) == 200
    mfa.sessions_dir.chmod(0o500)
    with pytest.raises(OSError):
        mfa.mod.disable()
    assert not mfa.mod.is_enrolled()
    assert await _keys(admin_client, session) == 401


async def test_enroll_with_read_only_session_store_refuses_old_session(
    admin_client, mfa, monkeypatch
):
    secret = _enforce(mfa, monkeypatch)
    session = await _login(admin_client, totp_code=_code(mfa, secret))
    mfa.sessions_dir.chmod(0o500)
    rotated = mfa.mod.generate_secret()
    with pytest.raises(OSError):
        mfa.mod.enroll(rotated)
    assert await _keys(admin_client, session) == 401
    # The rotation took effect: the new secret opens the admin, the old does not.
    assert (
        await _keys(admin_client, {**ADMIN_BEARER, TOTP_HEADER: _code(mfa, secret, 1)})
        == 401
    )
    assert (
        await _keys(admin_client, {**ADMIN_BEARER, TOTP_HEADER: _code(mfa, rotated, 1)})
        == 200
    )


async def test_session_from_before_enrolment_never_revives(admin_client, mfa):
    session = await _login(admin_client)
    assert await _keys(admin_client, session) == 200
    mfa.sessions_dir.chmod(0o500)
    with pytest.raises(OSError):
        mfa.mod.enroll(mfa.mod.generate_secret())
    assert await _keys(admin_client, session) == 401
    with pytest.raises(OSError):
        mfa.mod.disable()
    assert await _keys(admin_client, session) == 401


async def test_mfa_store_write_failure_changes_nothing(admin_client, mfa, monkeypatch):
    secret = _enforce(mfa, monkeypatch)
    session = await _login(admin_client, totp_code=_code(mfa, secret))
    mfa.mfa_dir.chmod(0o500)
    with pytest.raises(OSError):
        mfa.mod.enroll(mfa.mod.generate_secret())
    mfa.mfa_dir.chmod(0o700)
    assert mfa.mod.is_enrolled()
    assert await _keys(admin_client, session) == 200
    assert (
        await _keys(admin_client, {**ADMIN_BEARER, TOTP_HEADER: _code(mfa, secret, 1)})
        == 200
    )


async def test_sessions_survive_totp_use_and_new_logins(admin_client, mfa, monkeypatch):
    secret = _enforce(mfa, monkeypatch)
    first = await _login(admin_client, totp_code=_code(mfa, secret))
    mfa.clock.t += 30
    assert (
        await _keys(admin_client, {**ADMIN_BEARER, TOTP_HEADER: _code(mfa, secret)})
        == 200
    )
    mfa.clock.t += 30
    second = await _login(admin_client, totp_code=_code(mfa, secret))
    assert await _keys(admin_client, first) == 200
    assert await _keys(admin_client, second) == 200
