"""agy reported token usage: complete(), router, HTTP responses and billing rows.

Only the subprocess boundary (`asyncio.create_subprocess_exec`) is faked: the
real cli_subprocess parser, router, tokenizer, billing and SQLite usage store
run. Other CLIs keep the tiktoken estimate when they report no counters.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from aioresponses import aioresponses

from plugboard import cli_subprocess
from plugboard.tokenizer import estimate_completion_tokens, estimate_prompt_tokens
from tests.factories import PROVIDER_A

AUTH = {"Authorization": "Bearer testkey"}
MONTH = "2026_03"
INPUT_PRICE = 2.0
OUTPUT_PRICE = 10.0

AGY_ID = "pbtest-agy-cli"
AGY_MODEL = "pbtest-agy-model-medium"
GEMINI_ID = "pbtest-gemini-cli"
GEMINI_MODEL = "pbtest-gemini-model"
CODEX_ID = "pbtest-codex-cli"
CODEX_MODEL = "pbtest-codex-model"

AGY_PROVIDER = {
    "id": AGY_ID,
    "name": "Antigravity test",
    "base_url": "subprocess://agy",
    "bearer_token": "(via CLI)",
    "default_model": AGY_MODEL,
    "auth_source": "subprocess:agy",
    "provider_type": "cloud",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}
GEMINI_PROVIDER = {
    **AGY_PROVIDER,
    "id": GEMINI_ID,
    "name": "Gemini CLI test",
    "base_url": "subprocess://gemini",
    "default_model": GEMINI_MODEL,
    "auth_source": "subprocess:gemini",
}
CODEX_PROVIDER = {
    **AGY_PROVIDER,
    "id": CODEX_ID,
    "name": "Codex CLI test",
    "base_url": "subprocess://codex",
    "default_model": CODEX_MODEL,
    "auth_source": "subprocess:codex",
}

REPORTED_USAGE = {
    "input_tokens": 11510,
    "output_tokens": 4,
    "thinking_tokens": 0,
    "cache_read_tokens": 0,
    "total_tokens": 11514,
}
_MISSING = object()


def _agy_stdout(usage=REPORTED_USAGE, response="OK", status="SUCCESS") -> bytes:
    payload = {"status": status, "response": response}
    if usage is not _MISSING:
        payload["usage"] = usage
    return json.dumps(payload).encode()


def _expected_cost(prompt: int, completion: int) -> float:
    return prompt / 1_000_000 * INPUT_PRICE + completion / 1_000_000 * OUTPUT_PRICE


def _estimates(messages, text):
    prompt = estimate_prompt_tokens(messages)
    completion = estimate_completion_tokens(
        {"choices": [{"message": {"content": text}}]}
    )
    return prompt, completion


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def _providers_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Overrides the conftest fixture: CLI providers plus one HTTP provider."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pfile = data_dir / "providers.json"
    providers = [AGY_PROVIDER, GEMINI_PROVIDER, CODEX_PROVIDER, PROVIDER_A]
    pfile.write_text(json.dumps({"providers": providers}))
    monkeypatch.setenv("PLUGBOARD_DATA", str(data_dir))
    import plugboard.config as cfg

    cfg.PROVIDERS_FILE = pfile
    cfg.PROVIDERS_LOCK = data_dir / "providers.json.lock"
    from plugboard.router import invalidate_models_cache

    invalidate_models_cache()
    return pfile


@pytest.fixture(autouse=True)
def _billing_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Private SQLite usage store and a synthetic price grid."""
    import plugboard.billing as b
    import plugboard.usage as u

    store = tmp_path / "usage-store"
    monkeypatch.setattr(u, "_DATA_DIR", store)
    monkeypatch.setattr(u, "_DB_PATH", store / "usage.db")
    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    monkeypatch.setattr(b, "current_pricing_month", lambda: MONTH)
    price = {"input_price": INPUT_PRICE, "output_price": OUTPUT_PRICE}
    b.save_pricing(
        MONTH,
        {
            "models": {},
            "models_by_provider": {
                AGY_ID: {AGY_MODEL: dict(price)},
                GEMINI_ID: {GEMINI_MODEL: dict(price)},
                CODEX_ID: {CODEX_MODEL: dict(price)},
            },
        },
    )
    return u


