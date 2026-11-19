"""Tools emulated on CLI routes, through the chat route (ADR-039).

With the instance option (``PLUGBOARD_CLI_TOOLS_EMULATION=1``) and a key
holding ``allow_cli_tools``, a request with ``tools`` that only CLI routes
serve is sent as text and the answer is read back into ``tool_calls``. An
answer off the protocol is a 502 ``cli_tool_call_invalid`` that is neither
retried, billed nor counted against the route.

No CLI runs: ``cli_subprocess.complete`` is a fake that records the body it
gets and answers a text chosen by the test. The HTTP provider is a real server
on 127.0.0.1.
"""

from __future__ import annotations

import json
import logging

import pytest
from aiohttp import web

from tests.factories import FAKE_COMPLETION, PROVIDER_A

MODEL = "pbtest-any-model"
ADMIN = {"Authorization": "Bearer testkey"}


def _cli(provider_id: str, kind: str) -> dict:
    return {
        "id": provider_id,
        "name": f"{kind} CLI test",
        "base_url": f"subprocess://{kind}",
        "bearer_token": "(via CLI)",
        "default_model": "pbtest-default",
        "auth_source": f"subprocess:{kind}",
        "provider_type": "cloud",
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


CODEX = _cli("pbtest-codex-cli", "codex")
CODEX_2 = _cli("pbtest-codex-cli-2", "codex")
HTTP_BASE = {k: v for k, v in PROVIDER_A.items() if k != "models"}

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather of a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}
CALL = json.dumps(
    {"tool_calls": [{"name": "get_weather", "arguments": {"city": "Rabat"}}]}
)


def _chat(**extra) -> dict:
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "weather in Rabat?"}],
        "tools": [WEATHER],
        "cache": False,
        **extra,
    }


