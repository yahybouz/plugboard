"""Web access for the LLMs Plugboard serves, granted per key (PLU-158).

No CLI runs and nothing leaves the machine: ``cli_subprocess.complete`` is
replaced by a recorder, the launch test replaces ``create_subprocess_exec``
and the egress guard is tried against local sockets only.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from plugboard import apikeys, cli_subprocess, netguard, router, usage
from plugboard import web_access, web_egress
from plugboard.cli_subprocess import CliSubprocessError
from tests.factories import PROVIDER_B
from tests.test_cli_launch_unchanged import (  # noqa: F401 - pytest fixture
    CLAUDE_4141,
    _Recorded,
    server,
)
from tests.test_router_admission import CLI

MODEL = "pbtest-cli:model-x"
HTTP_MODEL = "other-provider:other:model-v2"
MSG = [{"role": "user", "content": "prix de l'essence au Maroc ?"}]


def _result(text: str = "Réponse, source https://example.com") -> dict:
    return {
        "id": "chatcmpl-web",
        "object": "chat.completion",
        "created": 1,
        "model": "claude-cli:model-x",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.setattr(apikeys, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(apikeys, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(apikeys, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    apikeys._invalidate_cache()
    plain = apikeys.create_key("plain", "no web")
    webby = apikeys.create_key("webby", "web on", web_access=True, web_max_uses=4)
    apikeys._invalidate_cache()
    yield {"plain": plain, "web": webby}
    apikeys._invalidate_cache()


@pytest.fixture
def calls(_providers_file, monkeypatch):
    """CLI route + an HTTP provider; the CLI is a recorder."""
    _providers_file.write_text(json.dumps({"providers": [CLI, PROVIDER_B]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    seen: list = []

    async def fake(kind, body, *, timeout_s=None, on_delta=None, **kw):
        seen.append({"kind": kind, "body": dict(body), **kw})
        policy = kw.get("web")
        if policy is not None:
            policy.uses = 2
        if on_delta is not None:
            on_delta("Réponse, ")
            await asyncio.sleep(0)
            on_delta("source https://example.com")
        return _result()

    monkeypatch.setattr(cli_subprocess, "complete", fake)
    monkeypatch.setattr(web_access, "_active", 0)
    return seen


def _auth(key: dict, web: bool = False) -> dict:
    h = {"Authorization": f"Bearer {key['key']}"}
    if web:
        h["X-Plugboard-Web"] = "1"
    return h


async def _chat(client, key, web=False, model=MODEL, **extra):
    return await client.post(
        "/v1/chat/completions",
        headers=_auth(key, web),
        json={"model": model, "messages": MSG, "cache": False, **extra},
    )


# ── HTTP behaviour ───────────────────────────────────────────────────────────


async def test_without_web_the_call_is_unchanged(client, keys, calls):
    resp = await _chat(client, keys["web"])
    assert resp.status_code == 200, resp.text
    (call,) = calls
    assert "web" not in call  # complete() called exactly as before


async def test_key_without_the_right_gets_403(client, keys, calls):
    resp = await _chat(client, keys["plain"], web=True)
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "web_not_allowed"
    assert calls == []


async def test_non_claude_provider_gets_400_web_unavailable(client, keys, calls):
    resp = await _chat(client, keys["web"], web=True, model=HTTP_MODEL)
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "web_unavailable"
    assert calls == []


async def test_web_cap_full_gets_429(client, keys, calls, monkeypatch):
    monkeypatch.setattr(web_access, "MAX_INFLIGHT", 1)
    held = web_access.try_acquire()
    try:
        resp = await _chat(client, keys["web"], web=True)
    finally:
        held.release()
    assert resp.status_code == 429, resp.text
    assert resp.json()["code"] == "web_busy"
    assert resp.headers["Retry-After"] == str(web_access.RETRY_AFTER_S)
    assert calls == []


async def test_web_call_gets_the_policy_and_the_slot_comes_back(client, keys, calls):
    resp = await _chat(client, keys["web"], plugboard={"web": True})
    assert resp.status_code == 200, resp.text
    (call,) = calls
    assert call["web"].max_uses == 4  # the key's web_max_uses
    assert "plugboard" not in call["body"]  # never reaches a provider
    assert web_access.stats()["active"] == 0


async def test_malformed_web_field_is_400(client, keys, calls):
    resp = await _chat(client, keys["web"], plugboard={"web": "yes"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "web_bad_request"
    resp = await client.post(
        "/v1/chat/completions",
        headers={**_auth(keys["web"]), "X-Plugboard-Web": "maybe"},
        json={"model": MODEL, "messages": MSG},
    )
    assert resp.status_code == 400
    assert calls == []


async def test_streamed_web_call_keeps_the_policy(client, keys, calls):
    resp = await _chat(client, keys["web"], web=True, stream=True)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    text = ""
    for raw in resp.text.split("\n\n"):
        raw = raw.strip()
        if raw.startswith("data:") and "[DONE]" not in raw:
            evt = json.loads(raw[5:])
            for ch in evt.get("choices") or []:
                text += (ch.get("delta") or {}).get("content") or ""
    assert "https://example.com" in text
    (call,) = calls
    assert call["web"] is not None  # the context reached the SSE generator
    assert web_access.stats()["active"] == 0


async def test_usage_marks_the_web_call(client, keys, calls):
    assert (await _chat(client, keys["web"], web=True)).status_code == 200
    assert (await _chat(client, keys["web"])).status_code == 200
    month = usage._billing.current_pricing_month()
    rows = [
        r for r in usage.get_usage(month) if r.get("key_id") == keys["web"]["key_id"]
    ]
    assert sorted((r["web"], r["web_uses"]) for r in rows) == [(0, 0), (1, 2)]
    summary = usage.summarize_usage_by_key(month, keys["web"]["key_id"])
    assert (summary["web_requests"], summary["web_uses"]) == (1, 2)
    assert usage.summarize_usage(rows)["web_uses"] == 2
    me = (await client.get("/v1/me", headers=_auth(keys["web"]))).json()
    assert me["web_access"] is True and me["web_max_uses"] == 4


async def test_admin_turns_web_on_and_off(client, keys):
    admin = {"Authorization": "Bearer testkey"}
    kid = keys["plain"]["key_id"]
    resp = await client.put(
        f"/admin/keys/{kid}/web", headers=admin, json={"web_access": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["web_access"] is True
    assert apikeys.allows_web(apikeys.get_key_record(keys["plain"]["key"]))
    bad = await client.put(
        f"/admin/keys/{kid}/web",
        headers=admin,
        json={"web_access": True, "web_max_uses": 11},
    )
    assert bad.status_code == 422
    missing = await client.put(
        "/admin/keys/nope/web", headers=admin, json={"web_access": True}
    )
    assert missing.status_code == 404
    stats = (await client.get("/admin/web", headers=admin)).json()
    assert {"active", "max_inflight", "egress_tunnels", "egress_refused"} <= set(stats)


def test_rotation_keeps_the_web_right(keys):
    new = apikeys.rotate_key(keys["web"]["key_id"])
    assert new["web_access"] is True and new["web_max_uses"] == 4
    assert apikeys._new_key("x", "y")[0]["web_access"] is False
    with pytest.raises(ValueError):
        apikeys.create_key("bad", "", web_max_uses=0)


def test_budget_exhausted_is_a_502_with_its_code():
    from plugboard.app import _cli_error_response

    exc = CliSubprocessError("web: used its 3 web tool calls", provider_name="claude")
    assert exc.web_exhausted is False
    exc.web_exhausted = True
    resp = _cli_error_response(exc, "cust")
    assert resp.status_code == 502
    assert json.loads(resp.body)["code"] == "web_budget_exhausted"


def test_metrics_list_the_web():
    from plugboard.metrics import format_prometheus

    text = format_prometheus()
    for name in (
        "plugboard_web_active",
        "plugboard_web_refused_not_allowed_total",
        "plugboard_web_refused_busy_total",
        "plugboard_web_egress_refused_total",
    ):
        assert name in text


# ── claude command line ──────────────────────────────────────────────────────


def test_web_args_name_only_the_two_web_tools():
    args = web_access.claude_args(list(CLAUDE_4141), web_access.WebPolicy(max_uses=3))

    def values(flag):
        return [args[i + 1] for i, a in enumerate(args[:-1]) if a == flag]

    assert values("--tools") == ["WebSearch,WebFetch"]
    assert values("--allowedTools") == ["WebSearch,WebFetch"]
    assert values("--permission-mode") == ["default"]
    assert values("--max-turns") == ["5"]
    assert "--strict-mcp-config" in args
    assert values("--setting-sources") == [""]
    for flag in ("--dangerously-skip-permissions", "--mcp-config", "--add-dir"):
        with pytest.raises(RuntimeError):
            web_access.check_web_args([*args, flag, "x"])
    with pytest.raises(RuntimeError):
        web_access.check_web_args(
            [a if a != "WebSearch,WebFetch" else "Bash,WebFetch" for a in args]
        )
    with pytest.raises(RuntimeError):  # base set not locked down
        web_access.claude_args(["-p", "--tools", "Bash"], web_access.WebPolicy(3))


def test_child_env_never_names_haiku():
    env = web_access.child_env(4242)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:4242"
    assert env["NO_PROXY"] == ""
    for name in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_SMALL_FAST_MODEL"):
        assert "haiku" not in env[name]
        assert env[name] == web_access.HELPER_MODEL


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_claude_web_launch(server, stream):  # noqa: F811
    policy = web_access.WebPolicy(max_uses=3)
    with pytest.raises(_Recorded):
        await cli_subprocess.complete(
            "claude",
            {"model": "m-1", "messages": MSG},
            on_delta=(lambda _t: None) if stream else None,
            web=policy,
        )
    (call,) = server["calls"]
    argv = call["argv"]
    web_access.check_web_args(argv)
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    env = call["env"]
    assert env["HTTPS_PROXY"].startswith("http://127.0.0.1:")
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == web_access.HELPER_MODEL
    assert not {"ADMIN_KEY", "API_KEY", "ENCRYPTION_KEY"} & set(env)
    await web_egress.stop()


async def test_web_is_refused_on_another_cli():
    with pytest.raises(RuntimeError):
        await cli_subprocess.complete(
            "codex", {"model": "m", "messages": MSG}, web=web_access.WebPolicy(3)
        )


def test_tool_uses_and_max_turns_are_read_from_stream_json():
    out = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "WebSearch"},
                            {"type": "tool_use", "name": "WebFetch"},
                        ]
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "error_max_turns"}),
        ]
    )
    policy = web_access.WebPolicy(max_uses=3)
    assert web_access.count_tool_uses(out, policy) == 2
    assert policy.tools_seen == ["WebSearch", "WebFetch"]
    assert web_access.max_turns_reached(out)
    result = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "modelUsage": {"claude-sonnet-5-5": {}, "claude-opus-5-5": {}},
        }
    )
    assert web_access.models_used(result, policy) == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
    ]
    before = web_access.stats()["helper_haiku"]
    haiku = result.replace("claude-sonnet-5-5", "claude-haiku-5-5")
    assert "claude-haiku-5-5" in web_access.models_used(haiku, policy)
    assert web_access.stats()["helper_haiku"] == before + 1
    assert not web_access.max_turns_reached('{"type":"result","subtype":"success"}')


# ── egress guard ─────────────────────────────────────────────────────────────


async def _connect(port: int, line: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(line + b"\r\n\r\n")
    await writer.drain()
    head = await asyncio.wait_for(reader.read(200), 5)
    writer.close()
    return head


async def test_egress_refuses_internal_destinations():
    port = await web_egress.ensure_started()
    try:
        for target in (
            b"127.0.0.1:443",
            b"localhost:443",
            b"10.0.0.1:443",
            b"172.18.0.1:443",
            b"169.254.169.254:80",
            b"100.64.0.1:443",
            b"[::1]:443",
        ):
            head = await _connect(port, b"CONNECT " + target + b" HTTP/1.1")
            assert head.startswith(b"HTTP/1.1 403"), (target, head)
        head = await _connect(port, b"GET http://example.com/ HTTP/1.1")
        assert head.startswith(b"HTTP/1.1 405")
        assert web_egress.stats()["egress_refused"] >= 7
    finally:
        await web_egress.stop()


async def test_egress_tunnels_to_a_public_address(monkeypatch):
    """A destination checked public is tunnelled (here a local echo server,
    the check forced to pass: no network)."""

    async def echo(reader, writer):
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    monkeypatch.setattr(netguard, "ip_is_public", lambda _ip: True)
    port = await web_egress.ensure_started()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(10), 5) == b"ping"
        writer.close()
    finally:
        await web_egress.stop()
        upstream.close()
        await upstream.wait_closed()