class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        self.returncode = returncode
        self.pid = 424242
        self._out = (stdout, stderr)

    async def communicate(self, input=None):
        return self._out

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


class _FakeCli:
    def __init__(self):
        self.stdout = b""
        self.returncode = 0
        self.calls = []

    def set(self, stdout: bytes, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode

    async def exec(self, *argv, **kwargs):
        self.calls.append(list(argv))
        return _FakeProc(self.stdout, self.returncode)


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeCli:
    fake = _FakeCli()
    monkeypatch.setattr(cli_subprocess, "_binary_path_cache", {})
    monkeypatch.setattr(
        cli_subprocess.shutil,
        "which",
        lambda cmd: f"/fake/bin/{cmd}" if cmd in {"agy", "gemini", "codex"} else None,
    )
    monkeypatch.setattr(cli_subprocess.asyncio, "create_subprocess_exec", fake.exec)
    spawn = tmp_path / "spawn"
    spawn.mkdir()
    monkeypatch.setenv("PLUGBOARD_SUBPROCESS_CWD", str(spawn))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gemini-operator"))
    return fake


def _messages(prompt: str):
    return [{"role": "user", "content": prompt}]


async def _call(client, model: str, prompt: str, stream: bool):
    res = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": model,
            "messages": _messages(prompt),
            "stream": stream,
            "cache": False,
        },
    )
    return res


def _sse_events(text: str):
    events = []
    for line in text.splitlines():
        if line.startswith("data: {"):
            events.append(json.loads(line[len("data: ") :]))
    return events


def _answer(res, stream: bool):
    """(content, usage) as the caller sees them."""
    assert res.status_code == 200, res.text
    if not stream:
        data = res.json()
        return data["choices"][0]["message"]["content"], data["usage"]
    assert "data: [DONE]" in res.text
    events = _sse_events(res.text)
    content = "".join(
        e["choices"][0]["delta"].get("content", "") for e in events if e.get("choices")
    )
    with_usage = [e["usage"] for e in events if e.get("usage")]
    assert len(with_usage) == 1, f"expected one usage chunk, got {with_usage}"
    return content, with_usage[0]


def _single_row(store, provider_id: str, model: str):
    rows = store.get_usage(MONTH)
    assert len(rows) == 1, f"expected exactly one billing row, got {rows}"
    row = rows[0]
    assert row["provider_id"] == provider_id
    assert row["model"] == model
    return row


# ── complete(): the real parser reads agy's usage ───────────────────────────


async def test_complete_returns_agy_reported_usage(fake_cli):
    fake_cli.set(_agy_stdout())
    messages = _messages("hi")
    result = await cli_subprocess.complete(
        "agy", {"model": AGY_MODEL, "messages": messages}
    )
    assert fake_cli.calls and fake_cli.calls[0][0] == "/fake/bin/agy"
    assert "--output-format" in fake_cli.calls[0]
    assert result["choices"][0]["message"]["content"] == "OK"
    usage = result["usage"]
    assert usage["prompt_tokens"] == 11510
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == 11514
    # The visible text would estimate to a handful of tokens.
    assert _estimates(messages, "OK")[0] < 100


@pytest.mark.parametrize(
    "stdout",
    [
        b"Loaded cached credentials.\n" + _agy_stdout() + b"\nSession closed.\n",
        b"Warning: something noisy\n"
        + json.dumps(
            {"status": "SUCCESS", "response": "OK", "usage": REPORTED_USAGE}, indent=2
        ).encode()
        + b"\n",
    ],
    ids=["single-line", "pretty-printed"],
)
async def test_complete_reads_agy_usage_through_noise(fake_cli, stdout):
    fake_cli.set(stdout)
    result = await cli_subprocess.complete(
        "agy", {"model": AGY_MODEL, "messages": _messages("hi")}
    )
    assert result["choices"][0]["message"]["content"] == "OK"
    assert result["usage"]["prompt_tokens"] == 11510
    assert result["usage"]["completion_tokens"] == 4
    assert result["usage"]["total_tokens"] == 11514


