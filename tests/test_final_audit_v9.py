"""V9: fixes from the final double audit (M1, A1-F1, A1-F2/A2-F1, A2-F2, A2-F5,
A1-F4, A1-F5, A2-F3). Offline: fake aiohttp, temporary grids and stores,
synthetic keys; the MCP launcher runs on a copy of the tree."""

import asyncio
import importlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import aiohttp
import pytest

from plugboard import billing
from tests.test_decision_api import ANSWERS, REQUEST
from tests.test_pricing_decision_parent import _key

W = Path(__file__).resolve().parents[1]
MONTH = "2026_10"
PAID = {"id": "acme", "base_url": "https://api.acme.example/v1"}
OVH_BRIDGE = "http://172.30.114.1:11434"


def _ollama(base_url, ident="ollama"):
    return {"id": ident, "base_url": base_url, "provider_type": "local"}


@pytest.fixture
def grid(tmp_path, monkeypatch):
    pricing = tmp_path / "pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    models = {
        "llama3": {"input_price": 0.0, "output_price": 0.0, "source": "known"},
        "gpt-oss:20b-cloud": {
            "input_price": 0.075,
            "output_price": 0.3,
            "source": "official",
        },
        "jev": {"input_price": 0.042, "output_price": 0.0, "source": "official"},
        "laya": {"input_price": 0.0, "output_price": 0.0, "source": "official"},
        "laya-multilingual": {
            "input_price": 0.0,
            "output_price": 0.0,
            "source": "official",
        },
    }
    billing.save_pricing(MONTH, {"models": models})
    return pricing


def _price(match):
    if match is None:
        return None
    return match.entry.get("input_price"), match.entry.get("output_price")


def _rows(key_id):
    from plugboard import usage

    return usage.get_usage_by_key(billing.current_pricing_month(), key_id)


# --- M1: the local zero needs a loopback or private literal host -----------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://127.20.30.40:11434",
        "http://[::1]:11434",
        "http://10.1.2.3:11434",
        "http://172.16.0.1:11434",
        "http://172.31.255.254:11434",
        OVH_BRIDGE,
        "http://192.168.1.20:11434",
        "http://[fd00::5]:11434",
        "http://[fd12:3456::1]:11434",
    ],
)
def test_local_model_is_free_on_loopback_or_private_literal_host(grid, base_url):
    match = billing.resolve_price("llama3", MONTH, provider=_ollama(base_url))
    assert _price(match) == (0.0, 0.0)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost.evil.com:11434",
        "http://127.0.0.1.nip.io:11434",
        "http://172.32.0.1:11434",
        "http://172.15.255.255:11434",
        "http://11.0.0.1:11434",
        "http://192.169.0.1:11434",
        "http://[fe80::1]:11434",
        "http://[2001:db8::1]:11434",
        "http://gpu.internal.test:11434",
        "https://ollama.com",
        "",
    ],
)
def test_local_model_is_not_free_on_any_other_host(grid, base_url):
    match = billing.resolve_price("llama3", MONTH, provider=_ollama(base_url))
    assert match is None or any(_price(match)), _price(match)


def test_local_model_is_not_free_on_a_provider_id_alone(grid):
    assert billing.resolve_price("llama3", MONTH, provider="ollama") is None


@pytest.mark.parametrize(
    "model", ["GPT-OSS:20B-Cloud", "gpt-oss:20b-CLOUD", " gpt-oss:20b-cloud\t"]
)
def test_mixed_case_cloud_name_takes_the_canonical_price(grid, model):
    match = billing.resolve_price(model, MONTH, provider=_ollama(OVH_BRIDGE))
    assert _price(match) == (0.075, 0.3)
    assert match.key == "gpt-oss:20b-cloud"
    assert not billing.budget_price_unknown(model, match)


@pytest.mark.parametrize("model", ["Gemma4:31B-CLOUD", "gemma4:Cloud", "QWEN3:CLOUD "])
def test_unpriced_mixed_case_cloud_name_is_unknown_to_a_budget(grid, model):
    match = billing.resolve_price(model, MONTH, provider=_ollama(OVH_BRIDGE))
    assert billing.budget_price_unknown(model, match), _price(match)


