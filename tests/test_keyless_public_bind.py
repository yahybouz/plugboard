"""Keyless mode (API_KEY and ADMIN_KEY empty) never listens on a network.

With both keys empty every route is open, the admin API and the dashboard
included, while ``run.py`` listens on ``0.0.0.0`` when ``HOST`` is unset.
``run.py`` (also behind ``make dev``, the Docker image and Railway) must then
refuse to start unless the interface is loopback.
"""

from __future__ import annotations

import runpy
from pathlib import Path

import dotenv
import pytest
import uvicorn

_RUN_PY = str(Path(__file__).resolve().parents[1] / "run.py")


@pytest.fixture
def calls():
    """Keyword arguments of each uvicorn.run call made by run.py."""
    return []


@pytest.fixture
def start(monkeypatch, calls):
    """Run run.py as ``python run.py``; return the uvicorn.run kwargs."""
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    # setenv first so monkeypatch removes what run.py writes into os.environ.
    monkeypatch.setenv("PLUGBOARD_BIND_HOST", "unset")
    monkeypatch.delenv("PLUGBOARD_BIND_HOST")

    def _start(host, api_key="", admin_key=""):
        if host is None:
            monkeypatch.delenv("HOST", raising=False)
        else:
            monkeypatch.setenv("HOST", host)
        for name, value in (("API_KEY", api_key), ("ADMIN_KEY", admin_key)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        runpy.run_path(_RUN_PY, run_name="__main__")
        return calls[-1]

    return _start


def _assert_refused(exc_info, calls):
    assert calls == []  # uvicorn never started
    message = str(exc_info.value.code)
    assert exc_info.value.code not in (0, None)
    # The message says what is wrong and both ways out.
    assert "API_KEY" in message and "ADMIN_KEY" in message
    assert "HOST=127.0.0.1" in message
    assert "make init" in message


@pytest.mark.parametrize("host", [None, "0.0.0.0", "::", "192.168.1.10", ""])
@pytest.mark.parametrize("keys", [("", ""), (None, None)])
def test_keyless_start_on_a_network_interface_is_refused(start, calls, host, keys):
    with pytest.raises(SystemExit) as exc:
        start(host, *keys)
    _assert_refused(exc, calls)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_keyless_start_on_loopback_is_allowed(start, host):
    assert start(host)["host"] == host


@pytest.mark.parametrize(
    ("api_key", "admin_key"),
    [
        ("synthetic-api-key", ""),
        ("", "synthetic-admin-key-0123456789"),
        ("synthetic-api-key", "synthetic-admin-key-0123456789"),
    ],
)
def test_one_key_is_enough_to_listen_on_a_network(start, api_key, admin_key):
    # The Docker image listens on 0.0.0.0 with its keys.
    assert start("0.0.0.0", api_key, admin_key)["host"] == "0.0.0.0"


def test_make_dev_is_refused_too(start, calls, monkeypatch):
    # `make dev` = RELOAD=true PLUGBOARD_OPEN_BROWSER=1 python run.py
    monkeypatch.setenv("RELOAD", "true")
    monkeypatch.setenv("PLUGBOARD_OPEN_BROWSER", "1")
    with pytest.raises(SystemExit) as exc:
        start("0.0.0.0")
    _assert_refused(exc, calls)


def test_names_the_server_does_not_read_do_not_count(start, calls, monkeypatch):
    # The gateway reads API_KEY and ADMIN_KEY only: PLUGBOARD_* and the old
    # LLM_ENABLER_* names of the keys (MCP client settings) leave it open,
    # and no bind variable can make 0.0.0.0 look local.
    for name in (
        "PLUGBOARD_API_KEY",
        "PLUGBOARD_ADMIN_KEY",
        "LLM_ENABLER_API_KEY",
        "LLM_ENABLER_ADMIN_KEY",
    ):
        monkeypatch.setenv(name, "synthetic-key-0123456789abcdef")
    monkeypatch.setenv("LLM_ENABLER_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("PLUGBOARD_DEV", "1")
    with pytest.raises(SystemExit) as exc:
        start("0.0.0.0")
    _assert_refused(exc, calls)


# ── The app itself: `uvicorn plugboard.app:app --host 0.0.0.0` ──────────────
#
# run.py is not the only documented start command, so the app also answers
# only connections that arrive on a loopback interface in keyless mode.
# scope["server"] is that local address (uvicorn: the socket's sockname);
# proxy headers, the Host header and the peer address never change it.

_REMOTE_PEER = ("203.0.113.7", 50000)


@pytest.fixture
def app_module(_providers_file, monkeypatch):
    import plugboard.app as module
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 0)
    monkeypatch.setattr(module, "_API_KEY", "")
    monkeypatch.setattr(module, "_ADMIN_KEY", "")
    return module


async def _get(app, path, server, client=_REMOTE_PEER, headers=None):
    from httpx import ASGITransport, AsyncClient

    # ASGITransport reports the URL host and port as scope["server"].
    netloc = f"[{server}]" if ":" in server else server
    async with AsyncClient(
        transport=ASGITransport(app=app, client=client),
        base_url=f"http://{netloc}:8765",
    ) as c:
        return await c.get(path, headers=headers or {})


async def _raw_call(app, server, scope_type="http"):
    scope = {
        "type": scope_type,
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/providers",
        "raw_path": b"/providers",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost")],
        "server": server,
        "client": _REMOTE_PEER,
    }
    sent = []

    async def receive():
        if scope_type == "websocket":
            return {"type": "websocket.connect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _assert_app_refused(resp):
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert "API_KEY" in detail and "ADMIN_KEY" in detail
    assert "make init" in detail and "127.0.0.1" in detail


@pytest.mark.parametrize("server", ["192.0.2.10", "10.0.0.5", "2001:db8::1"])
@pytest.mark.parametrize("path", ["/providers", "/v1/models", "/", "/health"])
async def test_keyless_app_refuses_a_connection_on_a_network_interface(
    app_module, server, path
):
    _assert_app_refused(await _get(app_module.app, path, server))


async def test_keyless_app_ignores_headers_and_peer_address(app_module):
    resp = await _get(
        app_module.app,
        "/providers",
        "192.0.2.10",
        client=("127.0.0.1", 50000),
        headers={
            "Host": "localhost:8765",
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
        },
    )
    _assert_app_refused(resp)


@pytest.mark.parametrize(
    "server",
    [None, ("/run/plugboard.sock", None), ("localhost", None), ("testserver", 80)],
)
async def test_keyless_app_fails_closed_on_an_unknown_interface(app_module, server):
    # A Unix socket (even one named "localhost") or a name is not loopback.
    assert (await _raw_call(app_module.app, server))[0]["status"] == 403


async def test_keyless_app_closes_a_websocket_on_a_network_interface(app_module):
    sent = await _raw_call(app_module.app, ("192.0.2.10", 8765), "websocket")
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.parametrize("server", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
async def test_keyless_app_serves_loopback(app_module, server):
    resp = await _get(app_module.app, "/providers", server, client=(server, 50000))
    assert resp.status_code == 200


@pytest.mark.parametrize(
    ("api_key", "admin_key"),
    [("synthetic-api-key", ""), ("", "synthetic-admin-key-0123456789")],
)
async def test_app_with_one_key_serves_a_network_interface(
    app_module, monkeypatch, api_key, admin_key
):
    monkeypatch.setattr(app_module, "_API_KEY", api_key)
    monkeypatch.setattr(app_module, "_ADMIN_KEY", admin_key)
    resp = await _get(app_module.app, "/health", "192.0.2.10")
    assert resp.status_code == 200