# ── HTTP: response usage and SQLite row match agy's counters ────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_agy_reported_usage_matches_billing_row(
    client, fake_cli, _billing_store, stream
):
    fake_cli.set(_agy_stdout())
    res = await _call(client, f"{AGY_ID}:{AGY_MODEL}", f"agy-usage-{stream}", stream)
    content, usage = _answer(res, stream)
    assert content.strip() == "OK"
    assert fake_cli.calls[0][0] == "/fake/bin/agy"
    assert usage["prompt_tokens"] == 11510
    assert usage["completion_tokens"] == 4
    assert usage["total_tokens"] == 11514
    cost = _expected_cost(11510, 4)
    if stream:
        assert usage["cost_usd"] == pytest.approx(cost, abs=1e-6)
    else:
        billing = res.json()["billing"]
        assert billing["prompt_tokens"] == 11510
        assert billing["completion_tokens"] == 4
        assert billing["total_tokens"] == 11514
        assert billing["cost_usd"] == pytest.approx(cost)
    row = _single_row(_billing_store, AGY_ID, AGY_MODEL)
    assert row["prompt_tokens"] == 11510
    assert row["completion_tokens"] == 4
    assert row["total_tokens"] == 11514
    assert row["cost_usd"] == pytest.approx(cost)


async def test_router_forward_stream_uses_agy_reported_usage(_providers_file, fake_cli):
    from plugboard import router

    fake_cli.set(_agy_stdout())
    body = {"model": f"{AGY_ID}:{AGY_MODEL}", "messages": _messages("hi")}
    raw = b"".join([chunk async for chunk in router.forward_stream(body)])
    events = _sse_events(raw.decode())
    usage = [e["usage"] for e in events if e.get("usage")]
    assert len(usage) == 1
    assert usage[0]["prompt_tokens"] == 11510
    assert usage[0]["completion_tokens"] == 4
    assert usage[0]["total_tokens"] == 11514
    assert usage[0]["cost_usd"] == pytest.approx(_expected_cost(11510, 4), abs=1e-6)


# ── Explicit zero counters are reported values, not "missing" ───────────────