def _ollama_chat(tmp_path, monkeypatch):
    from plugboard import config as cfg
    from tests.conftest import _setup_providers
    from tests.factories import PROVIDER_OLLAMA

    home = tmp_path / "ollama-home"
    home.mkdir()
    pricing = tmp_path / "chat-pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    monkeypatch.setattr(cfg, "PROVIDERS_FILE", cfg.PROVIDERS_FILE)
    monkeypatch.setattr(cfg, "PROVIDERS_LOCK", cfg.PROVIDERS_LOCK)
    _setup_providers(home, monkeypatch, [PROVIDER_OLLAMA])
    billing.save_pricing(
        billing.current_pricing_month(),
        {
            "models": {
                "gpt-oss:20b-cloud": {
                    "input_price": 0.075,
                    "output_price": 0.3,
                    "source": "official",
                }
            }
        },
    )
    sent = []

    async def fake_forward(body, *args, **kwargs):
        from plugboard.router import resolve_provider_and_model

        sent.append(resolve_provider_and_model(body["model"])[1])
        return {
            "id": "chatcmpl-v9",
            "object": "chat.completion",
            "model": sent[-1],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
                "total_tokens": 2_000_000,
            },
        }

    monkeypatch.setattr("plugboard.app.forward", fake_forward)
    return sent


async def test_budgeted_chat_bills_a_mixed_case_cloud_name_sent_unchanged(
    client, monkeypatch, tmp_path
):
    sent = _ollama_chat(tmp_path, monkeypatch)
    headers, rec = await _key(monkeypatch, tmp_path, monthly_limit_usd=5.0)
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "ollama:GPT-OSS:20B-Cloud",
            "messages": [{"role": "user", "content": "hi"}],
            "cache": False,
        },
    )
    assert response.status_code == 200, response.text
    assert sent == ["GPT-OSS:20B-Cloud"]  # the upstream gets the name as received
    rows = _rows(rec["key_id"])
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == pytest.approx(0.375)


async def test_budgeted_chat_refuses_an_unpriced_mixed_case_cloud_name(
    client, monkeypatch, tmp_path
):
    sent = _ollama_chat(tmp_path, monkeypatch)
    headers, rec = await _key(monkeypatch, tmp_path, monthly_limit_usd=5.0)
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "ollama:Gemma4:31B-CLOUD",
            "messages": [{"role": "user", "content": "hi"}],
            "cache": False,
        },
    )
    assert response.status_code == 402
    assert sent == []
    assert _rows(rec["key_id"]) == []


# --- A1-F1: suffix boundaries; decision prices never price a chat model ----


TABLE = {
    "claude-sonnet-4-6": {"input_price": 3.0, "output_price": 15.0},
    "llama": {"input_price": 1.0, "output_price": 1.0},
    "gemma-7b": {"input_price": 2.0, "output_price": 2.0},
}


@pytest.mark.parametrize(
    ("model", "key"),
    [
        ("openclaw/anthropic--claude-sonnet-4-6", "claude-sonnet-4-6"),
        ("vendor/claude-sonnet-4-6", "claude-sonnet-4-6"),
        ("host:claude-sonnet-4-6", "claude-sonnet-4-6"),
        ("a/b--llama", "llama"),
        ("x/llama", "llama"),
        ("pre:llama", "llama"),
        ("vendor/gemma.7b", "gemma-7b"),
        ("vendor/claude-sonnet-4.6-20260101", "claude-sonnet-4-6"),
        ("superllama", None),
        ("my-llama", None),
        ("xgemma-7b", None),
        ("xgemma.7b", None),
        ("vendor/mega-gemma.7b", None),
    ],
)
def test_suffix_match_needs_a_boundary(model, key):
    match = billing.match_in_grid({"models": TABLE}, [], billing._candidate_keys(model))
    assert (match.key if match else None) == key


