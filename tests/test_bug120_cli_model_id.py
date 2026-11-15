"""A model ID outside a CLI route's catalogue never starts the CLI (BUG-120).

Constat (UAT, 15/11): ``Codex:--help``, ``Codex:-c``,
``claude-via-claude-cli-1:--dangerously-skip-permissions``,
``antigravity-via-agy-cli:--yolo`` reached the CLI as its ``--model`` value
(502 with the CLI's own message); with the admin key, any ID after a CLI
route's prefix started the CLI.

Now, for every CLI subscription route (claude, codex, gemini, agy), before
anything is started: an ID that begins with ``-``, holds a space, a line
break or another control character, or is not in the route's catalogue (its
listed models, default model, family aliases and the CLI's documented
aliases) is answered 404 ``model_not_found`` with a generic detail, for every
caller (the admin key included), on /v1 and /v2, streamed or not, and by
the preflight. Nothing is quarantined, no other route is tried. The CLI
wrapper refuses such an ID too, before its command line is built.

No CLI runs: the process creation is a recorder.
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import api_contract
from plugboard import cli_subprocess as cs
from tests.test_model_not_found import _isolated_providers

CODEX = {
    "id": "chatgpt-via-codex-cli",
    "name": "ChatGPT (via codex CLI)",
    "base_url": "subprocess://codex",
    "bearer_token": "(via CLI)",
    "auth_source": "subprocess:codex",
    "provider_type": "cloud",
    "default_model": "gpt-5.6-luna",
    "models": [{"id": "gpt-5.6-luna"}, {"id": "gpt-6-luna"}],
    "is_active": True,
}
CLAUDE = {
    "id": "claude-via-claude-cli-1",
    "name": "Claude (via claude CLI)",
    "base_url": "subprocess://claude",
    "bearer_token": "(via CLI)",
    "auth_source": "subprocess:claude",
    "provider_type": "cloud",
    "default_model": "claude-sonnet-5-5",
    "models": [{"id": "claude-haiku-4-5"}, {"id": "claude-sonnet-5-5"}],
    "is_active": True,
}
AGY = {
    "id": "antigravity-via-agy-cli",
    "name": "Antigravity (via agy CLI)",
    "base_url": "subprocess://agy",
    "bearer_token": "(via CLI)",
    "auth_source": "subprocess:agy",
    "provider_type": "cloud",
    "default_model": "gemini-3.8-flash-medium",
    "models": [{"id": "gemini-3.8-flash-medium"}, {"id": "claude-opus-5-5-medium"}],
    "is_active": True,
}
CLI_ROUTES = [CODEX, CLAUDE, AGY]
ADMIN_KEY = "admin-key-for-tests"
_REAL_COMPLETE = cs.complete  # before any fixture wraps it

# The values of the constat (SECU-UAT 15/11, section 5), then the ones that
# reached codex as a model name.
INJECTED = [
    "Codex:--help",
    "Codex:-c",
    "claude-via-claude-cli-1:--dangerously-skip-permissions",
    "claude-via-claude-cli-1:haiku --tools Bash",
    "Codex:gpt-5.6-luna\n--sandbox danger-full-access",
    "antigravity-via-agy-cli:--yolo",
]
OUTSIDE = [
    "Codex:$(id)",
    "Codex:`id`",
    "Codex:../../../etc/passwd",
    "claude-via-claude-cli-1:claude-made-up-9",
    "antigravity-via-agy-cli:gemini-made-up",
]
# What the CLI routes serve, as clients send it today.
SERVED = [
    "Codex:gpt-6-luna",  # display alias
    "Codex:",  # display alias, the default model
    "chatgpt-via-codex-cli:gpt-5.6-luna",  # registered ID
    "gpt-6-luna",  # bare name a CLI route lists
    "claude-via-claude-cli-1:claude-haiku-4-5",
    "claude-via-claude-cli-1:claude-sonnet-5-5",
    "claude-via-claude-cli-1:haiku",  # Claude Code's own aliases
    "claude-via-claude-cli-1:sonnet",
    "claude-via-claude-cli-1:opus",
    "claude-via-claude-cli-1:fable",
    "antigravity-via-agy-cli:auto",  # agy's automatic choice
    "antigravity-via-agy-cli:claude-opus-5-5-medium",  # an effort level
    "antigravity-via-agy-cli:gemini-3.8-flash-medium",
]


@pytest.fixture
def routes(tmp_path, monkeypatch):
    """The three CLI routes; codex signed in with an API key (nothing refused)."""
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    return _isolated_providers(tmp_path, monkeypatch, CLI_ROUTES)


@pytest.fixture
def spawned(monkeypatch, agy_without_cage):
    """Every CLI start, recorded: complete() entered, process created."""
    calls: dict = {"complete": [], "spawn": []}

    async def _complete(kind, body, **kwargs):
        calls["complete"].append((kind, body.get("model")))
        return await _REAL_COMPLETE(kind, body, **kwargs)

    async def _spawn(*argv, **kwargs):
        calls["spawn"].append(list(argv))
        raise RuntimeError("recorded: no CLI is started in tests")

    monkeypatch.setattr(cs, "complete", _complete)
    monkeypatch.setattr(cs, "_resolve_binary", lambda cmd: f"/fake/bin/{cmd}")
    monkeypatch.setattr(cs.asyncio, "create_subprocess_exec", _spawn)
    return calls


@pytest.fixture
def quarantined(monkeypatch):
    """Every quarantine record the router makes."""
    from plugboard import model_quarantine

    records: list = []
    for name in (
        "record_failure",
        "record_account_unsupported",
        "record_terminal_unavailable",
    ):
        monkeypatch.setattr(
            model_quarantine,
            name,
            lambda *a, _n=name, **k: records.append((_n, a, k)),
        )
    return records


@pytest.fixture
async def http(routes, spawned, quarantined, tmp_path, monkeypatch):
    """The app with the real router; a client key and the admin key."""
    import plugboard.app as app_module
    import plugboard.ratelimit as rl
    from plugboard import apikeys

    monkeypatch.setattr(app_module, "_API_KEY", "instance-key")
    monkeypatch.setattr(app_module, "_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setattr(rl, "_LIMIT", 0)
    monkeypatch.setattr(apikeys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(apikeys, "KEYS_LOCK", tmp_path / "keys.lock")
    client_key = apikeys.create_key("acme", "bug-120")["key"]
    auth = {
        "client key": {"Authorization": f"Bearer {client_key}"},
        "admin key": {"Authorization": f"Bearer {ADMIN_KEY}"},
    }
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        yield client, auth


def _chat(model: str, stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        "cache": False,
    }


def _assert_clean_404(resp, model: str) -> None:
    assert resp.status_code == 404, resp.text
    body = resp.json()
    api_contract.ErrorResponse.model_validate(body)
    assert body["code"] == "model_not_found"
    assert "GET /v1/models" in body["hint"]
    text = resp.text
    # Generic: no CLI, no CLI output, no pid, no echo of the refused ID.
    for word in ("CLI", "cli", "exited", "pid", "stderr", "--model", "codex", "agy"):
        assert word not in body["detail"], body["detail"]
    native = model.split(":", 1)[1]
    assert json.dumps(native)[1:-1] not in text, text


# ── The rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "--help",
        "-c",
        "-",
        "haiku --tools Bash",
        "gpt-5.6-luna\n--sandbox danger-full-access",
        "tab\tinside",
        "nul\x00inside",
        "zero​width",
        "line separator",
        "\x7fdelete",
    ],
)
def test_an_id_that_is_an_option_or_holds_a_blank_or_control_is_malformed(name):
    assert cs.is_malformed_model_id(name) is True


@pytest.mark.parametrize(
    "name", ["claude-haiku-4-5", "gpt-5.6-luna", "qwen3:8b", "sonnet[1m]", "a-b_c.d/e"]
)
def test_a_model_name_is_not_malformed(name):
    assert cs.is_malformed_model_id(name) is False


@pytest.mark.parametrize("model", INJECTED + OUTSIDE)
def test_an_id_outside_the_cli_catalogue_is_unserved(routes, model):
    from plugboard.router import is_unserved_cli_model

    assert is_unserved_cli_model(model) is True


@pytest.mark.parametrize("model", SERVED)
def test_what_a_cli_route_serves_is_not_unserved(routes, model):
    from plugboard.router import is_unserved_cli_model

    assert is_unserved_cli_model(model) is False


def test_a_model_the_subscription_refuses_still_reaches_codex(
    routes, tmp_path, monkeypatch
):
    """A listed model a ChatGPT sign-in refuses keeps its 400 (BUG-78)."""
    from plugboard.router import is_unserved_cli_model

    (tmp_path / "codex-home" / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {}})
    )
    refused = next(iter(cs.CHATGPT_REFUSED_CODEX_MODELS))
    (tmp_path / "again").mkdir()
    _isolated_providers(
        tmp_path / "again",
        monkeypatch,
        [{**CODEX, "models": [*CODEX["models"], {"id": refused}]}],
    )
    assert is_unserved_cli_model(f"Codex:{refused}") is False


def test_a_vendor_spelling_is_not_passed_to_a_cli_as_is(routes):
    """``openai:gpt-6-luna`` matches the codex route's ``gpt-6-luna`` by its
    clean form, but forward() would give codex ``openai:gpt-6-luna`` as its
    model, a name codex does not serve: refused, nothing is started."""
    from plugboard.router import _resolve_all_providers, is_unserved_cli_model

    assert [m for _p, m in _resolve_all_providers("openai:gpt-6-luna")] == [
        "openai:gpt-6-luna"
    ]
    assert is_unserved_cli_model("openai:gpt-6-luna") is True


def test_a_cli_route_without_a_list_refuses_malformed_ids_only(tmp_path, monkeypatch):
    """A route that lists no model may serve any name, the gateway's rule for
    every provider (is_unknown_model); an option or a blank never passes."""
    from plugboard.router import is_unserved_cli_model

    bare = {k: v for k, v in CLAUDE.items() if k != "models"}
    _isolated_providers(tmp_path, monkeypatch, [bare])
    assert is_unserved_cli_model("claude-via-claude-cli-1:claude-sonnet-5-5") is False
    assert is_unserved_cli_model("claude-via-claude-cli-1:not-listed-yet") is False
    for name in ("--help", "haiku --tools Bash", "a\nb"):
        assert is_unserved_cli_model(f"claude-via-claude-cli-1:{name}") is True


def test_an_http_route_is_not_judged_by_this_rule(tmp_path, monkeypatch):
    """Only a CLI reads the model name as a command-line value."""
    from plugboard.router import is_unserved_cli_model
    from tests.test_model_not_found import ACME

    _isolated_providers(tmp_path, monkeypatch, [ACME])
    assert is_unserved_cli_model("acme-direct:not-listed") is False


# ── What the chat routes answer ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize("caller", ["client key", "admin key"])
@pytest.mark.parametrize("model", INJECTED + OUTSIDE)
async def test_an_unserved_id_gets_404_and_starts_nothing(
    http, spawned, quarantined, model, caller, path, stream
):
    client, auth = http
    resp = await client.post(path, headers=auth[caller], json=_chat(model, stream))

    _assert_clean_404(resp, model)
    assert spawned == {"complete": [], "spawn": []}
    assert quarantined == []


@pytest.mark.asyncio
async def test_a_budgeted_key_gets_404_not_402(http, spawned, tmp_path):
    """The 404 comes before the price check (the constat's 402)."""
    from plugboard import apikeys

    client, _auth = http
    key = apikeys.create_key("acme", "budget", monthly_limit_usd=5.0)["key"]
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=_chat("Codex:--help"),
    )
    _assert_clean_404(resp, "Codex:--help")
    assert spawned["spawn"] == []