_ZERO_CASES = [
    {"input_tokens": 0, "output_tokens": 7, "total_tokens": 7},
    {"input_tokens": 523, "output_tokens": 0, "total_tokens": 523},
    {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
]
_ZERO_IDS = ["zero-input", "zero-output", "zero-both"]
_LONG_TEXT = "Hello there, this answer has quite a few visible words in it."


@pytest.mark.parametrize("usage", _ZERO_CASES, ids=_ZERO_IDS)
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_agy_explicit_zero_is_kept(
    client, fake_cli, _billing_store, usage, stream
):
    fake_cli.set(_agy_stdout(usage=usage, response=_LONG_TEXT))
    prompt = f"zero-{usage['input_tokens']}-{stream}"
    est_p, est_c = _estimates(_messages(prompt), _LONG_TEXT)
    assert est_p > 0 and est_c > 0  # an estimate would be visible
    res = await _call(client, f"{AGY_ID}:{AGY_MODEL}", prompt, stream)
    content, got = _answer(res, stream)
    assert content.strip() == _LONG_TEXT
    assert got["prompt_tokens"] == usage["input_tokens"]
    assert got["completion_tokens"] == usage["output_tokens"]
    assert got["total_tokens"] == usage["total_tokens"]
    row = _single_row(_billing_store, AGY_ID, AGY_MODEL)
    assert row["prompt_tokens"] == usage["input_tokens"]
    assert row["completion_tokens"] == usage["output_tokens"]
    assert row["total_tokens"] == usage["total_tokens"]


@pytest.mark.parametrize("usage", _ZERO_CASES, ids=_ZERO_IDS)
async def test_router_forward_stream_keeps_agy_explicit_zero(
    _providers_file, fake_cli, usage
):
    from plugboard import router

    fake_cli.set(_agy_stdout(usage=usage, response=_LONG_TEXT))
    body = {"model": f"{AGY_ID}:{AGY_MODEL}", "messages": _messages("hi")}
    raw = b"".join([chunk async for chunk in router.forward_stream(body)])
    final = [e["usage"] for e in _sse_events(raw.decode()) if e.get("usage")][-1]
    assert final["prompt_tokens"] == usage["input_tokens"]
    assert final["completion_tokens"] == usage["output_tokens"]
    assert final["total_tokens"] == usage["total_tokens"]


# ── Untrusted usage falls back to the estimate, content stays healthy ──────


def _with(**fields):
    usage = {"input_tokens": 11510, "output_tokens": 4, "total_tokens": 11514}
    for key, value in fields.items():
        if value is _MISSING:
            usage.pop(key)
        else:
            usage[key] = value
    return usage


_UNTRUSTED = {
    "no-usage": _MISSING,
    "usage-null": None,
    "usage-string": "11514",
    "usage-list": [11510, 4],
    "usage-empty": {},
    "input-bool": _with(input_tokens=True),
    "input-string": _with(input_tokens="11510"),
    "input-fraction": _with(input_tokens=11510.5),
    "input-negative": _with(input_tokens=-1),
    "input-null": _with(input_tokens=None),
    "input-missing": _with(input_tokens=_MISSING),
    "output-bool": _with(output_tokens=False),
    "output-string": _with(output_tokens="4"),
    "output-fraction": _with(output_tokens=0.5),
    "output-negative": _with(output_tokens=-4),
    "output-null": _with(output_tokens=None),
    "output-missing": _with(output_tokens=_MISSING),
}


@pytest.mark.parametrize("usage", list(_UNTRUSTED.values()), ids=list(_UNTRUSTED))
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_agy_untrusted_usage_keeps_estimate(
    client, fake_cli, _billing_store, usage, stream
):
    fake_cli.set(_agy_stdout(usage=usage, response=_LONG_TEXT))
    prompt = "untrusted usage probe"
    est_p, est_c = _estimates(_messages(prompt), _LONG_TEXT)
    res = await _call(client, f"{AGY_ID}:{AGY_MODEL}", prompt, stream)
    content, got = _answer(res, stream)
    assert content.strip() == _LONG_TEXT
    assert got["prompt_tokens"] == est_p
    assert got["completion_tokens"] == est_c
    assert got["total_tokens"] == est_p + est_c
    row = _single_row(_billing_store, AGY_ID, AGY_MODEL)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (est_p, est_c)


@pytest.mark.parametrize("usage", list(_UNTRUSTED.values()), ids=list(_UNTRUSTED))
async def test_complete_untrusted_usage_does_not_crash(fake_cli, usage):
    fake_cli.set(_agy_stdout(usage=usage))
    result = await cli_subprocess.complete(
        "agy", {"model": AGY_MODEL, "messages": _messages("hi")}
    )
    assert result["choices"][0]["message"]["content"] == "OK"
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = result["usage"][key]
        assert type(value) is int and value >= 0


# ── Optional fields, details and totals ─────────────────────────────────────


@pytest.mark.parametrize(
    "usage,total",
    [
        ({"input_tokens": 11510, "output_tokens": 4, "total_tokens": 11514}, 11514),
        ({"input_tokens": 11510, "output_tokens": 4}, 11514),
        (
            {
                "input_tokens": 11510,
                "output_tokens": 40,
                "thinking_tokens": 30,
                "cache_read_tokens": 9000,
                "total_tokens": 11550,
            },
            11550,
        ),
    ],
    ids=["no-details", "no-total", "details-not-added"],
)
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_agy_core_counters_map_directly(
    client, fake_cli, _billing_store, usage, total, stream
):
    fake_cli.set(_agy_stdout(usage=usage))
    res = await _call(client, f"{AGY_ID}:{AGY_MODEL}", "details probe", stream)
    _, got = _answer(res, stream)
    assert got["prompt_tokens"] == usage["input_tokens"]
    assert got["completion_tokens"] == usage["output_tokens"]
    assert got["total_tokens"] == total
    row = _single_row(_billing_store, AGY_ID, AGY_MODEL)
    assert row["prompt_tokens"] == usage["input_tokens"]
    assert row["completion_tokens"] == usage["output_tokens"]
    assert row["total_tokens"] == total


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_agy_details_survive_and_marker_is_internal(
    client, fake_cli, stream
):
    usage = {
        "input_tokens": 11510,
        "output_tokens": 40,
        "thinking_tokens": 30,
        "cache_read_tokens": 9000,
        "total_tokens": 11550,
    }
    fake_cli.set(_agy_stdout(usage=usage))
    res = await _call(client, f"{AGY_ID}:{AGY_MODEL}", "marker probe", stream)
    _, got = _answer(res, stream)
    assert got["prompt_tokens_details"] == {"cached_tokens": 9000}
    assert got["completion_tokens_details"] == {"reasoning_tokens": 30}
    assert cli_subprocess.USAGE_REPORTED_KEY not in res.text


@pytest.mark.parametrize("bad_total", [99999, 0, -1, "11514", True, 11514.5], ids=str)
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_agy_contradictory_total_is_not_authoritative(
    client, fake_cli, _billing_store, bad_total, stream
):
    fake_cli.set(_agy_stdout(usage=_with(total_tokens=bad_total)))
    prompt = "total probe"
    est = _estimates(_messages(prompt), "OK")
    res = await _call(client, f"{AGY_ID}:{AGY_MODEL}", prompt, stream)
    content, got = _answer(res, stream)
    assert content.strip() == "OK"
    pair = (got["prompt_tokens"], got["completion_tokens"])
    # The whole report is rejected: estimated, never the reported pair.
    assert pair == est
    assert type(got["total_tokens"]) is int
    assert got["total_tokens"] == sum(pair)
    row = _single_row(_billing_store, AGY_ID, AGY_MODEL)
    assert (row["prompt_tokens"], row["completion_tokens"]) == pair
    assert row["total_tokens"] == sum(pair)


# ── Auth errors are unchanged and bill nothing ──────────────────────────────

_AUTH_ERROR = json.dumps(
    {
        "status": "ERROR",
        "error": "Authentication required: please sign in.",
        "usage": REPORTED_USAGE,
    }
).encode()


@pytest.mark.parametrize("returncode", [0, 1])
async def test_complete_agy_auth_error_unchanged(fake_cli, returncode):
    fake_cli.set(_AUTH_ERROR, returncode=returncode)
    with pytest.raises(cli_subprocess.CliSubprocessError) as info:
        await cli_subprocess.complete(
            "agy", {"model": AGY_MODEL, "messages": _messages("hi")}
        )
    assert cli_subprocess.AGY_SIGN_IN_HINT in str(info.value)


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_agy_auth_error_bills_nothing(
    client, fake_cli, _billing_store, stream
):
    fake_cli.set(_AUTH_ERROR)
    res = await _call(client, f"{AGY_ID}:{AGY_MODEL}", "auth probe", stream)
    assert res.status_code == 502
    assert cli_subprocess.AGY_SIGN_IN_HINT in res.json()["detail"]
    assert _billing_store.get_usage(MONTH) == []


# ── Other backends keep the estimator ───────────────────────────────────────

_CODEX_STDOUT = (
    json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": _LONG_TEXT},
        }
    )
    + "\n"
).encode()