@pytest.mark.parametrize(
    "model",
    [
        "jev",
        "laya",
        "laya-multilingual",
        "vendor/laya",
        "host:jev",
        "acme--laya-multilingual",
    ],
)
def test_chat_never_takes_a_decision_engine_price(grid, model):
    assert billing.resolve_price(model, MONTH, provider=PAID) is None
    assert billing.resolve_price(model, MONTH) is None


def test_chat_never_takes_a_decision_engine_price_from_the_official_table(
    tmp_path, monkeypatch
):
    pricing = tmp_path / "empty-pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    for model in ("jev", "laya", "laya-multilingual"):
        assert billing.resolve_price(model, MONTH, provider=PAID) is None


def test_decision_pricing_still_reads_the_grid_and_the_official_table(
    tmp_path, monkeypatch
):
    from plugboard import decision_api

    pricing = tmp_path / "decision-pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    assert decision_api._price("jev") == (
        {"input_usd_per_mtok": 0.042, "output_usd_per_mtok": 0.0, "unit": "token"},
        "official",
    )
    month = billing.current_pricing_month()
    billing.save_pricing(
        month,
        {
            "models": {
                "jev": {"input_price": 0.05, "output_price": 0.0, "source": "manual"}
            }
        },
    )
    assert decision_api._price("jev") == (
        {"input_usd_per_mtok": 0.05, "output_usd_per_mtok": 0.0, "unit": "token"},
        f"grid:manual:{month}",
    )


# --- A1-F2 / A2-F1: a Jev call TypeSafe answered is billed ------------------


class _Body:
    def __init__(self, data, fail=None):
        self.data, self.fail = data, fail

    async def iter_chunked(self, n):
        for i in range(0, len(self.data), n):
            yield self.data[i : i + n]
        if self.fail is not None:
            raise self.fail


class _Resp:
    def __init__(self, status, data=b"", fail=None):
        self.status, self.content = status, _Body(data, fail)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _jev(monkeypatch, behaviour):
    from plugboard import decisions

    posts = []

    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, **k):
            posts.append(url)
            return behaviour()

    monkeypatch.setattr(decisions.aiohttp, "ClientSession", Session)
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.delenv("PLUGBOARD_JEV_PRICE_PER_CALL_USD", raising=False)
    return posts


def _valid(usage):
    return json.dumps({**ANSWERS, "usage": usage}).encode()


async def _jev_key(monkeypatch, tmp_path, limit=10.0):
    return await _key(
        monkeypatch, tmp_path, monthly_limit_usd=limit, allowed_decision_models=["jev"]
    )


async def test_rejected_200_bills_the_reported_usage(client, monkeypatch, tmp_path):
    body = json.dumps(
        {
            "answers": {"route": {"type": "noul", "noul": 0.5}},
            "usage": {"input_tokens": 1000, "output_tokens": 5},
        }
    ).encode()
    _jev(monkeypatch, lambda: _Resp(200, body))
    headers, rec = await _jev_key(monkeypatch, tmp_path)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 502
    assert response.json() == {"detail": "Invalid decision engine response"}
    rows = _rows(rec["key_id"])
    assert [(r["prompt_tokens"], r["completion_tokens"]) for r in rows] == [(1000, 5)]
    assert rows[0]["cost_usd"] == pytest.approx(1000 * 0.042 / 1_000_000)
    assert rows[0]["model"] == "decision:jev"


@pytest.mark.parametrize(
    "body",
    [
        b"x" * 70_000,
        b'{"answers": {}, "answers": {}}',
        _valid({"input_tokens": -1}).replace(b'"returns"', b'"nowhere"', 1),
    ],
)
async def test_rejected_200_without_valid_usage_bills_the_ceiling(
    client, monkeypatch, tmp_path, body
):
    _jev(monkeypatch, lambda: _Resp(200, body))
    headers, rec = await _jev_key(monkeypatch, tmp_path)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 502
    rows = _rows(rec["key_id"])
    assert [r["prompt_tokens"] for r in rows] == [64_000]
    assert rows[0]["price_source"].startswith("official-estimate:")


