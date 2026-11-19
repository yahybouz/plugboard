"""PLU-158 acceptance criteria, one named test (or more) per criterion.

Completes tests/test_web_access.py, whose fixtures it reuses. No CLI runs,
no inference, nothing leaves the machine: ``cli_subprocess.complete`` is a
recorder, or ``create_subprocess_exec`` returns a fake process; the egress
guard is tried against local sockets only and every name resolution is a
fake (``loop.getaddrinfo`` replaced). Settings read at import time
(PLUGBOARD_WEB_HELPER_MODEL, PLUGBOARD_WEB_TIMEOUT_S) are read by a private
copy of plugboard/web_access.py, never by the module the app uses.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import socket
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from plugboard import apikeys, cli_subprocess, usage, web_access, web_egress
from plugboard.cli_subprocess import CliSubprocessError
from tests import test_web_access as _base
from tests.test_web_access import MODEL, MSG, _auth, _chat

# The fixtures of tests/test_web_access.py (a CLI recorder, two keys).
calls = _base.calls
keys = _base.keys

# ── helpers ──────────────────────────────────────────────────────────────────

SECRET_PATH = "/private/page-7f3a"
SECRET_QUERY = "token=q9z8y7"
SECRET_URL = f"https://docs.example.org{SECRET_PATH}?{SECRET_QUERY}"
PROMPT_MARK = "question-marker-5c1e"

TOOL = {
    "type": "function",
    "function": {
        "name": "lookup",
        "description": "Look something up.",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
}


def _fresh_web_access(monkeypatch, **env: str):
    """A private copy of plugboard/web_access.py read with ``env``: its
    import-time settings, the app's module untouched."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location(
        "plugboard_web_access_probe", web_access.__file__
    )
    mod = importlib.util.module_from_spec(spec)
    # dataclasses look their module up in sys.modules while it runs.
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


def _new_key(name: str, **rights) -> dict:
    rec = apikeys.create_key(name, "criteria", **rights)
    apikeys._invalidate_cache()
    return rec


def _web_counts() -> tuple:
    s = web_access.stats()
    return s["active"], s["requests"]


