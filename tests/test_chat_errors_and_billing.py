"""PLU-28: a client key never gets an exception's raw text, only a generic
message and an ``error_ref`` that finds it in the gateway log; the admin
keeps the text. A chat answered whole (a CLI route) is counted, priced,
recorded and replayed as a stream in one place, ``plugboard.chat_billing``.

``plugboard.app`` is patched by its dotted name, never through a module
object imported here: another test file reloads it. Nothing here reaches a
real CLI nor the network (``no_real_calls``)."""

import ast
import asyncio
import json
import logging
import re
from pathlib import Path

import pytest
from aioresponses import aioresponses

from plugboard import chat_billing, cli_subprocess, router
from plugboard.cli_auth import AuthError
from tests.factories import FAKE_COMPLETION, PROVIDER_A, PROVIDER_B

ROOT = Path(__file__).resolve().parent.parent
ADMIN = {"Authorization": "Bearer testkey"}
SECRET = "[test-provider] 10.0.0.7:8443 token=sk-live-secret"
SECRET_LOGGED = "[test-provider] 10.0.0.7:8443 token=***REDACTED***"
CLI = {
    "id": "pbtest-cli",
    "name": "CLI test",
    "base_url": "subprocess://claude",
    "bearer_token": "(via CLI)",
    "default_model": "model-x",
    "auth_source": "subprocess:claude",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
PRICE = {"input_price": 2.0, "output_price": 10.0}  # USD per million tokens


@pytest.fixture(autouse=True)
def no_real_calls(monkeypatch):
    """A CLI route is never run for real (the error tests also take away the
    router's HTTP session: a missed patch fails instead of calling out)."""

    async def no_cli(kind, body, *, timeout_s=None, on_delta=None):
        raise AssertionError("a test reached a CLI it did not fake")

    monkeypatch.setattr(cli_subprocess, "complete", no_cli)


@pytest.fixture
def keys(client, tmp_path, monkeypatch, _providers_file):
    """A client key, a usage database in tmp_path, a known price, a CLI route."""
    import plugboard.apikeys as ak
    import plugboard.billing as b
    import plugboard.usage as u

    _providers_file.write_text(json.dumps({"providers": [PROVIDER_A, PROVIDER_B, CLI]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_11")
    match = b.PriceMatch(dict(PRICE), "model-x", "models")
    monkeypatch.setattr(b, "resolve_price", lambda *a, **k: match)
    rec = ak.create_key("acme", "test")
    return {
        "client": {"Authorization": f"Bearer {rec['key']}"},
        "key_id": rec["key_id"],
    }


def _body(model, **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


def _fail_forward(monkeypatch, exc):
    async def forward(body, **kwargs):
        raise exc

    async def forward_stream(body, **kwargs):
        raise exc
        yield b""  # pragma: no cover - makes it an async generator

    async def no_network(provider):
        raise AssertionError("a test reached the network it did not fake")

    monkeypatch.setattr("plugboard.app.forward", forward)
    monkeypatch.setattr("plugboard.app.forward_stream", forward_stream)
    monkeypatch.setattr("plugboard.http_pool.session_for", no_network)


# ── Generic errors ───────────────────────────────────────────────────────────

ROUTES = [
    ("test-provider:fake:model-v1", False),
    ("test-provider:fake:model-v1", True),
    ("pbtest-cli:model-x", True),  # a CLI route asked to stream
]
ROUTE_IDS = ["plain", "stream", "cli-stream"]


@pytest.mark.parametrize(
    "exc, status",
    [
        (RuntimeError(SECRET), 502),
        (RuntimeError(f"{SECRET} timeout (>900s)"), 503),
        (AuthError(f"Auth not available for subprocess:claude. {SECRET}", 401), 401),
    ],
    ids=["failure", "timeout", "auth"],
)
@pytest.mark.parametrize("model, stream", ROUTES, ids=ROUTE_IDS)
async def test_a_client_key_gets_a_generic_error_with_a_reference(
    client, keys, monkeypatch, caplog, exc, status, model, stream
):
    _fail_forward(monkeypatch, exc)
    with caplog.at_level(logging.WARNING):
        resp = await client.post(
            "/v1/chat/completions",
            headers=keys["client"],
            json=_body(model, stream=stream),
        )
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert "secret" not in resp.text and "10.0.0.7" not in resp.text
    assert "subprocess:claude" not in resp.text
    assert re.fullmatch(r"[0-9a-f]{8}", body["error_ref"])
    assert body["error_ref"] in body["detail"]
    # The gateway log keeps the text, under the same reference; the log
    # filter masks the token in it (BUG-143).
    assert any(
        body["error_ref"] in r.getMessage() and SECRET_LOGGED in r.getMessage()
        for r in caplog.records
    )
    assert not any("sk-live-secret" in r.getMessage() for r in caplog.records)
    if status == 503:
        assert body["retry_after"] == 10 and resp.headers["Retry-After"] == "10"


@pytest.mark.parametrize("model, stream", ROUTES, ids=ROUTE_IDS)
async def test_the_admin_keeps_the_text(client, keys, monkeypatch, model, stream):
    _fail_forward(monkeypatch, RuntimeError(SECRET))
    resp = await client.post(
        "/v1/chat/completions", headers=ADMIN, json=_body(model, stream=stream)
    )
    assert resp.status_code == 502
    assert resp.json()["detail"] == SECRET
    assert re.fullmatch(r"[0-9a-f]{8}", resp.json()["error_ref"])


# ── One place for the billing of an answer given whole ───────────────────────


def _usage_rows():
    import plugboard.usage as u

    return u.get_usage("2026_11")


def _sse_events(text):
    return [
        json.loads(line[6:])
        for line in text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


@pytest.fixture
def cli_answers(monkeypatch):
    """The fake CLI answers with the given completion (deep-copied each time)."""
    answer = {"completion": dict(FAKE_COMPLETION, usage={})}

    async def complete(kind, body, *, timeout_s=None, on_delta=None):
        return json.loads(json.dumps(answer["completion"]))

    monkeypatch.setattr(cli_subprocess, "complete", complete)
    return answer


async def test_a_streamed_cli_chat_is_billed_like_a_plain_one(
    client, keys, monkeypatch, cli_answers
):
    alerts = []
    monkeypatch.setattr(
        "plugboard.app._send_spending_alert", lambda *a, **k: alerts.append(a)
    )
    model = "pbtest-cli:model-x"
    plain = await client.post(
        "/v1/chat/completions", headers=keys["client"], json=_body(model, cache=False)
    )
    streamed = await client.post(
        "/v1/chat/completions",
        headers=keys["client"],
        json=_body(model, stream=True, cache=False),
    )
    assert plain.status_code == streamed.status_code == 200
    first, second = _usage_rows()
    for field in ("prompt_tokens", "completion_tokens", "cost_usd", "price_key"):
        assert first[field] == second[field], field
    assert second["key_id"] == keys["key_id"] and second["cost_usd"] > 0
    final = _sse_events(streamed.text)[-1]
    assert final["usage"]["cost_usd"] == round(second["cost_usd"], 6)
    assert final["usage"]["completion_tokens"] == second["completion_tokens"]
    assert len(alerts) == 2  # the spending alert is checked on both


async def test_the_router_cli_stream_replays_through_the_shared_code(
    client, keys, cli_answers
):
    """forward_stream() on a CLI route: the answer's own finish_reason and
    tool calls, and a usage priced by chat_billing."""
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ],
    }
    cli_answers["completion"] = dict(
        FAKE_COMPLETION,
        choices=[{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        usage={},
    )
    provider = next(p for p in router.load_providers() if p["id"] == "pbtest-cli")
    chunks = [
        c async for c in router._stream_route(_body("model-x"), provider, "model-x", 30)
    ]
    events = _sse_events(b"".join(chunks).decode())
    assert events[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "f"
    final = events[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    expected = chat_billing.price(
        provider,
        "model-x",
        final["usage"]["prompt_tokens"],
        final["usage"]["completion_tokens"],
    )
    assert final["usage"]["cost_usd"] == round(expected.cost_usd, 6)


def _calls(path, names):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in names:
                found.append((name, node.lineno))
    return found


def test_chat_usage_is_priced_and_written_in_one_place():
    for module in ("app.py", "router.py"):
        path = ROOT / "plugboard" / module
        assert _calls(path, {"append_usage", "calculate_cost"}) == [], module
        assert "def _replay_completion_as_sse" not in path.read_text(encoding="utf-8")


def test_counts_estimate_only_what_the_provider_did_not_report():
    body = _body("m")
    result = dict(FAKE_COMPLETION, usage={"prompt_tokens": 0, "completion_tokens": 7})
    counted = chat_billing.counts(body, result, reported=False)
    assert counted[0] > 0 and counted[1] == 7  # the 0 is estimated
    assert chat_billing.counts(body, result, reported=True) == (0, 7)  # a count


@pytest.mark.parametrize("finish", ["stop", "tool_calls"])
async def test_replay_keeps_the_answer_finish_reason(finish):
    completion = dict(
        FAKE_COMPLETION,
        choices=[{"index": 0, "message": {"content": "a b"}, "finish_reason": finish}],
    )
    chunks = [
        c
        async for c in chat_billing.replay_as_sse(
            completion, completion_id="x", created=1, model="m"
        )
    ]
    events = _sse_events(b"".join(chunks).decode())
    assert [e["choices"][0]["delta"].get("content") for e in events[:-1]] == ["a ", "b"]
    assert events[-1]["choices"][0]["finish_reason"] == finish
    assert chunks[-1] == b"data: [DONE]\n\n"


async def test_plain_chat_still_records_and_reports_its_cost(client, keys):
    with aioresponses() as mock:
        mock.post("http://fake-llm.test/v1/chat/completions", payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions",
            headers=keys["client"],
            json=_body("test-provider:fake:model-v1", cache=False),
        )
    assert resp.status_code == 200
    [row] = _usage_rows()
    assert (row["prompt_tokens"], row["completion_tokens"]) == (5, 2)
    assert resp.json()["billing"]["cost_usd"] == row["cost_usd"]
    assert resp.headers["X-Cost-USD"] == str(row["cost_usd"])
    await asyncio.sleep(0)


# ── Review follow-ups ────────────────────────────────────────────────────────


async def test_v2_answers_the_same_generic_error(client, keys, monkeypatch):
    _fail_forward(monkeypatch, RuntimeError(SECRET))
    resp = await client.post(
        "/v2/chat/completions",
        headers=keys["client"],
        json=_body("test-provider:fake:model-v1"),
    )
    assert resp.status_code == 502 and "secret" not in resp.text
    assert resp.headers["X-API-Version"] == "2"


async def test_a_provider_credential_error_keeps_its_status(client, keys, monkeypatch):
    _fail_forward(monkeypatch, AuthError(f"Copilot licence missing {SECRET}", 403))
    resp = await client.post(
        "/v1/chat/completions",
        headers=keys["client"],
        json=_body("test-provider:fake:model-v1"),
    )
    assert resp.status_code == 403 and "secret" not in resp.text
    assert resp.json()["detail"].startswith("The provider's credentials")


async def test_a_timeout_names_the_provider_only(client, keys, monkeypatch):
    import asyncio as _asyncio

    monkeypatch.setattr(router, "_DELAYS", [])
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            exception=_asyncio.TimeoutError(),
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=keys["client"],
            json=_body("test-provider:fake:model-v1", cache=False),
        )
    assert resp.status_code == 503 and resp.headers["Retry-After"] == "10"
    # The whole sentence, not a substring search: the random reference may
    # itself contain "900" (BUG-155). Nothing else is in it: neither the model
    # nor the 900 s timeout.
    body = resp.json()
    ref = body["error_ref"]
    assert len(ref) == 8 and all(c in "0123456789abcdef" for c in ref)
    assert body["detail"] == (
        "The test-provider provider did not answer in time "
        f"(reference {ref} in the gateway log)."
    )


async def test_the_router_cli_stream_raises_before_its_first_chunk(keys):
    """Nothing is sent yet: the error goes to the chat route, which writes the
    answer (the CLI's text and stderr go to the admin only)."""

    async def failing(kind, body, *, timeout_s=None, on_delta=None):
        raise cli_subprocess.CliSubprocessError(
            "CLI failed: /home/ops/.claude/creds", stderr="ops@example.invalid"
        )

    provider = next(p for p in router.load_providers() if p["id"] == "pbtest-cli")
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli_subprocess, "complete", failing)
        gen = router._stream_route(_body("model-x"), provider, "model-x", 30)
        with _pytest.raises(cli_subprocess.CliSubprocessError):
            await gen.__anext__()


async def test_a_cli_stream_billing_error_still_serves_the_answer(
    client, keys, monkeypatch, cli_answers
):
    def broken(*args, **kwargs):
        raise OSError("usage.db is read-only")

    monkeypatch.setattr(chat_billing, "record", broken)
    resp = await client.post(
        "/v1/chat/completions",
        headers=keys["client"],
        json=_body("pbtest-cli:model-x", stream=True, cache=False),
    )
    assert resp.status_code == 200 and "data: [DONE]" in resp.text
    assert _usage_rows() == []


async def test_the_admin_gets_no_spending_alert(client, keys, monkeypatch, cli_answers):
    alerts = []
    monkeypatch.setattr(
        "plugboard.app._send_spending_alert", lambda *a, **k: alerts.append(a)
    )
    resp = await client.post(
        "/v1/chat/completions",
        headers=ADMIN,
        json=_body("pbtest-cli:model-x", stream=True, cache=False),
    )
    assert resp.status_code == 200 and alerts == []
    [row] = _usage_rows()
    assert row["client_id"] == "admin"