@pytest.mark.parametrize(
    "provider_id,model,stdout",
    [
        (GEMINI_ID, GEMINI_MODEL, json.dumps({"response": _LONG_TEXT}).encode()),
        # agy-shaped usage on another CLI is not trusted: the change is agy only.
        (
            GEMINI_ID,
            GEMINI_MODEL,
            json.dumps({"response": _LONG_TEXT, "usage": REPORTED_USAGE}).encode(),
        ),
        (CODEX_ID, CODEX_MODEL, _CODEX_STDOUT),
    ],
    ids=["gemini-no-usage", "gemini-agy-shaped-usage", "codex-no-usage"],
)
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_other_cli_keeps_estimate(
    client, fake_cli, _billing_store, provider_id, model, stdout, stream
):
    fake_cli.set(stdout)
    prompt = "other cli probe"
    est_p, est_c = _estimates(_messages(prompt), _LONG_TEXT)
    res = await _call(client, f"{provider_id}:{model}", prompt, stream)
    content, got = _answer(res, stream)
    assert content.strip() == _LONG_TEXT
    assert (got["prompt_tokens"], got["completion_tokens"]) == (est_p, est_c)
    row = _single_row(_billing_store, provider_id, model)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (est_p, est_c)


async def test_http_provider_zero_usage_still_estimated(client, _billing_store):
    text = "Hello from the HTTP provider"
    completion = {
        "id": "chatcmpl-zero",
        "object": "chat.completion",
        "created": 1774000000,
        "model": "fake:model-v1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    prompt = "http zero probe"
    est_p, est_c = _estimates(_messages(prompt), text)
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions", payload=completion, status=200
        )
        res = await _call(client, "test-provider:fake:model-v1", prompt, False)
    assert res.status_code == 200, res.text
    billing = res.json()["billing"]
    assert (billing["prompt_tokens"], billing["completion_tokens"]) == (est_p, est_c)
    rows = _billing_store.get_usage(MONTH)
    assert len(rows) == 1
    assert (rows[0]["prompt_tokens"], rows[0]["completion_tokens"]) == (est_p, est_c)