async def _send(port: int, data: bytes) -> bytes:
    """Write ``data`` to the egress guard and read what it answers."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    head = await asyncio.wait_for(reader.read(200), 5)
    writer.close()
    return head


async def _connect(port: int, target: str) -> bytes:
    return await _send(port, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())


@pytest.fixture
async def fake_dns(monkeypatch):
    """Name resolution of the running loop replaced: ``answers`` maps a name
    to the addresses it resolves to; a name not there fails (OSError)."""
    answers: dict = {}
    asked: list = []
    loop = asyncio.get_running_loop()

    async def getaddrinfo(host, port, *args, **kwargs):
        asked.append(host)
        if host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return [
            (
                socket.AF_INET6 if ":" in a else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (a, port),
            )
            for a in answers[host]
        ]

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    return {"answers": answers, "asked": asked}


@pytest.fixture
async def guard():
    port = await web_egress.ensure_started()
    try:
        yield port
    finally:
        await web_egress.stop()


# ── 1. no web asked: the request is unchanged ────────────────────────────────


async def test_c1_without_web_no_policy_no_slot_and_complete_called_as_before(
    client, keys, calls, monkeypatch
):
    seen_policy: list = []
    recorder = cli_subprocess.complete

    async def spy(kind, body, **kw):
        seen_policy.append(web_access.current())
        return await recorder(kind, body, **kw)

    monkeypatch.setattr(cli_subprocess, "complete", spy)
    before = _web_counts()
    resp = await _chat(client, keys["web"])  # a key holding the right
    assert resp.status_code == 200, resp.text
    (call,) = calls
    assert "web" not in call
    assert seen_policy == [None]
    assert _web_counts() == before  # no web slot taken, none counted


async def test_c1_without_web_the_cache_is_used(
    client, keys, calls, tmp_path, monkeypatch
):
    import plugboard.cache as response_cache

    monkeypatch.setattr(response_cache, "_DB_PATH", tmp_path / "cache.db")
    body = {"model": MODEL, "messages": MSG}  # cache left on, as a client does
    first = await client.post(
        "/v1/chat/completions", headers=_auth(keys["web"]), json=body
    )
    assert first.status_code == 200, first.text
    again = await client.post(
        "/v1/chat/completions", headers=_auth(keys["web"]), json=body
    )
    assert again.status_code == 200, again.text
    assert again.headers.get("x-cache") == "HIT"
    assert len(calls) == 1


async def test_c1_web_answer_is_never_cached(
    client, keys, calls, tmp_path, monkeypatch
):
    import plugboard.cache as response_cache

    monkeypatch.setattr(response_cache, "_DB_PATH", tmp_path / "cache.db")
    body = {"model": MODEL, "messages": MSG}
    for _ in range(2):
        resp = await client.post(
            "/v1/chat/completions", headers=_auth(keys["web"], web=True), json=body
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("x-cache") != "HIT"
    assert len(calls) == 2


async def test_c1_policy_of_a_web_request_does_not_leak_into_the_next(
    client, keys, calls
):
    assert (await _chat(client, keys["web"], web=True)).status_code == 200
    assert (await _chat(client, keys["web"])).status_code == 200
    assert calls[0]["web"] is not None
    assert "web" not in calls[1]


# ── 2. key without the right ─────────────────────────────────────────────────


async def test_c2_key_without_right_403_nothing_called_no_slot(client, keys, calls):
    before = _web_counts()
    refused = web_access.stats()["refused_not_allowed"]
    resp = await _chat(client, keys["plain"], web=True)
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "web_not_allowed"
    assert calls == []
    assert _web_counts() == before
    assert web_access.stats()["refused_not_allowed"] == refused + 1


async def test_c2_admin_key_never_holds_the_web_right(client, keys, calls):
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey", "X-Plugboard-Web": "1"},
        json={"model": MODEL, "messages": MSG, "cache": False},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "web_not_allowed"
    assert calls == []


async def test_c2_body_extension_without_right_is_403_too(client, keys, calls):
    resp = await _chat(client, keys["plain"], plugboard={"web": True})
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "web_not_allowed"
    assert calls == []


# ── 3. concurrent web cap ────────────────────────────────────────────────────


async def test_c3_web_off_with_cap_zero_gets_429_retry_after(
    client, keys, calls, monkeypatch
):
    monkeypatch.setattr(web_access, "MAX_INFLIGHT", 0)
    resp = await _chat(client, keys["web"], web=True)
    assert resp.status_code == 429, resp.text
    assert resp.json()["code"] == "web_busy"
    assert resp.json()["retry_after"] == web_access.RETRY_AFTER_S
    assert resp.headers["Retry-After"] == str(web_access.RETRY_AFTER_S)
    assert calls == []
    assert web_access.stats()["active"] == 0


# ── 4. egress guard ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target",
    [
        "127.0.0.1:443",
        "10.0.0.1:443",
        "10.255.255.254:443",
        "172.16.0.1:443",
        "172.31.255.254:443",
        "192.168.0.1:443",
        "192.168.255.254:443",
        "100.64.0.1:443",
        "100.127.255.254:443",
        "169.254.169.254:443",
        "169.254.169.254:80",
        "[::ffff:10.0.0.1]:443",
    ],
)
async def test_c4_egress_refuses_non_public_ip(guard, fake_dns, target):
    tunnels = web_egress.stats()["egress_tunnels"]
    head = await _connect(guard, target)
    assert head.startswith(b"HTTP/1.1 403"), (target, head)
    assert web_egress.stats()["egress_tunnels"] == tunnels
    assert fake_dns["asked"] == []  # an address literal is never resolved


async def test_c4_egress_refuses_a_name_resolving_to_a_private_address(guard, fake_dns):
    fake_dns["answers"]["intranet.example.test"] = ["192.168.10.5"]
    refused = web_egress.stats()["egress_refused"]
    head = await _connect(guard, "intranet.example.test:443")
    assert head.startswith(b"HTTP/1.1 403"), head
    assert fake_dns["asked"] == ["intranet.example.test"]
    assert web_egress.stats()["egress_refused"] == refused + 1


async def test_c4_egress_refuses_a_name_mixing_public_and_private(guard, fake_dns):
    fake_dns["answers"]["mixed.example.test"] = ["93.184.216.34", "10.1.2.3"]
    tunnels = web_egress.stats()["egress_tunnels"]
    head = await _connect(guard, "mixed.example.test:443")
    assert head.startswith(b"HTTP/1.1 403"), head
    assert web_egress.stats()["egress_tunnels"] == tunnels


async def test_c4_egress_refuses_a_name_that_does_not_resolve(guard, fake_dns):
    head = await _connect(guard, "nowhere.example.test:443")
    assert head.startswith(b"HTTP/1.1 403"), head
    assert fake_dns["asked"] == ["nowhere.example.test"]


async def test_c4_egress_public_check_uses_the_resolved_address(fake_dns):
    """The tunnel is opened to the address checked here (no second lookup)."""
    fake_dns["answers"]["public.example.test"] = ["93.184.216.34"]
    addrs = await web_egress._public_addresses("public.example.test", 443)
    assert [a for _f, a in addrs] == ["93.184.216.34"]
    fake_dns["answers"]["metadata.example.test"] = ["169.254.169.254"]
    assert await web_egress._public_addresses("metadata.example.test", 443) == []


@pytest.mark.parametrize(
    "line",
    [
        b"GET http://example.com/ HTTP/1.1",
        b"POST http://example.com/form HTTP/1.1",
        b"GET / HTTP/1.1",
        b"CONNECT example.com HTTP/1.1",  # no port
        b"CONNECT example.com:0 HTTP/1.1",
    ],
)
async def test_c4_egress_refuses_plain_http_and_malformed_requests(
    guard, fake_dns, line
):
    head = await _send(guard, line + b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 405"), (line, head)
    assert fake_dns["asked"] == []


# ── 5. the helper model is never Haiku ───────────────────────────────────────


@pytest.mark.parametrize(
    "value", ["claude-haiku-5-5", "Claude-3-5-HAIKU-latest", "haiku"]
)
def test_c5_haiku_helper_setting_is_replaced_by_sonnet(monkeypatch, caplog, value):
    with caplog.at_level(logging.WARNING):
        mod = _fresh_web_access(monkeypatch, PLUGBOARD_WEB_HELPER_MODEL=value)
    assert mod.HELPER_MODEL == "claude-sonnet-5-5"
    env = mod.child_env(4242)
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-sonnet-5-5"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-sonnet-5-5"
    assert any("Haiku" in r.getMessage() for r in caplog.records)


def test_c5_other_helper_setting_is_kept_and_blank_gives_sonnet(monkeypatch):
    mod = _fresh_web_access(monkeypatch, PLUGBOARD_WEB_HELPER_MODEL="claude-opus-5-5")
    assert mod.child_env(1)["ANTHROPIC_SMALL_FAST_MODEL"] == "claude-opus-5-5"
    mod = _fresh_web_access(monkeypatch, PLUGBOARD_WEB_HELPER_MODEL="   ")
    assert mod.HELPER_MODEL == "claude-sonnet-5-5"


# ── 6. bounds: web_max_uses, timeout, sizes ──────────────────────────────────


@pytest.mark.parametrize("value", [0, 11, -1, True, "3", 2.5])
async def test_c6_create_key_refuses_web_max_uses_out_of_1_to_10(client, keys, value):
    resp = await client.post(
        "/admin/keys",
        headers={"Authorization": "Bearer testkey"},
        json={"client_id": "crit-client", "web_access": True, "web_max_uses": value},
    )
    assert resp.status_code == 422, (value, resp.text)


@pytest.mark.parametrize("value", [1, 10])
async def test_c6_create_key_accepts_web_max_uses_1_and_10(client, keys, value):
    resp = await client.post(
        "/admin/keys",
        headers={"Authorization": "Bearer testkey"},
        json={"client_id": "crit-client", "web_access": True, "web_max_uses": value},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["web_max_uses"] == value


@pytest.mark.parametrize("value", [0, 11, True])
async def test_c6_put_web_refuses_web_max_uses_out_of_1_to_10(client, keys, value):
    kid = keys["web"]["key_id"]
    resp = await client.put(
        f"/admin/keys/{kid}/web",
        headers={"Authorization": "Bearer testkey"},
        json={"web_access": True, "web_max_uses": value},
    )
    assert resp.status_code == 422, (value, resp.text)
    rec = apikeys.get_key_record(keys["web"]["key"])
    assert rec["web_max_uses"] == 4  # unchanged


@pytest.mark.parametrize("value", [1, 10])
async def test_c6_put_web_accepts_web_max_uses_1_and_10(client, keys, value):
    kid = keys["web"]["key_id"]
    resp = await client.put(
        f"/admin/keys/{kid}/web",
        headers={"Authorization": "Bearer testkey"},
        json={"web_access": True, "web_max_uses": value},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["web_max_uses"] == value


def test_c6_max_turns_follow_web_max_uses():
    assert web_access.WebPolicy(max_uses=1).max_turns == 3
    assert web_access.WebPolicy(max_uses=10).max_turns == 12


@pytest.mark.parametrize(
    ("raw", "expected"), [("", 900.0), ("60", 60.0), ("10", 900.0), ("nan", 900.0)]
)
def test_c6_web_timeout_setting_has_a_30_s_floor(monkeypatch, raw, expected):
    mod = _fresh_web_access(monkeypatch, PLUGBOARD_WEB_TIMEOUT_S=raw)
    assert mod.TIMEOUT_S == expected


@pytest.fixture
def fake_claude(monkeypatch):
    """The real claude wrapper on a fake process (no CLI is started)."""
    procs: list = []

    def _setup(stdout: bytes = b"", communicate=None) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 4242
        proc.communicate = communicate or AsyncMock(return_value=(stdout, b""))
        monkeypatch.setattr(
            cli_subprocess, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}"
        )
        monkeypatch.setattr(
            cli_subprocess.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=proc),
        )
        procs.append(proc)
        return proc

    return _setup


async def test_c6_web_run_has_its_own_deadline(fake_claude, monkeypatch):
    async def hangs(_data=None):
        await asyncio.sleep(30)

    fake_claude(communicate=hangs)
    monkeypatch.setattr(cli_subprocess, "_kill_group", AsyncMock())
    monkeypatch.setattr(web_access, "TIMEOUT_S", 0.05)
    try:
        with pytest.raises(CliSubprocessError) as err:
            await asyncio.wait_for(
                cli_subprocess.complete(
                    "claude",
                    {"model": "m-1", "messages": MSG},
                    timeout_s=600,  # the route's deadline: the web one wins
                    web=web_access.WebPolicy(max_uses=3),
                ),
                5,
            )
    finally:
        await web_egress.stop()
    assert err.value.timed_out is True


async def test_c6_egress_answers_431_to_an_oversized_head(guard, fake_dns):
    padding = "X-Pad: " + "a" * (web_egress._MAX_HEAD + 100)
    data = f"CONNECT 10.0.0.1:443 HTTP/1.1\r\n{padding}\r\n\r\n".encode()
    assert web_egress._MAX_HEAD < len(data) < 2 * web_egress._MAX_HEAD
    head = await _send(guard, data)
    assert head.startswith(b"HTTP/1.1 431"), head
    assert fake_dns["asked"] == []


async def test_c6_egress_drops_a_head_past_its_read_limit(guard, fake_dns):
    padding = "X-Pad: " + "a" * (3 * web_egress._MAX_HEAD)
    data = f"CONNECT 10.0.0.1:443 HTTP/1.1\r\n{padding}\r\n\r\n".encode()
    head = await _send(guard, data)
    assert head == b""  # closed, nothing tunnelled
    assert fake_dns["asked"] == []


def test_c6_names_kept_from_a_run_are_bounded():
    long_tool = "WebSearch" + "x" * 200
    long_model = "claude-sonnet-5-5" + "y" * 200
    out = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": long_tool}]},
                }
            ),
            json.dumps({"type": "result", "modelUsage": {long_model: {}}}),
        ]
    )
    policy = web_access.WebPolicy(max_uses=3)
    web_access.count_tool_uses(out, policy)
    assert [len(n) for n in policy.tools_seen] == [40]
    assert [len(n) for n in web_access.models_used(out, policy)] == [60]


# ── 7. logs: host names only, never a path, a query or the content ───────────


def _all_logged(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


async def test_c7_web_request_logs_no_path_query_or_content(
    client, keys, calls, caplog
):
    caplog.set_level(logging.DEBUG)
    resp = await client.post(
        "/v1/chat/completions",
        headers=_auth(keys["web"], web=True),
        json={
            "model": MODEL,
            "messages": [
                {"role": "user", "content": f"{PROMPT_MARK} read {SECRET_URL}"}
            ],
            "cache": False,
        },
    )
    assert resp.status_code == 200, resp.text
    logged = _all_logged(caplog)
    assert "web call done" in logged
    assert keys["web"]["key_id"] in logged
    for secret in (SECRET_PATH, SECRET_QUERY, PROMPT_MARK, "example.com"):
        assert secret not in logged, secret


async def test_c7_web_run_log_names_tools_not_urls(fake_claude, caplog):
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "WebFetch",
                        "input": {"url": SECRET_URL},
                    },
                    {
                        "type": "tool_use",
                        "name": "WebSearch",
                        "input": {"query": PROMPT_MARK},
                    },
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": f"Answer, see {SECRET_URL}"}]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": f"Answer, see {SECRET_URL}",
            "modelUsage": {"claude-sonnet-5-5": {}},
            "usage": {"input_tokens": 3, "output_tokens": 4},
        },
    ]
    fake_claude(stdout="".join(json.dumps(e) + "\n" for e in events).encode())
    caplog.set_level(logging.DEBUG)
    policy = web_access.WebPolicy(max_uses=3)
    try:
        await cli_subprocess.complete(
            "claude",
            {"model": "m-1", "messages": [{"role": "user", "content": PROMPT_MARK}]},
            web=policy,
        )
    finally:
        await web_egress.stop()
    assert policy.uses == 2
    runs = [r.getMessage() for r in caplog.records if "web run:" in r.getMessage()]
    assert runs and "tools=WebFetch,WebSearch" in runs[0]
    logged = _all_logged(caplog)
    for secret in (SECRET_PATH, SECRET_QUERY, "docs.example.org", PROMPT_MARK):
        assert secret not in logged, secret


async def test_c7_egress_logs_the_refused_host_only(guard, fake_dns, caplog):
    caplog.set_level(logging.DEBUG, logger="plugboard.web_egress")
    fake_dns["answers"]["intranet.example.test"] = ["10.9.8.7"]
    long_host = "a" * 300 + ".example.test"
    fake_dns["answers"][long_host] = ["10.9.8.7"]
    await _connect(guard, "intranet.example.test:443")
    await _connect(guard, f"{long_host}:443")
    await _send(
        guard,
        f"GET http://plain.example.test{SECRET_PATH}?{SECRET_QUERY} HTTP/1.1\r\n\r\n".encode(),
    )
    refused = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert "host=intranet.example.test port=443" in refused[0]
    assert f"host={'a' * 253} port=443" in refused[1]  # bounded to 253
    logged = _all_logged(caplog)
    for secret in (SECRET_PATH, SECRET_QUERY, "10.9.8.7", "plain.example.test"):
        assert secret not in logged, secret


async def test_c7_egress_logs_the_tunnel_host_not_what_goes_through(
    monkeypatch, caplog
):
    from plugboard import netguard

    async def echo(reader, writer):
        writer.write(await reader.read(200))
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    monkeypatch.setattr(netguard, "ip_is_public", lambda _ip: True)
    caplog.set_level(logging.DEBUG, logger="plugboard.web_egress")
    port = await web_egress.ensure_started()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        inner = f"GET {SECRET_PATH}?{SECRET_QUERY} HTTP/1.1\r\n\r\n".encode()
        writer.write(inner)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(200), 5) == inner
        writer.close()
    finally:
        await web_egress.stop()
        upstream.close()
        await upstream.wait_closed()
    logged = _all_logged(caplog)
    assert f"web egress tunnel: host=127.0.0.1 port={up_port}" in logged
    assert SECRET_PATH not in logged and SECRET_QUERY not in logged


# ── 8. cost counted per key ──────────────────────────────────────────────────


async def test_c8_month_summary_sums_web_per_key(client, keys, calls):
    other = _new_key("webby-2", web_access=True, web_max_uses=2)
    assert (await _chat(client, keys["web"], web=True)).status_code == 200
    for _ in range(2):
        assert (await _chat(client, other, web=True)).status_code == 200
    assert (await _chat(client, other)).status_code == 200
    month = usage._billing.current_pricing_month()
    a = usage.summarize_usage_by_key(month, keys["web"]["key_id"])
    b = usage.summarize_usage_by_key(month, other["key_id"])
    assert (a["web_requests"], a["web_uses"]) == (1, 2)
    assert (b["web_requests"], b["web_uses"]) == (2, 4)
    rows = usage.get_usage_by_key(month, other["key_id"])
    assert sorted((r["web"], r["web_uses"]) for r in rows) == [(0, 0), (1, 2), (1, 2)]
    assert all(r["key_id"] == other["key_id"] for r in rows)


async def test_c8_streamed_web_call_is_marked_in_usage(client, keys, calls):
    resp = await _chat(client, keys["web"], web=True, stream=True)
    assert resp.status_code == 200, resp.text
    month = usage._billing.current_pricing_month()
    rows = usage.get_usage_by_key(month, keys["web"]["key_id"])
    assert [(r["web"], r["web_uses"]) for r in rows] == [(1, 2)]


# ── 9. web and tools in one request (ADR-040, 4.15.0) ────────────────────────


@pytest.fixture
def tools_key(keys, monkeypatch):
    """A key holding both rights, the tools emulation option on."""
    monkeypatch.setenv("PLUGBOARD_CLI_TOOLS_EMULATION", "1")
    return _new_key(
        "webby-tools", web_access=True, web_max_uses=3, allow_cli_tools=True
    )


async def test_c9_web_with_tools_is_400_nothing_called_no_slot(
    client, tools_key, calls
):
    before = _web_counts()
    resp = await _chat(client, tools_key, web=True, tools=[TOOL])
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "web_with_tools_unsupported"
    assert calls == []
    assert _web_counts() == before


async def test_c9_web_with_tools_streamed_is_400_too(client, tools_key, calls):
    before = _web_counts()
    resp = await _chat(
        client, tools_key, plugboard={"web": True}, tools=[TOOL], stream=True
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "web_with_tools_unsupported"
    assert calls == []
    assert _web_counts() == before


async def test_c9_web_with_tools_is_400_before_the_busy_429(
    client, tools_key, calls, monkeypatch
):
    monkeypatch.setattr(web_access, "MAX_INFLIGHT", 0)
    resp = await _chat(client, tools_key, web=True, tools=[TOOL])
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "web_with_tools_unsupported"
    assert calls == []


async def test_c9_web_with_empty_tools_is_served(client, tools_key, calls):
    resp = await _chat(client, tools_key, web=True, tools=[])
    assert resp.status_code == 200, resp.text
    (call,) = calls
    assert call["web"] is not None