async def test_timeout_while_reading_a_200_bills_the_ceiling(
    client, monkeypatch, tmp_path
):
    _jev(monkeypatch, lambda: _Resp(200, b'{"answers"', fail=asyncio.TimeoutError()))
    headers, rec = await _jev_key(monkeypatch, tmp_path)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 504
    assert [r["prompt_tokens"] for r in _rows(rec["key_id"])] == [64_000]


def _raise(exc):
    def behaviour():
        raise exc

    return behaviour


@pytest.mark.parametrize(
    ("behaviour", "status"),
    [
        (_raise(aiohttp.ClientConnectionError("refused")), 502),
        (_raise(aiohttp.ConnectionTimeoutError("connect timeout")), 504),
        (lambda: _Resp(401, b'{"error": "bad key"}'), 502),
        (lambda: _Resp(429, b'{"error": "slow down"}'), 502),
    ],
)
async def test_connection_failure_or_error_status_is_not_billed(
    client, monkeypatch, tmp_path, behaviour, status
):
    _jev(monkeypatch, behaviour)
    headers, rec = await _jev_key(monkeypatch, tmp_path)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == status
    assert _rows(rec["key_id"]) == []


async def test_valid_answer_is_billed_once(client, monkeypatch, tmp_path):
    _jev(monkeypatch, lambda: _Resp(200, _valid({"input_tokens": 2000})))
    headers, rec = await _jev_key(monkeypatch, tmp_path)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    assert response.json()["billing"]["status"] == "billed"
    assert [r["prompt_tokens"] for r in _rows(rec["key_id"])] == [2000]


async def test_spent_budget_stops_jev_before_any_call(client, monkeypatch, tmp_path):
    from plugboard import usage

    posts = _jev(monkeypatch, lambda: _Resp(200, _valid({"input_tokens": 10})))
    headers, rec = await _jev_key(monkeypatch, tmp_path, limit=0.01)
    usage.append_usage(
        client_id=rec["client_id"],
        model="decision:jev",
        provider_id="decisions",
        prompt_tokens=1,
        completion_tokens=0,
        pricing_month=billing.current_pricing_month(),
        cost_usd=0.02,
        key_id=rec["key_id"],
    )
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 429
    assert posts == []


async def test_failed_laya_decision_is_not_billed(client, monkeypatch, tmp_path):
    from plugboard import decision_api
    from plugboard.decisions import DecisionUpstreamError

    async def engine(model, payload):
        raise DecisionUpstreamError()

    monkeypatch.setattr(decision_api, "dispatch", engine)
    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=1.0, allowed_decision_models=["laya"]
    )
    response = await client.post(
        "/v1/decisions", headers=headers, json={**REQUEST, "model": "laya"}
    )
    assert response.status_code == 502
    assert _rows(rec["key_id"]) == []


# --- A2-F2: usage store down => budgeted decisions stop ---------------------


def _engine(monkeypatch, calls):
    from plugboard import decision_api

    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")

    async def engine(model, payload):
        calls.append(model)
        return {**ANSWERS, "usage": {"input_tokens": 1000, "output_tokens": 0}}

    monkeypatch.setattr(decision_api, "dispatch", engine)


def _break_usage(monkeypatch):
    from plugboard import usage

    real = usage.append_usage

    def boom(**kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(usage, "append_usage", boom)
    return lambda: monkeypatch.setattr(usage, "append_usage", real)


async def test_breaker_refuses_budgeted_keys_until_a_write_succeeds(
    client, monkeypatch, tmp_path
):
    calls = []
    _engine(monkeypatch, calls)
    budgeted, _ = await _jev_key(monkeypatch, tmp_path, limit=10.0)
    free, free_rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=0, allowed_decision_models=["jev"]
    )
    repair = _break_usage(monkeypatch)

    async def post(headers):
        return await client.post("/v1/decisions", headers=headers, json=REQUEST)

    first = await post(budgeted)
    assert first.status_code == 200
    assert first.json()["billing"] == {"status": "unrecorded", "cost_usd": None}
    refused = await post(budgeted)
    assert refused.status_code == 503
    assert refused.json() == {"detail": "Decision usage recording unavailable"}
    assert len(calls) == 1
    # Keys without a budget keep the V6b behaviour: served, not recorded.
    assert (await post(free)).status_code == 200
    assert (await post(budgeted)).status_code == 503
    repair()
    assert (await post(free)).status_code == 200
    assert len(_rows(free_rec["key_id"])) == 1
    served = await post(budgeted)
    assert served.status_code == 200
    assert served.json()["billing"]["status"] == "billed"
    assert len(calls) == 4


