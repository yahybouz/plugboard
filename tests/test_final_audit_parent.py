"""Parent oracles V9: final double-audit findings before commit (registre #137).

Audit 2 NO-GO: M1 (medium, new) case/host bypass of the Ollama local-zero rule.
Low findings fixed with it: decision prices leaking into chat by suffix (A1-F1),
Jev calls answered 200 but rejected not billed (A1-F2 / A2-F1), unrecorded usage
spending without bound (A2-F2), network MCP process keeping gateway secrets
(A2-F5), unreadable MFA file silently disabling MFA (A1-F4), non-string chat
model -> 500 (A1-F5). Offline: fake aiohttp, tmp grids, synthetic keys.
"""

import asyncio
import importlib.util
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from plugboard import billing

W = Path(__file__).resolve().parents[1]
MONTH = "2026_10"
LOCAL = {"id": "ollama", "base_url": "http://localhost:11434", "provider_type": "local"}
LOOPBACK = {
    "id": "ollama",
    "base_url": "http://127.0.0.1:11434",
    "provider_type": "local",
}
OVH_BRIDGE = {
    "id": "ollama",
    "base_url": "http://172.30.114.1:11434",
    "provider_type": "local",
}
REMOTE_NAMED_OLLAMA = {
    "id": "ollama-cloud-direct",
    "base_url": "https://ollama.com",
    "provider_type": "cloud",
}
PUBLIC_11434 = {
    "id": "acme",
    "base_url": "https://llm.acme.example:11434/v1",
    "provider_type": "cloud",
}
PAID = {
    "id": "acme",
    "base_url": "https://api.acme.example/v1",
    "provider_type": "cloud",
}


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
        "laya": {"input_price": 0.0, "output_price": 0.0, "source": "official"},
        "laya-multilingual": {
            "input_price": 0.0,
            "output_price": 0.0,
            "source": "official",
        },
        "jev": {"input_price": 0.042, "output_price": 0.0, "source": "official"},
    }
    billing.save_pricing(MONTH, {"models": models})
    return models


def _free(match):
    return (
        match is not None
        and not match.entry.get("input_price")
        and not match.entry.get("output_price")
    )


# --- M1: casefold and host bypass of the local-zero rule -------------------


@pytest.mark.parametrize(
    "model",
    ["gpt-oss:20b-CLOUD", "GPT-OSS:20B-Cloud", "gemma4:Cloud", "gpt-oss:20b-cloud "],
)
def test_cloud_suffix_is_case_and_space_insensitive(grid, model):
    match = billing.resolve_price(model, MONTH, provider=LOCAL)
    assert not _free(match), f"{model!r} billed free as local"


@pytest.mark.parametrize("provider", [REMOTE_NAMED_OLLAMA, PUBLIC_11434])
@pytest.mark.parametrize("model", ["llama3", "qwen3:8b"])
def test_local_zero_requires_a_loopback_or_private_host(grid, provider, model):
    match = billing.resolve_price(model, MONTH, provider=provider)
    assert not _free(match), f"{model} free on {provider['base_url']}"


@pytest.mark.parametrize("provider", [LOCAL, LOOPBACK, OVH_BRIDGE])
def test_local_ollama_stays_free_on_loopback_and_private_bridge(grid, provider):
    assert _free(billing.resolve_price("llama3", MONTH, provider=provider))


# --- A1-F1: decision engine prices never price chat models ----------------


@pytest.mark.parametrize(
    "model", ["acme-laya", "myhost:acme-jev", "acme-laya-multilingual", "laya", "jev"]
)
def test_decision_prices_do_not_apply_to_chat_models(grid, model):
    match = billing.resolve_price(model, MONTH, provider=PAID)
    assert billing.budget_price_unknown(model, match), (
        f"{model} priced from a decision engine entry"
    )


# --- A1-F2 / A2-F1: Jev answered 200 then rejected is still billed ---------


class _Content:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, n):
        for i in range(0, len(self.data), n):
            yield self.data[i : i + n]


class _Resp:
    def __init__(self, status, data):
        self.status, self.content = status, _Content(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake_jev(monkeypatch, behaviour):
    from plugboard import decisions

    class Sess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, **k):
            return behaviour()

    monkeypatch.setattr(decisions.aiohttp, "ClientSession", Sess)
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")


def _rows(key_id):
    from plugboard import usage

    return usage.get_usage_by_key(billing.current_pricing_month(), key_id)


@pytest.mark.parametrize(
    ("body", "tokens"),
    [
        (
            b'{"answers": {"q": {"type": "bogus"}}, "usage": {"input_tokens": 1000, "output_tokens": 5}}',
            1000,
        ),
        (b'{"answers": "not-an-object"}', 64_000),
        (b"not json at all", 64_000),
    ],
)
async def test_jev_200_rejected_is_billed_and_counted(
    client, monkeypatch, tmp_path, body, tokens
):
    from tests.test_decision_api import REQUEST
    from tests.test_pricing_decision_parent import _key

    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    _fake_jev(monkeypatch, lambda: _Resp(200, body))
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 502, response.text
    rows = _rows(rec["key_id"])
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == tokens
    assert rows[0]["cost_usd"] == pytest.approx(tokens * 0.042 / 1_000_000)