@pytest.mark.asyncio
async def test_a_key_whose_list_names_the_id_still_gets_404(http, spawned):
    """allowed_models cannot open a way to the CLI's options."""
    from plugboard import apikeys

    client, _auth = http
    key = apikeys.create_key("acme", "lim", allowed_models=["Codex:--help"])["key"]
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=_chat("Codex:--help"),
    )
    _assert_clean_404(resp, "Codex:--help")
    assert spawned["spawn"] == []


@pytest.mark.asyncio
async def test_a_limited_key_gets_its_403_first(http, spawned):
    """A limited key cannot probe which IDs exist; nothing starts either."""
    from plugboard import apikeys

    client, _auth = http
    key = apikeys.create_key(
        "acme", "lim", allowed_models=["chatgpt-via-codex-cli:gpt-6-luna"]
    )["key"]
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=_chat("Codex:--help"),
    )
    assert resp.status_code == 403, resp.text
    assert spawned["spawn"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "kind", "native"),
    [
        ("Codex:gpt-6-luna", "codex", "gpt-6-luna"),
        ("claude-via-claude-cli-1:claude-haiku-4-5", "claude", "claude-haiku-4-5"),
        ("claude-via-claude-cli-1:haiku", "claude", "haiku"),
        ("antigravity-via-agy-cli:auto", "agy", "auto"),
    ],
)
@pytest.mark.parametrize("caller", ["client key", "admin key"])
async def test_a_served_model_still_starts_its_cli_with_the_same_argv(
    http, spawned, model, kind, native, caller
):
    client, auth = http
    await client.post("/v1/chat/completions", headers=auth[caller], json=_chat(model))

    assert spawned["complete"] == [(kind, native)]
    (argv,) = spawned["spawn"]
    assert argv[0] == f"/fake/bin/{kind}"
    assert argv[argv.index("--model") + 1] == native
    # The very command line complete() builds for that model on its own.
    with pytest.raises(RuntimeError, match="recorded"):
        await _REAL_COMPLETE(kind, _chat(native))
    assert spawned["spawn"][1] == argv