async def test_breaker_admits_one_trial_per_retry_interval(
    client, monkeypatch, tmp_path
):
    from plugboard import decision_api

    calls = []
    _engine(monkeypatch, calls)
    budgeted, rec = await _jev_key(monkeypatch, tmp_path, limit=10.0)
    repair = _break_usage(monkeypatch)
    assert (
        await client.post("/v1/decisions", headers=budgeted, json=REQUEST)
    ).status_code == 200
    long_ago = time.monotonic() - decision_api.USAGE_RETRY_SECONDS - 1
    monkeypatch.setattr(decision_api, "_usage_down_since", long_ago)
    trial = await client.post("/v1/decisions", headers=budgeted, json=REQUEST)
    assert trial.status_code == 200 and len(calls) == 2
    after = await client.post("/v1/decisions", headers=budgeted, json=REQUEST)
    assert after.status_code == 503 and len(calls) == 2
    repair()
    monkeypatch.setattr(decision_api, "_usage_down_since", long_ago)
    assert (
        await client.post("/v1/decisions", headers=budgeted, json=REQUEST)
    ).status_code == 200
    assert (
        await client.post("/v1/decisions", headers=budgeted, json=REQUEST)
    ).status_code == 200
    assert len(_rows(rec["key_id"])) == 2


async def test_failed_jev_write_also_opens_the_breaker(client, monkeypatch, tmp_path):
    posts = _jev(monkeypatch, lambda: _Resp(200, b"not json"))
    headers, _ = await _jev_key(monkeypatch, tmp_path)
    _break_usage(monkeypatch)
    assert (
        await client.post("/v1/decisions", headers=headers, json=REQUEST)
    ).status_code == 502
    assert (
        await client.post("/v1/decisions", headers=headers, json=REQUEST)
    ).status_code == 503
    assert len(posts) == 1


# --- A2-F5: the network MCP process holds only what the MCP server needs ----


SECRETS = {
    "ADMIN_KEY": "synthetic-admin-0123456789abcdef0123",
    "PLUGBOARD_ADMIN_KEY": "synthetic-padmin-0123456789abcdef01",
    "LLM_ENABLER_ADMIN_KEY": "synthetic-legacy-0123456789abcdef01",
    "ENCRYPTION_KEY": "synthetic-encryption-0123456789abcd",
    "PLUGBOARD_JEV_API_KEY": "synthetic-jev-0123456789abcdef0123",
    "OPENAI_API_KEY": "synthetic-openai-0123456789abcdef01",
    "ANTHROPIC_API_KEY": "synthetic-anthropic-0123456789abcd",
}
KEPT = {
    "PLUGBOARD_URL": "http://127.0.0.1:8765",
    "PLUGBOARD_API_KEY": "synthetic-client-key-for-mcp-0123",
    "PLUGBOARD_MCP_TOKEN": "T" * 40,
    "PLUGBOARD_MCP_TIMEOUT": "120",
    "MCP_PORT": "8799",
    "MCP_HOST": "127.0.0.1",
}


