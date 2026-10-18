"""Local auto-login: only a local install opened in the operator's browser.

The Host header is client-controlled: before this fix, any client able to
reach the port could send ``Host: localhost`` and receive an admin session.
A deployed instance (public bind, proxy, forwarder) must always ask for a key.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from plugboard import app as app_module

_COOKIE = app_module._ADMIN_COOKIE_NAME
_LOGGER = app_module.log.name
# What a browser sends when the user types the URL or opens a bookmark.
_NAV = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"}
# Written out here (not read from the app) so that dropping one from the
# production list fails a test.
_PROXY_HEADERS = [
    "Forwarded",
    "Via",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Forwarded-Port",
    "X-Forwarded-Server",
    "X-Original-Forwarded-For",
    "X-Real-IP",
    "X-Client-IP",
    "X-Cluster-Client-IP",
    "CF-Connecting-IP",
    "True-Client-IP",
    "Fly-Client-IP",
    "Fastly-Client-IP",
]


@pytest.fixture
def audit_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 0)
    rl._buckets.clear()
    app_module._refusal_audit_seen.clear()
    app_module._warned_auto_login_values.clear()
    calls: list = []
    monkeypatch.setattr(
        app_module, "_audit", lambda action, **kw: calls.append((action, kw))
    )
    monkeypatch.setattr(app_module, "_ADMIN_KEY", "adminkey-auto-login")
    monkeypatch.setenv("PLUGBOARD_BIND_HOST", "127.0.0.1")
    monkeypatch.delenv("PLUGBOARD_LOCAL_AUTO_LOGIN", raising=False)
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    return calls


async def _get_login(
    peer: str,
    base_url: str,
    headers: dict | None = None,
    app=None,
    path: str = "/login",
):
    transport = ASGITransport(app=app or app_module.app, client=(peer, 50123))
    async with AsyncClient(transport=transport, base_url=base_url) as client:
        return await client.get(path, headers=headers or {}, follow_redirects=False)


def _granted(resp) -> bool:
    return resp.status_code == 302 and _COOKIE in resp.cookies


def _key_form(resp) -> bool:
    """Refused requests still get the normal ADMIN_KEY form, never an error."""
    return (
        resp.status_code == 200
        and "enter your admin key" in resp.text.lower()
        and _COOKIE not in resp.cookies
    )


@pytest.mark.parametrize(
    ("peer", "base_url"),
    [
        ("127.0.0.1", "http://localhost:8765"),
        ("::1", "http://[::1]:8765"),
        ("::ffff:127.0.0.1", "http://127.0.0.1:8765"),
    ],
)
async def test_local_browser_navigation_gets_session(audit_calls, peer, base_url):
    resp = await _get_login(peer, base_url, _NAV)

    assert _granted(resp)
    assert resp.headers["location"] == "/dashboard"
    assert [a for a, _ in audit_calls] == ["auth.auto_login"]
    assert audit_calls[0][1]["ip"] == peer


async def test_same_origin_redirect_gets_session(audit_calls):
    headers = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate"}

    assert _granted(await _get_login("127.0.0.1", "http://localhost:8765", headers))


async def test_local_operator_opening_root_lands_on_dashboard(audit_calls):
    # Added by the /ship coverage audit.
    # Value: protects=a local operator opening http://localhost:8765/ ends on
    # the dashboard with a session; fails_when=the auto-login cookie is not
    # accepted along / -> /dashboard -> /login?next= -> /dashboard (Secure flag,
    # path, name) or ?next= breaks the guard; why_new=other tests stop at the
    # /login 302; seam=none
    transport = ASGITransport(app=app_module.app, client=("127.0.0.1", 50123))
    async with AsyncClient(
        transport=transport, base_url="http://localhost:8765"
    ) as client:
        resp = await client.get("/", headers=_NAV, follow_redirects=True)

    assert resp.status_code == 200
    assert resp.url.path == "/dashboard"
    assert "/login" in [r.url.path for r in resp.history]
    assert 'id="logout-btn"' in resp.text
    assert [a for a, _ in audit_calls] == ["auth.auto_login"]


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "::1", "127.0.0.2"])
async def test_auto_mode_accepts_every_loopback_bind(audit_calls, monkeypatch, host):
    monkeypatch.setenv("PLUGBOARD_BIND_HOST", host)

    assert _granted(await _get_login("127.0.0.1", "http://localhost:8765", _NAV))


async def test_spoofed_host_from_remote_peer_gets_no_session(audit_calls):
    resp = await _get_login("203.0.113.7", "http://localhost:8765", _NAV)

    assert _key_form(resp)
    assert audit_calls == [
        (
            "auth.auto_login_refused",
            {"details": "reason=non_loopback_peer host=localhost", "ip": "203.0.113.7"},
        )
    ]


@pytest.mark.parametrize(("window_s", "audited"), [(None, 1), (0.0, 5)])
async def test_repeated_refusals_are_audited_once_per_window(
    audit_calls, monkeypatch, window_s, audited
):
    # Extended by the /ship coverage audit: the 0 s row proves the window ends.
    # Value: protects=a peer still spoofing localhost after the audit window is
    # audited again; fails_when=refusal dedupe ignores time (e.g. a plain set of
    # peers); why_new=the default row only proves the first refusal is audited;
    # seam=none
    if window_s is not None:
        monkeypatch.setattr(app_module, "_REFUSAL_AUDIT_WINDOW_S", window_s)
    for _ in range(5):
        await _get_login("203.0.113.7", "http://localhost:8765", _NAV)

    assert len(audit_calls) == audited


@pytest.mark.parametrize("header", _PROXY_HEADERS)
async def test_loopback_peer_behind_proxy_gets_no_session(audit_calls, header):
    # A reverse proxy on the same machine connects from 127.0.0.1 for every
    # client, so a loopback peer with a proxy header proves nothing.
    resp = await _get_login(
        "127.0.0.1", "http://localhost:8765", headers={**_NAV, header: "127.0.0.1"}
    )

    assert _key_form(resp)


async def test_trusted_proxy_rewriting_peer_still_gets_no_session(audit_calls):
    # FORWARDED_ALLOW_IPS=* makes uvicorn rewrite the peer from
    # X-Forwarded-For; the header stays visible, so auto-login still refuses.
    app = ProxyHeadersMiddleware(app_module.app, trusted_hosts="*")
    resp = await _get_login(
        "203.0.113.7",
        "http://localhost:8765",
        headers={**_NAV, "X-Forwarded-For": "127.0.0.1"},
        app=app,
    )

    assert _key_form(resp)


async def test_headerless_local_proxy_on_public_bind_gets_no_session(
    audit_calls, monkeypatch
):
    # nginx's default proxy_pass: peer 127.0.0.1, Host 127.0.0.1:8765, no
    # X-Forwarded-*. Indistinguishable from a browser, so a public bind is
    # treated as a deployment.
    monkeypatch.setenv("PLUGBOARD_BIND_HOST", "0.0.0.0")

    assert _key_form(await _get_login("127.0.0.1", "http://127.0.0.1:8765", _NAV))


async def test_unknown_bind_is_treated_as_public(audit_calls, monkeypatch, caplog):
    # Started by `uvicorn --host 0.0.0.0` with HOST=127.0.0.1 from .env: only
    # run.py exports the real bind, so HOST alone must not count as local.
    monkeypatch.delenv("PLUGBOARD_BIND_HOST", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.1")

    with caplog.at_level("INFO", logger=_LOGGER):
        resp = await _get_login("127.0.0.1", "http://127.0.0.1:8765", _NAV)

    assert _key_form(resp)
    assert "reason=not_local_install" in caplog.text
    assert audit_calls == []


async def test_explicit_opt_in_allows_public_bind(audit_calls, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("PLUGBOARD_LOCAL_AUTO_LOGIN", "1")

    assert _granted(await _get_login("127.0.0.1", "http://localhost:8765", _NAV))


@pytest.mark.parametrize(
    ("peer", "base_url", "extra"),
    [
        ("203.0.113.7", "http://localhost:8765", {}),
        ("127.0.0.1", "http://localhost:8765", {"X-Forwarded-For": "203.0.113.7"}),
        ("127.0.0.1", "http://gateway.example.com", {}),
        ("127.0.0.1", "http://localhost:8765", {"Sec-Fetch-Site": "same-site"}),
    ],
)
async def test_opt_in_on_public_bind_keeps_every_other_guard(
    audit_calls, monkeypatch, peer, base_url, extra
):
    monkeypatch.setenv("PLUGBOARD_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("PLUGBOARD_LOCAL_AUTO_LOGIN", "1")

    assert _key_form(await _get_login(peer, base_url, {**_NAV, **extra}))


async def test_loopback_peer_with_public_host_gets_no_session(audit_calls):
    assert _key_form(await _get_login("127.0.0.1", "http://gateway.example.com", _NAV))


@pytest.mark.parametrize("host", ["x_y.attacker.example:8765", "", "[1:2]"])
async def test_malformed_or_missing_host_gets_no_session(audit_calls, host):
    # Starlette falls back to the local socket address for a Host it cannot
    # parse; that must not read as "localhost". "[1:2]" used to raise a 500.
    async def app(scope, receive, send):
        scope["headers"] = [(k, v) for k, v in scope["headers"] if k != b"host"]
        if host:
            scope["headers"].append((b"host", host.encode()))
        await app_module.app(scope, receive, send)

    assert _key_form(await _get_login("127.0.0.1", "http://localhost:8765", _NAV, app))


async def test_request_without_client_address_gets_no_session(audit_calls):
    # Added by the /ship coverage audit.
    # Value: protects=a request with no peer address (uvicorn --uds behind a
    # proxy) gets the key form, never a session or a 500; fails_when=a missing
    # request.client is read as local or dereferenced unguarded;
    # why_new=every other test gives the ASGI scope a client; seam=none
    async def app(scope, receive, send):
        scope["client"] = None
        await app_module.app(scope, receive, send)

    resp = await _get_login("127.0.0.1", "http://localhost:8765", _NAV, app)

    assert _key_form(resp)
    assert "auth.auto_login" not in [a for a, _ in audit_calls]


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl, requests, aiohttp: no fetch metadata
        {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate"},
        {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"},
        {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "cors"},
    ],
)
async def test_non_navigation_or_other_site_gets_no_session(
    audit_calls, caplog, headers
):
    # A page on another localhost port is "same-site": it must not be able to
    # mint a session in the operator's browser. Logged, not audited (benign
    # tools trigger it too).
    with caplog.at_level("INFO", logger=_LOGGER):
        resp = await _get_login("127.0.0.1", "http://localhost:8765", headers)

    assert _key_form(resp)
    assert "reason=not_navigation" in caplog.text
    assert audit_calls == []


@pytest.mark.parametrize(
    ("peer", "base_url", "extra", "reason", "audited"),
    [
        ("127.0.0.1", "http://localhost:8765", {"Via": "1.1 nginx"}, "proxied", True),
        ("203.0.113.7", "http://gateway.example.com", {}, "non_loopback_peer", False),
        ("127.0.0.1", "http://gateway.example.com", {}, "non_loopback_host", False),
    ],
)
async def test_refusal_is_logged_and_audited_only_when_suspicious(
    audit_calls, caplog, peer, base_url, extra, reason, audited
):
    with caplog.at_level("INFO", logger=_LOGGER):
        resp = await _get_login(peer, base_url, {**_NAV, **extra})

    assert _key_form(resp)
    assert f"auth.auto_login refused reason={reason} peer={peer}" in caplog.text
    expected = (
        [
            (
                "auth.auto_login_refused",
                {"details": f"reason={reason} host=localhost", "ip": peer},
            )
        ]
        if audited
        else []
    )
    assert audit_calls == expected


async def test_explicit_logout_is_not_undone_by_auto_login(audit_calls):
    resp = await _get_login(
        "127.0.0.1", "http://localhost:8765", _NAV, path="/login?logged_out=1"
    )

    assert _key_form(resp)
    assert audit_calls == []


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE", " Off "])
async def test_switch_disables_auto_login(audit_calls, monkeypatch, value):
    monkeypatch.setenv("PLUGBOARD_LOCAL_AUTO_LOGIN", value)

    assert _key_form(await _get_login("127.0.0.1", "http://localhost:8765", _NAV))
    assert audit_calls == []


@pytest.mark.parametrize(
    ("value", "mode"),
    [
        ("", "auto"),
        ("auto", "auto"),
        (" AUTO ", "auto"),
        ("1", "on"),
        ("true", "on"),
        ("yes", "on"),
        (" On ", "on"),
        ("no", "off"),
    ],
)
def test_switch_value_table(monkeypatch, value, mode):
    monkeypatch.setenv("PLUGBOARD_LOCAL_AUTO_LOGIN", value)

    assert app_module._auto_login_mode() == mode


async def test_unknown_switch_value_fails_closed_and_warns_once(
    audit_calls, monkeypatch, caplog
):
    monkeypatch.setenv("PLUGBOARD_LOCAL_AUTO_LOGIN", "flase")

    with caplog.at_level("WARNING", logger=_LOGGER):
        for _ in range(3):
            resp = await _get_login("127.0.0.1", "http://localhost:8765", _NAV)

    assert _key_form(resp)
    assert caplog.text.count("'flase' is not recognised") == 1


async def test_enforced_mfa_disables_auto_login(audit_calls, monkeypatch):
    from plugboard import admin_mfa

    monkeypatch.setattr(admin_mfa, "is_required", lambda: True)

    assert _key_form(await _get_login("127.0.0.1", "http://localhost:8765", _NAV))


async def test_no_admin_key_means_no_session(audit_calls, monkeypatch, caplog):
    monkeypatch.setattr(app_module, "_ADMIN_KEY", "")

    with caplog.at_level("INFO", logger=_LOGGER):
        resp = await _get_login("127.0.0.1", "http://localhost:8765", _NAV)

    assert _key_form(resp)
    assert "reason=no_admin_key" in caplog.text


async def test_refusal_audits_stay_bounded_under_a_peer_flood(audit_calls, monkeypatch):
    # Many distinct peers (e.g. rotating IPv6 addresses) must not reset the
    # dedupe table nor write one audit row per request.
    monkeypatch.setattr(app_module, "_REFUSAL_AUDIT_MAX_KEYS", 3)
    for _ in range(2):
        for i in range(10):
            await _get_login(f"203.0.113.{i}", "http://localhost:8765", _NAV)

    assert len(app_module._refusal_audit_seen) <= 3
    assert len(audit_calls) == 3


async def test_expired_refusal_entries_free_room_for_new_peers(
    audit_calls, monkeypatch
):
    # Once the window has passed, old peers are evicted and new ones are
    # audited again instead of the full table blocking audits for good.
    monkeypatch.setattr(app_module, "_REFUSAL_AUDIT_MAX_KEYS", 3)
    monkeypatch.setattr(app_module, "_REFUSAL_AUDIT_WINDOW_S", 0.0)
    for i in range(10):
        await _get_login(f"203.0.113.{i}", "http://localhost:8765", _NAV)

    assert len(app_module._refusal_audit_seen) <= 3
    assert len(audit_calls) == 10


@pytest.mark.parametrize(
    ("host_header", "expected"),
    [
        ("localhost:8765", "localhost"),
        ("LocalHost", "localhost"),
        ("[::1]:8765", "::1"),
        ("127.0.0.1", "127.0.0.1"),
        ("x_y.attacker.example:8765", "x_y.attacker.example"),
        ("localhost:abc", "localhost:abc"),
        ("[::1", ""),
        ("", ""),
    ],
)
def test_host_header_name(host_header, expected):
    scope = {
        "type": "http",
        "headers": [(b"host", host_header.encode())] if host_header else [],
    }

    assert app_module._host_header_name(Request(scope)) == expected


def test_env_example_is_a_local_install_with_no_comment_values():
    # python-dotenv reads "KEY=   # note" as the value "# note": an unfilled
    # ADMIN_KEY would become public text, and the switch an unknown value.
    from pathlib import Path

    from dotenv import dotenv_values

    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")

    assert [k for k, v in values.items() if (v or "").lstrip().startswith("#")] == []
    assert values["PLUGBOARD_LOCAL_AUTO_LOGIN"] == "auto"
    assert app_module._is_loopback_address(values["HOST"])


@pytest.mark.parametrize(
    ("host", "expected"), [(None, "0.0.0.0"), ("127.0.0.1", "127.0.0.1")]
)
def test_run_py_exports_the_real_bind(monkeypatch, host, expected):
    import runpy
    from pathlib import Path

    import dotenv
    import uvicorn

    captured: dict = {}
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: captured.update(kw))
    # setenv first so monkeypatch records the variable and removes the value
    # run.py writes into os.environ.
    monkeypatch.setenv("PLUGBOARD_BIND_HOST", "unset")
    monkeypatch.delenv("PLUGBOARD_BIND_HOST")
    if host is None:
        monkeypatch.delenv("HOST", raising=False)
    else:
        monkeypatch.setenv("HOST", host)

    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "run.py"), run_name="__main__"
    )

    assert captured["host"] == expected
    assert app_module._bind_host() == expected
    # uvicorn's default proxy handling is kept (per-IP limits behind a proxy).
    assert "proxy_headers" not in captured


@pytest.mark.parametrize(
    "value",
    [
        "#",
        "# [REQUIRED] Cle admin",
        # Extended by the /ship coverage audit.
        # Value: protects=a commented secret is refused even with the leading
        # spaces an env file read literally keeps (docker run --env-file);
        # fails_when=the .lstrip() before the '#' check is dropped;
        # why_new=the only row had no leading whitespace; seam=none
        "                               # [REQUIRED] Cle admin",
    ],
)
@pytest.mark.parametrize("name", ["API_KEY", "ADMIN_KEY"])
def test_comment_valued_secret_refuses_to_start(name, value):
    with pytest.raises(RuntimeError, match=name) as exc:
        app_module._reject_comment_valued_secrets({name: value})
    # Emptying an auth key would open the instance: ask for a random one.
    assert "openssl rand" in str(exc.value)
    assert "empty" not in str(exc.value)


def test_comment_valued_encryption_key_only_warns(caplog):
    # Refusing it would force a new key and make stored tokens unreadable.
    with caplog.at_level("CRITICAL", logger=_LOGGER):
        app_module._reject_comment_valued_secrets({"ENCRYPTION_KEY": "# genere via"})

    assert "ENCRYPTION_KEY holds comment text" in caplog.text


@pytest.mark.parametrize("admin_key", ["k-123#456", "#Pa55-real-key"])
def test_real_or_empty_secrets_are_accepted(admin_key):
    # A real key may contain or start with "#"; only comment text is refused.
    app_module._reject_comment_valued_secrets(
        {"API_KEY": "", "ADMIN_KEY": admin_key, "ENCRYPTION_KEY": None}
    )


def test_server_refuses_to_start_with_a_comment_valued_secret(tmp_path):
    # Added by the /ship coverage audit.
    # Value: protects=the app refuses to start when ADMIN_KEY holds
    # .env.example comment text; fails_when=the import-time
    # _reject_comment_valued_secrets(os.environ) call is removed;
    # why_new=the other tests call the helper directly, not the startup;
    # seam=none
    import os
    import subprocess
    import sys
    from pathlib import Path

    def start(admin_key: str) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PLUGBOARD_SKIP_DOTENV": "1",
            "PLUGBOARD_DATA": str(tmp_path),
            "ADMIN_KEY": admin_key,
        }
        return subprocess.run(
            [sys.executable, "-c", "import plugboard.app"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    started = start("adminkey-real-value")
    refused = start("# [REQUIRED] Cle admin (dashboard, CRUD, gestion keys)")

    assert started.returncode == 0, started.stderr[-2000:]
    assert refused.returncode != 0
    assert "ADMIN_KEY holds comment text" in refused.stderr


@pytest.mark.parametrize(
    "dev_env, starts",
    [
        ({}, False),
        ({"PLUGBOARD_DEV": "1"}, True),
        # Pre-rename name: deliberately not carried over (it only quietened a
        # warning before 4.0.0), so it never lifts the ADMIN_KEY requirement.
        ({"LLM_ENABLER_DEV": "1"}, False),
    ],
    ids=["no-dev-mode-refused", "dev-mode-starts", "legacy-dev-name-refused"],
)
def test_server_without_admin_key_starts_only_in_dev_mode(tmp_path, dev_env, starts):
    # Added by the /ship coverage audit.
    # Value: protects=without ADMIN_KEY the server refuses to start unless dev
    # mode is on (never under its pre-rename name); fails_when=the import-time
    # _require_admin_key_outside_dev call or the package-level envcompat.apply()
    # is removed; why_new=test_storage calls the helper directly, not the
    # startup; seam=none
    import os
    import subprocess
    import sys
    from pathlib import Path

    dropped = {"ADMIN_KEY", "PLUGBOARD_DEV", "LLM_ENABLER_DEV"}
    env = {k: v for k, v in os.environ.items() if k not in dropped}
    env.update(
        {"PLUGBOARD_SKIP_DOTENV": "1", "PLUGBOARD_DATA": str(tmp_path), **dev_env}
    )
    proc = subprocess.run(
        [sys.executable, "-c", "import plugboard.app"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if starts:
        assert proc.returncode == 0, proc.stderr[-2000:]
    else:
        assert proc.returncode != 0
        assert "ADMIN_KEY is not set" in proc.stderr