# ── The preflight ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["client key", "admin key"])
@pytest.mark.parametrize(
    ("provider", "model"),
    [("Codex", "--help"), ("antigravity-via-agy-cli", "--yolo")],
)
async def test_preflight_answers_model_not_found_without_a_ping(
    http, spawned, caller, provider, model
):
    client, auth = http
    resp = await client.post(
        "/v1/preflight",
        headers=auth[caller],
        json={"items": [{"provider": provider, "model": model}]},
    )
    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["results"]
    assert result["ok"] is False
    assert result["error"] == "model_not_found"
    assert spawned == {"complete": [], "spawn": []}


# ── The router itself ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["forward", "forward_stream"])
async def test_the_router_refuses_before_any_start(
    routes, spawned, quarantined, stream
):
    """Callers that skip the chat route (jobs, benchmarks) are refused too."""
    from plugboard import router

    with pytest.raises(cs.ModelNotServedError):
        if stream:
            async for _chunk in router.forward_stream(_chat("Codex:--help")):
                pass
        else:
            await router.forward(_chat("Codex:--help"))
    assert spawned == {"complete": [], "spawn": []}
    assert quarantined == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["forward", "forward_stream"])
async def test_the_wrapper_refusal_is_final_for_the_router(
    tmp_path, monkeypatch, spawned, quarantined, stream
):
    """Were a malformed ID to pass the router's check, the CLI wrapper refuses
    it: forward() raises at once, no other route is tried, no quarantine, no
    circuit count."""
    from plugboard import circuit_breaker, router

    # Two CLI routes without a list: an unprefixed name may go to both.
    no_list = [{k: v for k, v in p.items() if k != "models"} for p in (CODEX, CLAUDE)]
    _isolated_providers(tmp_path, monkeypatch, no_list)
    monkeypatch.setattr(router, "cli_route_refuses", lambda *_a: False)
    failures: list = []
    monkeypatch.setattr(
        circuit_breaker, "record_failure", lambda *a, **k: failures.append(a)
    )

    with pytest.raises(cs.ModelNotServedError):
        if stream:
            async for _chunk in router.forward_stream(_chat("--help")):
                pass
        else:
            await router.forward(_chat("--help"))
    assert len(spawned["complete"]) == 1, "the second route is not tried"
    assert spawned["spawn"] == []
    assert quarantined == []
    assert failures == []