# ── The trusted marker cannot be forged or inherited ────────────────────────

_HTTP_TEXT = "Hello from the HTTP provider"


def _http_zero_completion(**extra):
    return {
        "id": "chatcmpl-zero",
        "object": "chat.completion",
        "created": 1774000000,
        "model": "fake:model-v1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": _HTTP_TEXT},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        **extra,
    }


async def test_http_upstream_marker_does_not_make_zero_trusted(client, _billing_store):
    completion = _http_zero_completion(**{cli_subprocess.USAGE_REPORTED_KEY: True})
    prompt = "http forged marker probe"
    est_p, est_c = _estimates(_messages(prompt), _HTTP_TEXT)
    assert est_p > 0 and est_c > 0
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions", payload=completion, status=200
        )
        res = await _call(client, "test-provider:fake:model-v1", prompt, False)
    assert res.status_code == 200, res.text
    billing = res.json()["billing"]
    assert (billing["prompt_tokens"], billing["completion_tokens"]) == (est_p, est_c)
    row = _single_row(_billing_store, "test-provider", "fake:model-v1")
    assert (row["prompt_tokens"], row["completion_tokens"]) == (est_p, est_c)


async def test_trusted_marker_is_not_inherited_by_later_calls(
    _providers_file, fake_cli, _billing_store
):
    """Consecutive forward() calls in one context: the marker is reset each time."""
    from plugboard import app as app_module
    from plugboard import router

    agy_body = {"model": f"{AGY_ID}:{AGY_MODEL}", "messages": _messages("hi")}
    fake_cli.set(_agy_stdout(usage={"input_tokens": 0, "output_tokens": 0}))
    result = await router.forward(agy_body)
    assert router.usage_reported() is True
    assert cli_subprocess.USAGE_REPORTED_KEY not in result

    # A failed agy call after a trusted one leaves no trusted state behind.
    fake_cli.set(_AUTH_ERROR)
    with pytest.raises(cli_subprocess.CliSubprocessError):
        await router.forward(agy_body)
    assert router.usage_reported() is False

    fake_cli.set(_agy_stdout(usage={"input_tokens": 0, "output_tokens": 0}))
    await router.forward(agy_body)
    assert router.usage_reported() is True

    http_body = {
        "model": "test-provider:fake:model-v1",
        "messages": _messages("http after agy"),
    }
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=_http_zero_completion(),
            status=200,
        )
        http_result = await router.forward(http_body)
    assert router.usage_reported() is False
    est_p, est_c = _estimates(http_body["messages"], _HTTP_TEXT)
    bill = app_module._bill_completion("admin", "", http_body, http_result)
    assert (bill["prompt_tokens"], bill["completion_tokens"]) == (est_p, est_c)
    rows = _billing_store.get_usage(MONTH)
    assert len(rows) == 1
    assert rows[0]["provider_id"] == "test-provider"
    assert (rows[0]["prompt_tokens"], rows[0]["completion_tokens"]) == (est_p, est_c)