@pytest.fixture
def cli(monkeypatch, tmp_path, _providers_file):
    """Fresh breaker, alerts, metrics, cache and usage; a fake CLI."""
    import plugboard.alerts as alerts
    import plugboard.cache as response_cache
    import plugboard.metrics as metrics
    import plugboard.model_quarantine as quarantine
    import plugboard.router as router
    import plugboard.usage as usage
    from plugboard import circuit_breaker as cb
    from plugboard import cli_subprocess, provider_admission

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    monkeypatch.setattr(metrics, "_stats", {})
    monkeypatch.setattr(response_cache, "_DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    qfile = tmp_path / "quarantined_models.json"
    monkeypatch.setattr(quarantine, "_QUARANTINE_FILE", qfile)
    for fn in (
        quarantine.record_failure,
        quarantine.record_account_unsupported,
        quarantine.record_terminal_unavailable,
    ):
        monkeypatch.setitem(fn.__kwdefaults__, "path", qfile)
    provider_admission.reset()

    class FakeCli:
        def __init__(self):
            self.calls: list[tuple[str, dict, object]] = []
            self.answers = [CALL]

        def write(self, *providers: dict) -> None:
            _providers_file.write_text(json.dumps({"providers": list(providers)}))
            router.invalidate_models_cache()

    fake = FakeCli()

    async def fake_complete(kind, body, *, timeout_s=None, on_delta=None):
        fake.calls.append((kind, json.loads(json.dumps(body)), on_delta))
        text = fake.answers[min(len(fake.calls), len(fake.answers)) - 1]
        answer = json.loads(json.dumps(FAKE_COMPLETION))
        answer["choices"][0]["message"]["content"] = text
        answer["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
        answer["usage"]["total_tokens"] = 0
        return answer

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    fake.write(CODEX)
    yield fake
    provider_admission.reset()


@pytest.fixture
def keys(tmp_path, monkeypatch):
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    return ak


@pytest.fixture
def agent(keys):
    """Authorization header of a key holding allow_cli_tools."""
    rec = keys.create_key("pbtest-agent", "test", allow_cli_tools=True)
    return {"Authorization": f"Bearer {rec['key']}"}


@pytest.fixture
def option_on(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_CLI_TOOLS_EMULATION", "1")


@pytest.fixture
async def http_provider():
    """An HTTP provider on 127.0.0.1 that answers a native tool call."""
    received: list[dict] = []
    answer = json.loads(json.dumps(FAKE_COMPLETION))
    answer["choices"][0]["message"] = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_http",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{}"},
            }
        ],
    }
    answer["choices"][0]["finish_reason"] = "tool_calls"

    async def handle(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.json_response(answer)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield {**HTTP_BASE, "base_url": f"http://127.0.0.1:{site.port}"}, received
    finally:
        await runner.cleanup()


# -- served (case 4 through the route) ------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_call_is_served_on_a_cli_route(client, cli, agent, option_on):
    resp = await client.post("/v1/chat/completions", headers=agent, json=_chat())

    assert resp.status_code == 200, resp.text
    choice = resp.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    (call,) = choice["message"]["tool_calls"]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Rabat"}
    ((kind, body, _),) = cli.calls
    assert kind == "codex"
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & set(body)
    assert body["messages"][0]["role"] == "system"
    assert "get_weather" in body["messages"][0]["content"]


# -- an answer off the protocol (cases 7 and 9) ---------------------------------


@pytest.mark.asyncio
async def test_an_answer_off_the_protocol_is_a_502_not_retried(
    client, cli, agent, option_on
):
    cli.answers = ["Sunny in Rabat."]

    resp = await client.post("/v1/chat/completions", headers=agent, json=_chat())

    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "cli_tool_call_invalid"
    assert resp.json()["error_ref"]
    assert resp.headers["x-should-retry"] == "false"
    assert len(cli.calls) == 1


@pytest.mark.asyncio
async def test_an_undeclared_tool_is_named_in_the_detail(client, cli, agent, option_on):
    cli.answers = [json.dumps({"tool_calls": [{"name": "rm_rf", "arguments": {}}]})]

    resp = await client.post("/v1/chat/completions", headers=agent, json=_chat())

    assert resp.status_code == 502, resp.text
    assert "rm_rf" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_a_streamed_request_gets_the_502_as_json(client, cli, agent, option_on):
    cli.answers = ["Sunny in Rabat."]

    resp = await client.post(
        "/v1/chat/completions", headers=agent, json=_chat(stream=True)
    )

    assert resp.status_code == 502, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["code"] == "cli_tool_call_invalid"
    assert resp.headers["x-should-retry"] == "false"
    assert len(cli.calls) == 1


@pytest.mark.asyncio
async def test_a_bad_answer_is_not_billed(client, cli, keys, option_on, monkeypatch):
    from types import SimpleNamespace

    import plugboard.usage as usage
    from plugboard import chat_billing
    from plugboard.billing import current_pricing_month

    rec = keys.create_key(
        "pbtest-agent", "t", monthly_limit_usd=100.0, allow_cli_tools=True
    )
    cli.answers = ["not json"]
    # A priced call, so the budget hold is not zero.
    monkeypatch.setattr(
        chat_billing, "price", lambda *a, **k: SimpleNamespace(cost_usd=0.5)
    )
    held: list = []
    real_hold = usage.hold_budget

    def spy_hold(**kwargs):
        held.append(kwargs["cost"])
        return real_hold(**kwargs)

    monkeypatch.setattr(usage, "hold_budget", spy_hold)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {rec['key']}"},
        json=_chat(),
    )

    assert resp.status_code == 502, resp.text
    assert usage.get_usage(current_pricing_month()) == []
    assert held and held[0] > 0  # a hold was taken...
    month = current_pricing_month()
    assert usage.get_held_cost(month, key_id=rec["key_id"]) == 0  # ...and given back