async def test_jev_timeout_after_send_is_billed_at_ceiling(
    client, monkeypatch, tmp_path
):
    from tests.test_decision_api import REQUEST
    from tests.test_pricing_decision_parent import _key

    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )

    def hang():
        raise asyncio.TimeoutError()

    _fake_jev(monkeypatch, hang)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 504
    rows = _rows(rec["key_id"])
    assert len(rows) == 1 and rows[0]["prompt_tokens"] == 64_000


async def test_jev_upstream_error_status_is_not_billed(client, monkeypatch, tmp_path):
    from tests.test_decision_api import REQUEST
    from tests.test_pricing_decision_parent import _key

    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    _fake_jev(monkeypatch, lambda: _Resp(500, b'{"error": "down"}'))
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code in (502, 503)
    assert _rows(rec["key_id"]) == []


# --- A2-F2: usage store down => budgeted keys do not spend without bound ----


async def test_unrecorded_usage_trips_a_breaker_for_budgeted_keys(
    client, monkeypatch, tmp_path
):
    from plugboard import decision_api, usage
    from tests.test_decision_api import ANSWERS, REQUEST
    from tests.test_pricing_decision_parent import _key

    headers, _ = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=0.0001, allowed_decision_models=["jev"]
    )
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")
    calls = []

    async def engine(model, payload):
        calls.append(model)
        return {**ANSWERS, "usage": {"input_tokens": 2**20, "output_tokens": 1}}

    monkeypatch.setattr(decision_api, "dispatch", engine)

    def boom(**kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(usage, "append_usage", boom)
    statuses = [
        (await client.post("/v1/decisions", headers=headers, json=REQUEST)).status_code
        for _ in range(5)
    ]
    assert statuses.count(200) <= 1, statuses
    assert len(calls) <= 1, calls
    assert all(s in (200, 503) for s in statuses), statuses


# --- A2-F5: network MCP keeps no gateway secret in its environment ----------


def test_network_mcp_environment_holds_no_gateway_secret(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP

    tree = tmp_path / "tree"
    shutil.copytree(
        W / "plugboard",
        tree / "plugboard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(W / "scripts/run_mcp.py", tree / "scripts/run_mcp.py")
    secrets = {
        "ADMIN_KEY": "synthetic-admin-0123456789abcdef0123",
        "PLUGBOARD_ADMIN_KEY": "synthetic-padmin-0123456789abcdef01",
        "ENCRYPTION_KEY": "synthetic-encryption-0123456789abcd",
        "PLUGBOARD_JEV_API_KEY": "synthetic-jev-0123456789abcdef0123",
    }
    (tree / ".env").write_text("".join(f"{k}={v}\n" for k, v in secrets.items()))
    saved = {
        k: v
        for k, v in sys.modules.items()
        if k == "plugboard" or k.startswith("plugboard.")
    }
    for k in saved:
        del sys.modules[k]
    monkeypatch.delenv("PLUGBOARD_SKIP_DOTENV", raising=False)
    for k in secrets:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PLUGBOARD_MCP_TOKEN", "T" * 40)
    monkeypatch.setenv("PLUGBOARD_API_KEY", "synthetic-client-key-for-mcp-0123")
    monkeypatch.chdir(tree)
    monkeypatch.setattr(sys, "argv", ["run_mcp.py", "--sse"])
    seen = {}

    def fake_run(self, transport="stdio", mount_path=None):
        seen["env"] = {k: os.environ.get(k) for k in secrets}
        seen["api"] = os.environ.get("PLUGBOARD_API_KEY")
        seen["file"] = sys.modules["plugboard.mcp_server"].__file__

    monkeypatch.setattr(FastMCP, "run", fake_run)
    old_path = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            "v9_run_mcp_copy", tree / "scripts/run_mcp.py"
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
        for k in secrets:
            os.environ.pop(k, None)
    assert str(tree) in seen["file"]
    assert seen["env"] == {k: None for k in secrets}, sorted(
        k for k, v in seen["env"].items() if v
    )
    assert seen["api"]  # the client key the MCP tools need is kept


# --- A1-F4: unreadable MFA file with enforcement on fails closed -----------


async def test_unreadable_mfa_file_with_enforcement_fails_closed(
    admin_client, monkeypatch, tmp_path
):
    import importlib

    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    from plugboard import admin_mfa

    importlib.reload(admin_mfa)
    admin_mfa._MFA_FILE.parent.mkdir(parents=True, exist_ok=True)
    admin_mfa._MFA_FILE.write_text("{not json")
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    response = await admin_client.get(
        "/admin/keys", headers={"Authorization": "Bearer adminkey"}
    )
    assert response.status_code in (401, 503), response.status_code


# --- A1-F5: non-string chat model is a client error, not a 500 -------------


@pytest.mark.parametrize("model", [123, None, ["a"], {"x": 1}])
async def test_non_string_chat_model_is_rejected_cleanly(client, model):
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert 400 <= response.status_code < 500, response.status_code