# ── Response cache keeps reported zeros and bills once ──────────────────────


@pytest.mark.parametrize("second_stream", [False, True], ids=["json", "sse"])
async def test_cached_reported_zero_is_kept_and_billed_once(
    client, fake_cli, _billing_store, monkeypatch, second_stream
):
    monkeypatch.setenv("PLUGBOARD_SEMANTIC_CACHE", "0")
    fake_cli.set(
        _agy_stdout(
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            response=_LONG_TEXT,
        )
    )
    # Unique prompt: the real response cache never matches another test.
    body = {
        "model": f"{AGY_ID}:{AGY_MODEL}",
        "messages": _messages(f"cache zero probe {uuid.uuid4().hex}"),
    }
    first = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**body, "stream": False}
    )
    _, usage = _answer(first, False)
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (0, 0)
    assert usage["total_tokens"] == 0

    second = await client.post(
        "/v1/chat/completions", headers=AUTH, json={**body, "stream": second_stream}
    )
    assert second.headers.get("X-Cache") == "HIT"
    content, usage = _answer(second, second_stream)
    assert content.strip() == _LONG_TEXT
    assert (usage["prompt_tokens"], usage["completion_tokens"]) == (0, 0)
    assert usage["total_tokens"] == 0
    assert cli_subprocess.USAGE_REPORTED_KEY not in second.text
    assert len(fake_cli.calls) == 1
    row = _single_row(_billing_store, AGY_ID, AGY_MODEL)
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (
        0,
        0,
        0,
    )


# ── Real agy 1.4.4 sample and invalid details ───────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_http_agy_real_cached_sample(client, fake_cli, _billing_store, stream):
    usage = {
        "input_tokens": 6458,
        "output_tokens": 5,
        "thinking_tokens": 0,
        "cache_read_tokens": 5091,
        "total_tokens": 6463,
    }
    fake_cli.set(_agy_stdout(usage=usage))
    res = await _call(client, f"{AGY_ID}:{AGY_MODEL}", "real sample probe", stream)
    _, got = _answer(res, stream)
    assert (got["prompt_tokens"], got["completion_tokens"]) == (6458, 5)
    assert got["total_tokens"] == 6463
    assert got["prompt_tokens_details"] == {"cached_tokens": 5091}
    assert got["completion_tokens_details"] == {"reasoning_tokens": 0}
    row = _single_row(_billing_store, AGY_ID, AGY_MODEL)
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (
        6458,
        5,
        6463,
    )
    assert row["cost_usd"] == pytest.approx(_expected_cost(6458, 5))


@pytest.mark.parametrize(
    "details",
    [
        {"cache_read_tokens": "5091", "thinking_tokens": None},
        {"cache_read_tokens": -1, "thinking_tokens": 1.5},
        {"cache_read_tokens": True, "thinking_tokens": False},
        {"cache_read_tokens": 99999, "thinking_tokens": 99},
    ],
    ids=["string-null", "negative-fraction", "bool", "above-parent"],
)
async def test_http_agy_invalid_details_ignored(
    client, fake_cli, _billing_store, details
):
    usage = {"input_tokens": 6458, "output_tokens": 5, "total_tokens": 6463, **details}
    fake_cli.set(_agy_stdout(usage=usage))
    res = await _call(client, f"{AGY_ID}:{AGY_MODEL}", "invalid details probe", False)
    _, got = _answer(res, False)
    assert (got["prompt_tokens"], got["completion_tokens"]) == (6458, 5)
    assert got["total_tokens"] == 6463
    assert "prompt_tokens_details" not in got
    assert "completion_tokens_details" not in got
    row = _single_row(_billing_store, AGY_ID, AGY_MODEL)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (6458, 5)