@pytest.mark.asyncio
async def test_a_bad_answer_does_not_fall_back_nor_count_against_the_route(
    client, cli, agent, option_on, monkeypatch
):
    import plugboard.router as router
    from plugboard import circuit_breaker as cb

    cli.write(CODEX, CODEX_2)
    cli.answers = ["not json", CALL]
    failures: list = []
    monkeypatch.setattr(cb, "record_failure", lambda *a, **k: failures.append(a))

    async def alert(provider_id):
        failures.append(("alert", provider_id))

    monkeypatch.setattr(router, "record_provider_failure", alert)
    import plugboard.metrics as metrics
    import plugboard.model_quarantine as quarantine

    real_record = metrics.record

    def record(provider_id, latency, *, error=False):
        if error:
            failures.append(("error-metric", provider_id))
        real_record(provider_id, latency, error=error)

    monkeypatch.setattr(metrics, "record", record)
    for name in (
        "record_failure",
        "record_account_unsupported",
        "record_terminal_unavailable",
    ):
        monkeypatch.setattr(
            quarantine, name, lambda *a, _n=name, **k: failures.append((_n, a))
        )

    resp = await client.post("/v1/chat/completions", headers=agent, json=_chat())

    assert resp.status_code == 502, resp.text
    assert len(cli.calls) == 1
    assert failures == []
    assert metrics._stats["pbtest-codex-cli"]["errors"] == 0


# -- streaming (case 11) --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["claude", "agy"])
async def test_a_streamed_request_is_answered_whole_then_replayed(
    client, cli, agent, option_on, monkeypatch, kind
):
    from plugboard import app as app_mod

    cli.write(_cli(f"pbtest-{kind}-cli", kind))
    streamed: list = []

    def no_stream(*args, **kwargs):
        streamed.append(args)
        raise AssertionError("forward_stream must not be used")

    monkeypatch.setattr(app_mod, "forward_stream", no_stream)

    resp = await client.post(
        "/v1/chat/completions", headers=agent, json=_chat(stream=True)
    )

    assert resp.status_code == 200, resp.text
    assert streamed == []
    ((got_kind, _, on_delta),) = cli.calls
    assert got_kind == kind and on_delta is None
    events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    names = [
        tc["function"]["name"]
        for e in events
        for tc in (e["choices"][0].get("delta") or {}).get("tool_calls") or []
        if (tc.get("function") or {}).get("name")
    ]
    finishes = [e["choices"][0].get("finish_reason") for e in events]
    assert names == ["get_weather"]
    assert "tool_calls" in finishes
    assert resp.text.rstrip().endswith("data: [DONE]")


# -- the other callers keep refusing (case 12) ----------------------------------


@pytest.mark.asyncio
async def test_without_the_flag_forward_and_forward_stream_refuse(cli, option_on):
    from plugboard.router import UnsupportedOnCliError, forward, forward_stream

    with pytest.raises(UnsupportedOnCliError):
        await forward(_chat())
    with pytest.raises(UnsupportedOnCliError):
        await forward_stream(_chat(stream=True)).__anext__()
    assert cli.calls == []


@pytest.fixture
def forward_spy(monkeypatch):
    """Records the keyword arguments of every forward() the app makes."""
    from plugboard import app as app_mod

    seen: list[dict] = []
    real = app_mod.forward

    async def spy(body, *args, **kwargs):
        seen.append(kwargs)
        return await real(body, *args, **kwargs)

    monkeypatch.setattr(app_mod, "forward", spy)
    return seen


def _no_instruction(cli) -> bool:
    return all(body["messages"][0]["role"] != "system" for _, body, _ in cli.calls)


@pytest.mark.asyncio
async def test_the_preflight_ping_never_emulates_tools(
    client, cli, agent, option_on, forward_spy
):
    resp = await client.post(
        "/v1/preflight",
        headers=agent,
        json={"items": [{"provider": "pbtest-codex-cli", "model": MODEL}]},
    )

    assert resp.status_code == 200, resp.text
    assert forward_spy and all(not kw.get("cli_tools") for kw in forward_spy)
    assert cli.calls and _no_instruction(cli)