def _run_mcp_copy(tmp_path, monkeypatch, *flags):
    from mcp.server.fastmcp import FastMCP

    tree = tmp_path / "tree"
    shutil.copytree(
        W / "plugboard",
        tree / "plugboard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(W / "scripts/run_mcp.py", tree / "scripts/run_mcp.py")
    dotenv = {k: v for k, v in SECRETS.items() if k != "ANTHROPIC_API_KEY"}
    dotenv["PLUGBOARD_MCP_TIMEOUT"] = KEPT["PLUGBOARD_MCP_TIMEOUT"]
    (tree / ".env").write_text("".join(f"{k}={v}\n" for k, v in dotenv.items()))
    saved = {
        k: v
        for k, v in sys.modules.items()
        if k == "plugboard" or k.startswith("plugboard.")
    }
    for k in saved:
        del sys.modules[k]
    monkeypatch.delenv("PLUGBOARD_SKIP_DOTENV", raising=False)
    for k in [*SECRETS, *KEPT]:
        monkeypatch.delenv(k, raising=False)
    for k, v in KEPT.items():
        if k != "PLUGBOARD_MCP_TIMEOUT":
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRETS["ANTHROPIC_API_KEY"])
    monkeypatch.chdir(tree)
    monkeypatch.setattr(sys, "argv", ["run_mcp.py", *flags])
    seen = {}

    def fake_run(self, transport="stdio", mount_path=None):
        ms = sys.modules["plugboard.mcp_server"]
        seen.update(
            env=dict(os.environ),
            admin=ms._ADMIN_KEY,
            file=ms.__file__,
            transport=transport,
        )

    monkeypatch.setattr(FastMCP, "run", fake_run)
    old_path = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            "v9_producer_run_mcp", tree / "scripts/run_mcp.py"
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
    assert str(tree) in seen["file"]
    return seen


@pytest.mark.parametrize("flag", ["--sse", "--http"])
def test_network_mcp_keeps_only_its_own_variables(tmp_path, monkeypatch, flag):
    seen = _run_mcp_copy(tmp_path, monkeypatch, flag)
    env = seen["env"]
    assert sorted(k for k in SECRETS if k in env) == []
    assert seen["admin"] == ""
    assert {k: env.get(k) for k in KEPT} == KEPT
    assert env.get("PATH") and env.get("HOME")
    assert env.get("PLUGBOARD_DATA")


def test_stdio_mcp_environment_is_unchanged(tmp_path, monkeypatch):
    seen = _run_mcp_copy(tmp_path, monkeypatch)
    assert seen["transport"] == "stdio"
    assert seen["env"]["ADMIN_KEY"] == SECRETS["ADMIN_KEY"]
    assert seen["env"]["ANTHROPIC_API_KEY"] == SECRETS["ANTHROPIC_API_KEY"]
    assert seen["admin"] == SECRETS["PLUGBOARD_ADMIN_KEY"]


# --- A1-F4: MFA required and its store unreadable => fail closed ------------