# ── The CLI wrapper's own check ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["codex", "claude", "gemini", "agy"])
@pytest.mark.parametrize("model", ["--help", "-c", "a b", "a\nb", "a\x1bb"])
async def test_the_wrapper_refuses_before_building_the_argv(spawned, kind, model):
    with pytest.raises(cs.ModelNotServedError) as info:
        await _REAL_COMPLETE(kind, _chat(model))
    assert spawned["spawn"] == []
    assert model not in str(info.value)


# ── The MCP tools go through the gateway ────────────────────────────────────


@pytest.fixture
async def mcp_gateway(monkeypatch):
    """A gateway answering every chat with the chat routes' own 404 body."""
    pytest.importorskip("mcp")
    from aiohttp import web

    from plugboard import mcp_server
    from plugboard.app import _model_not_served_response

    built = _model_not_served_response()

    async def chat(_request):
        return web.Response(
            body=built.body,
            status=built.status_code,
            headers={"Content-Type": "application/json"},
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(mcp_server, "_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(mcp_server, "_API_KEY", "pb_client")
    monkeypatch.setattr(mcp_server, "_HTTP_SESSION", None)
    try:
        yield json.loads(built.body)
    finally:
        if mcp_server._HTTP_SESSION is not None:
            await mcp_server._HTTP_SESSION.close()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["chat", "chat_stream"])
async def test_mcp_chat_tools_report_the_404_as_a_tool_error(mcp_gateway, tool):
    from mcp.types import CallToolResult

    from plugboard import mcp_server

    out = await getattr(mcp_server, tool)(model="Codex:--help", message="hi")
    assert isinstance(out, CallToolResult) and out.isError is True
    assert out.content[0].text == (f"Error: model_not_found: {mcp_gateway['detail']}")


# ── The contract ────────────────────────────────────────────────────────────


def test_the_contract_documents_the_cli_catalogue_404():
    major, minor, _patch = (int(p) for p in api_contract.CONTRACT_VERSION.split("."))
    assert (major, minor) >= (2, 6)
    doc = api_contract.build_openapi()
    chat = doc["paths"]["/v1/chat/completions"]["post"]["responses"]["404"]
    assert "catalogue" in chat["description"]
    assert "-" in chat["description"]