@pytest.mark.asyncio
async def test_the_admin_internet_probe_never_emulates_tools(
    client, cli, option_on, forward_spy
):
    resp = await client.post(
        "/admin/models/check-internet",
        headers=ADMIN,
        json={"model_id": f"pbtest-codex-cli:{MODEL}"},
    )

    assert resp.status_code == 200, resp.text
    assert forward_spy and all(not kw.get("cli_tools") for kw in forward_spy)
    assert cli.calls and _no_instruction(cli)


@pytest.mark.asyncio
async def test_the_catalogue_probe_sends_plain_text(cli, option_on):
    from plugboard import model_availability

    await model_availability.probe_subprocess_model("codex", MODEL, timeout_s=5)

    ((kind, body, _),) = cli.calls
    assert kind == "codex"
    assert not {"tools", "tool_choice", "parallel_tool_calls"} & set(body)
    assert _no_instruction(cli)


# -- cache (case 13) ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cached_answer_is_not_served_once_the_option_is_off(
    client, cli, agent, option_on, monkeypatch
):
    body = {k: v for k, v in _chat().items() if k != "cache"}
    first = await client.post("/v1/chat/completions", headers=agent, json=body)
    assert first.status_code == 200, first.text
    again = await client.post("/v1/chat/completions", headers=agent, json=body)
    assert again.headers.get("x-cache") == "HIT"

    monkeypatch.delenv("PLUGBOARD_CLI_TOOLS_EMULATION")
    resp = await client.post("/v1/chat/completions", headers=agent, json=body)

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "unsupported_on_cli"
    assert "x-cache" not in resp.headers
    assert len(cli.calls) == 1


# -- tokens (case 14) -----------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_tokens_count_the_instruction(client, cli, agent, option_on):
    from plugboard import cli_tools
    from plugboard.tokenizer import estimate_prompt_tokens

    resp = await client.post("/v1/chat/completions", headers=agent, json=_chat())

    assert resp.status_code == 200, resp.text
    sent = cli_tools.to_text_body(_chat())["messages"]
    assert resp.json()["usage"]["prompt_tokens"] == estimate_prompt_tokens(sent)
    assert estimate_prompt_tokens(sent) > estimate_prompt_tokens(_chat()["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
async def test_billing_counts_the_instruction_when_the_router_estimate_fails(
    client, cli, agent, option_on, monkeypatch, stream
):
    """The router's estimate failed: billing estimates on the translated body."""
    import plugboard.tokenizer as tokenizer
    from plugboard import cli_tools

    real = tokenizer.estimate_prompt_tokens

    def broken(messages):
        raise RuntimeError("estimate failed")

    monkeypatch.setattr(tokenizer, "estimate_prompt_tokens", broken)

    resp = await client.post(
        "/v1/chat/completions", headers=agent, json=_chat(stream=stream)
    )

    assert resp.status_code == 200, resp.text
    sent = cli_tools.to_text_body(_chat())["messages"]
    if stream:
        usage = [
            json.loads(line[len("data: ") :])
            for line in resp.text.splitlines()
            if line.startswith("data: ") and '"usage"' in line
        ][-1]["usage"]
    else:
        usage = resp.json()["billing"]  # what was billed
    assert usage["prompt_tokens"] == real(sent)


@pytest.mark.asyncio
async def test_the_budget_hold_counts_the_instruction(
    client, cli, keys, option_on, monkeypatch
):
    from plugboard import cli_tools
    from plugboard.chat_path import limits

    rec = keys.create_key(
        "pbtest-agent", "t", monthly_limit_usd=100.0, allow_cli_tools=True
    )
    seen: list[int] = []
    real_estimate = limits._prompt_estimate

    def spy(body):
        tokens = real_estimate(body)
        seen.append(tokens)
        return tokens

    monkeypatch.setattr(limits, "_prompt_estimate", spy)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {rec['key']}"},
        json=_chat(),
    )

    assert resp.status_code == 200, resp.text
    assert seen and seen[0] == real_estimate(cli_tools.to_text_body(_chat()))


# -- bounds and sessions (case 15) ----------------------------------------------


def _tool(name: str, description: str = "") -> dict:
    return {"type": "function", "function": {"name": name, "description": description}}


ASSISTANT = {
    "role": "assistant",
    "content": None,
    "tool_calls": [
        {
            "id": "call_a",
            "type": "function",
            "function": {"name": "get_weather", "arguments": "{}"},
        }
    ],
}


def _with_tool_output(*contents: str) -> list:
    return [
        {"role": "user", "content": "weather?"},
        ASSISTANT,
        *({"role": "tool", "tool_call_id": "call_a", "content": c} for c in contents),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"tools": [_tool(f"t{i}") for i in range(33)]},
        {"tools": [_tool("big", "d" * (32 * 1024))]},
        {"messages": _with_tool_output("x" * (64 * 1024 + 1))},
        {"messages": _with_tool_output(*["x" * (60 * 1024)] * 5)},
        {"tool_choice": {"type": "function", "function": {"name": "nope"}}},
        {"session_id": "pbtest-session"},
    ],
    ids=[
        "33-tools",
        "tools-too-big",
        "tool-message-too-big",
        "tool-messages-too-big",
        "unknown-named-tool",
        "session",
    ],
)
async def test_a_request_past_a_bound_is_a_400_before_any_call(
    client, cli, agent, option_on, extra
):
    resp = await client.post("/v1/chat/completions", headers=agent, json=_chat(**extra))

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_request"
    assert cli.calls == []