@pytest.fixture
def mfa_store(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    from plugboard import admin_mfa

    importlib.reload(admin_mfa)
    admin_mfa._MFA_FILE.parent.mkdir(parents=True, exist_ok=True)
    yield admin_mfa._MFA_FILE
    monkeypatch.undo()
    importlib.reload(admin_mfa)


ADMIN = {"Authorization": "Bearer adminkey"}


@pytest.mark.parametrize("content", ["{CANARY-SECRET", "[]", '"text"', "\udcff"])
async def test_unreadable_mfa_store_refuses_admin_when_required(
    admin_client, mfa_store, monkeypatch, caplog, content
):
    mfa_store.write_text(content, errors="surrogateescape")
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    response = await admin_client.get("/admin/keys", headers=ADMIN)
    assert response.status_code in (401, 503)
    login = await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    assert login.status_code == 401
    assert "set-cookie" not in login.headers
    assert "CANARY" not in caplog.text
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_unreadable_mfa_store_without_the_flag_changes_nothing(
    admin_client, mfa_store, monkeypatch
):
    mfa_store.write_text("{not json")
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    assert (await admin_client.get("/admin/keys", headers=ADMIN)).status_code == 200


async def test_missing_mfa_store_with_the_flag_still_lets_the_admin_enrol(
    admin_client, mfa_store, monkeypatch
):
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert not mfa_store.exists()
    assert (await admin_client.get("/admin/keys", headers=ADMIN)).status_code == 200


# --- A1-F5: a non-string chat model is a client error ----------------------


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize("model", [123, None, ["a"], {"x": 1}, True, 1.5])
async def test_non_string_model_is_a_400(client, path, model):
    response = await client.post(
        path,
        headers={"Authorization": "Bearer testkey"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": "model must be a string"}


@pytest.mark.parametrize("body", [[], "text", 3])
async def test_non_object_chat_body_is_a_400(client, body):
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json=body,
    )
    assert response.status_code == 400, response.text


@pytest.mark.parametrize("model", [123, None, ["a"]])
def test_router_refuses_a_non_string_model(model):
    from plugboard.router import resolve_provider_and_model

    with pytest.raises(ValueError, match="model must be a string"):
        resolve_provider_and_model(model)


# --- A2-F3: a refresh in flight never resurrects a changed credential -------


@pytest.fixture
def oauth_db(tmp_path, monkeypatch):
    from plugboard import cli_auth, oauth_manager, oauth_store

    data = tmp_path / "oauth"
    data.mkdir()
    monkeypatch.setattr(oauth_store, "_DATA_DIR", data)
    monkeypatch.setattr(oauth_store, "_DB_PATH", data / "credentials.db")
    monkeypatch.setattr(
        oauth_manager,
        "get_config",
        lambda source: {"client_id": "cid", "token_url": "https://token.invalid/"},
    )
    cli_auth.reset_loaders()
    yield oauth_store
    cli_auth.reset_loaders()


def _gated_refresh(monkeypatch):
    from plugboard import oauth_manager

    gate = asyncio.Event()
    posts = []

    class Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            await gate.wait()
            return {"access_token": "REFRESHED-OLD-ACCOUNT", "expires_in": 3600}

        async def text(self):
            return ""

    class Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, *a, **k):
            posts.append(a)
            return Resp()

    monkeypatch.setattr(oauth_manager.aiohttp, "ClientSession", Session)
    return gate, posts


def _expired(store, account):
    store.save_credential(
        store.OAuthCredential(
            source="google",
            account=account,
            access_token=f"OLD-{account}",
            refresh_token=f"RT-{account}",
            expires_at=time.time() - 10,
        )
    )


async def test_disconnect_during_refresh_is_not_undone(oauth_db, monkeypatch):
    from plugboard import cli_auth

    gate, posts = _gated_refresh(monkeypatch)
    _expired(oauth_db, "first@example.test")
    loader = cli_auth.get_loader("broker:google")
    task = asyncio.create_task(loader.get_token())
    for _ in range(50):
        if posts:
            break
        await asyncio.sleep(0.01)
    assert posts, "refresh never started"
    assert oauth_db.delete_credential("google")
    gate.set()
    assert await task is None
    assert oauth_db.get_credential("google") is None
    assert await loader.get_token() is None


async def test_reconnect_during_refresh_keeps_the_new_account(oauth_db, monkeypatch):
    from plugboard import cli_auth

    gate, posts = _gated_refresh(monkeypatch)
    _expired(oauth_db, "first@example.test")
    loader = cli_auth.get_loader("broker:google")
    task = asyncio.create_task(loader.get_token())
    for _ in range(50):
        if posts:
            break
        await asyncio.sleep(0.01)
    oauth_db.save_credential(
        oauth_db.OAuthCredential(
            source="google",
            account="second@example.test",
            access_token="SECOND-TOKEN",
            refresh_token="RT-second",
            expires_at=time.time() + 3600,
        )
    )
    gate.set()
    token = await task
    assert token is None or token.access_token == "SECOND-TOKEN"
    stored = oauth_db.get_credential("google")
    assert (stored.account, stored.access_token) == (
        "second@example.test",
        "SECOND-TOKEN",
    )
    assert (await loader.get_token()).access_token == "SECOND-TOKEN"


async def test_refresh_without_concurrent_change_is_saved(oauth_db, monkeypatch):
    from plugboard import cli_auth

    gate, _ = _gated_refresh(monkeypatch)
    gate.set()
    _expired(oauth_db, "first@example.test")
    token = await cli_auth.get_loader("broker:google").get_token()
    assert token.access_token == "REFRESHED-OLD-ACCOUNT"
    assert oauth_db.get_credential("google").access_token == "REFRESHED-OLD-ACCOUNT"