@pytest.mark.asyncio
async def test_a_session_with_emulated_tools_says_why(client, cli, agent, option_on):
    resp = await client.post(
        "/v1/chat/completions", headers=agent, json=_chat(session_id="pbtest-s")
    )

    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "detail": "session_id cannot be used with tools on a CLI subscription route",
        "code": "invalid_request",
    }
    assert cli.calls == []


# -- mixed routes (case 16) -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_mixed_route_list_is_served_by_the_http_route(
    client, cli, agent, option_on, http_provider
):
    provider, received = http_provider
    cli.write(CODEX, provider)

    resp = await client.post("/v1/chat/completions", headers=agent, json=_chat())

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["tool_calls"][0]["id"] == "call_http"
    assert cli.calls == []
    assert received[0]["tools"] == [WEATHER]


# -- the log (case 17) ----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        json.dumps(
            {"tool_calls": [{"name": "get_weather", "arguments": {"city": "ARG_X"}}]}
        ),
        json.dumps(
            {"tool_calls": [{"name": "TOOL_X", "arguments": {"city": "ARG_X"}}]}
        ),
        "MODEL_TEXT_X",
    ],
    ids=["calls", "undeclared", "not-json"],
)
async def test_the_log_holds_no_tool_name_argument_or_output(
    client, cli, agent, option_on, caplog, answer
):
    cli.answers = [answer]
    body = _chat(
        messages=[
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "OUTPUT_X"},
        ]
    )

    with caplog.at_level(logging.DEBUG):
        await client.post("/v1/chat/completions", headers=agent, json=body)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "cli tools: provider=pbtest-codex-cli tools=1 outcome=" in text
    for secret in ("get_weather", "TOOL_X", "ARG_X", "OUTPUT_X", "MODEL_TEXT_X"):
        assert secret not in text, secret


@pytest.mark.asyncio
async def test_with_the_right_only_what_is_still_refused_is_listed(
    client, cli, agent, option_on
):
    image = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": "data:,"}},
            ],
        }
    ]
    resp = await client.post(
        "/v1/chat/completions", headers=agent, json=_chat(messages=image)
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "unsupported_on_cli"
    assert resp.json()["unsupported"] == ["content:image_url"]
    assert cli.calls == []
