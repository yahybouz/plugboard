"""V6 counter-tests for the four inherited runtime flaws.

Complements tests/test_inherited_runtime_parent.py (whose fixtures are reused):
the parent proves each defect, these tests pin the edges of the fix and the
behaviour that must not change. No network, no real subprocess.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal

import pytest

from plugboard import cli_subprocess
from plugboard.cli_subprocess import CliSubprocessError
from tests import test_inherited_runtime_parent as _parent
from tests.factories import PROVIDER_A, PROVIDER_B, PROVIDER_OLLAMA
from tests.test_inherited_runtime_parent import (
    _KNOWN_PRICE,
    _MONTH,
    _OK_STDOUT,
    _PID,
    _FakeProc,
    _auth,
    _body,
    _new_key,
    _set_price,
)

# The parent's fixtures, made available to this module under the same names.
cli_env = _parent.cli_env
fake_forward = _parent.fake_forward
keys_env = _parent.keys_env

_CLOSED_CODES = {
    "unknown_model",
    "forbidden_model",
    "budget_exceeded",
    "price_unavailable",
    "rate_limited",
    "upstream_error",
}


def _items(*pairs):
    return {"items": [{"provider": p, "model": m} for p, m in pairs]}


_A = ("test-provider", "fake:model-v1")
_B = ("other-provider", "other:model-v2")


# ── #1 /v1/preflight ─────────────────────────────────────────────────────────


async def test_preflight_ten_items_accepted(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "pf-ten")
    resp = await client.post(
        "/v1/preflight", headers=_auth(rec), json=_items(*[_A] * 10)
    )
    assert resp.status_code == 200, resp.text[:200]
    assert [r["ok"] for r in resp.json()["results"]] == [True] * 10
    assert len(fake_forward["calls"]) == 10


async def test_preflight_contract_bounds_items_and_closes_errors():
    from plugboard.api_contract import build_openapi

    schemas = build_openapi()["components"]["schemas"]
    assert schemas["PreflightRequest"]["properties"]["items"]["maxItems"] == 10
    error = json.dumps(schemas["PreflightResult"]["properties"]["error"])
    for code in _CLOSED_CODES:
        assert code in error


async def test_preflight_errors_are_closed_codes(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, _KNOWN_PRICE)
    fake_forward["exc"] = RuntimeError("upstream 503: body=SYNTH-BODY-77a1")
    rec = _new_key(keys_env, "pf-codes", allowed_models=[_A[1]])
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(_A, _B))
    assert resp.status_code == 200, resp.text[:200]
    results = resp.json()["results"]
    assert [(r["ok"], r["error"]) for r in results] == [
        (False, "upstream_error"),
        (False, "forbidden_model"),
    ]
    assert "SYNTH-BODY-77a1" not in resp.text
    assert len(fake_forward["calls"]) == 1


async def test_preflight_budget_error_code(client, keys_env, fake_forward, monkeypatch):
    import plugboard.usage as u

    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "pf-budget-code", monthly_limit_usd=1.0)
    u.append_usage(
        client_id="pf-budget-code",
        model="fake:model-v1",
        provider_id="test-provider",
        prompt_tokens=1,
        completion_tokens=1,
        pricing_month=_MONTH,
        cost_usd=2.0,
        key_id=rec["key_id"],
    )
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(_A))
    assert resp.json()["results"][0]["error"] == "budget_exceeded"
    assert fake_forward["calls"] == []


async def test_preflight_unpriced_model_refused_for_budgeted_key(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, None)
    rec = _new_key(keys_env, "pf-unpriced", monthly_limit_usd=5.0)
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(_A))
    assert resp.json()["results"][0]["error"] == "price_unavailable"
    assert fake_forward["calls"] == []


async def test_preflight_rpm_counts_each_item(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "pf-rpm", rpm_limit=2)
    resp = await client.post(
        "/v1/preflight", headers=_auth(rec), json=_items(_A, _A, _A)
    )
    results = resp.json()["results"]
    assert [r.get("error") for r in results] == [None, None, "rate_limited"]
    assert len(fake_forward["calls"]) == 2


async def test_preflight_respects_inflight_cap(
    client, keys_env, fake_forward, monkeypatch
):
    import plugboard.app as app_mod

    _set_price(monkeypatch, _KNOWN_PRICE)
    monkeypatch.setattr(app_mod, "_INFLIGHT_CHAT_MAX", 1)
    monkeypatch.setattr(app_mod, "_INFLIGHT_WAIT_S", 0.0)
    monkeypatch.setattr(app_mod, "_inflight_chat_sem", None)
    rec = _new_key(keys_env, "pf-inflight")
    sem = app_mod._get_inflight_sem()
    await sem.acquire()
    try:
        resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(_A))
        assert resp.status_code == 503
        assert fake_forward["calls"] == []
    finally:
        sem.release()
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(_A))
    assert resp.status_code == 200 and resp.json()["results"][0]["ok"] is True


async def test_preflight_bills_the_ping(client, keys_env, fake_forward, monkeypatch):
    import plugboard.usage as u

    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "pf-bill", monthly_limit_usd=5.0)
    resp = await client.post("/v1/preflight", headers=_auth(rec), json=_items(_A))
    assert resp.json()["results"][0]["ok"] is True
    # FAKE_COMPLETION usage: 5 prompt * $1/M + 2 completion * $2/M
    assert u.get_total_cost_by_key(_MONTH, rec["key_id"]) == pytest.approx(9e-06)


# ── chat unchanged for an allowed, priced key ───────────────────────────────


def _chat(model="test-provider:fake:model-v1", **extra):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "cache": False,
        **extra,
    }


@pytest.fixture
def fake_stream(monkeypatch):
    state = {"calls": []}

    async def _stream(body, *args, **kwargs):
        state["calls"].append(body)
        yield b'data: {"choices":[{"delta":{"content":"Hi"},"index":0}]}\n\n'
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr("plugboard.app.forward_stream", _stream)
    return state


async def test_chat_stream_priced_budgeted_key_unchanged(
    client, keys_env, fake_forward, fake_stream, monkeypatch
):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "chat-stream-ok", monthly_limit_usd=5.0)
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat(stream=True)
    )
    assert resp.status_code == 200, resp.text[:200]
    assert '"content":"Hi"' in resp.text and "[DONE]" in resp.text
    assert len(fake_stream["calls"]) == 1


async def test_chat_stream_unpriced_model_refused_for_budgeted_key(
    client, keys_env, fake_forward, fake_stream, monkeypatch
):
    _set_price(monkeypatch, None)
    rec = _new_key(keys_env, "chat-stream-402", monthly_limit_usd=5.0)
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat(stream=True)
    )
    assert resp.status_code == 402
    assert fake_stream["calls"] == [] and fake_forward["calls"] == []


async def test_unpriced_refusal_message_is_static_and_logged(
    client, keys_env, fake_forward, monkeypatch, caplog
):
    _set_price(monkeypatch, None)
    rec = _new_key(keys_env, "chat-static", monthly_limit_usd=5.0)
    with caplog.at_level(logging.WARNING, logger="plugboard.app"):
        a = await client.post("/v1/chat/completions", headers=_auth(rec), json=_chat())
        b = await client.post(
            "/v1/chat/completions",
            headers=_auth(rec),
            json=_chat(model="other-provider:other:model-v2"),
        )
    from plugboard.api_contract import ErrorResponse

    assert a.status_code == b.status_code == 402
    assert a.json() == b.json()
    ErrorResponse.model_validate(a.json())
    assert "fake:model-v1" not in a.text and rec["key_id"] not in a.text
    refusals = [
        r.getMessage() for r in caplog.records if "fake:model-v1" in r.getMessage()
    ]
    assert refusals and all("Hi" not in m for m in refusals)


async def test_unpriced_fallback_route_refused_for_budgeted_key(
    client, keys_env, fake_forward, monkeypatch
):
    """A bare model id may fall back to every active provider: all are checked."""
    import plugboard.billing as b
    from plugboard.router import _resolve_all_providers

    routes = [p["id"] for p, _m in _resolve_all_providers("shared-model-x")]
    # Order follows provider load metrics left by earlier tests: compare sets.
    assert sorted(routes) == ["other-provider", "test-provider"], routes
    match = b.PriceMatch(dict(_KNOWN_PRICE), "shared-model-x", "models")
    monkeypatch.setattr(
        b,
        "resolve_price",
        lambda m, month=None, provider=None, **k: (
            None if (provider or {}).get("id") == "other-provider" else match
        ),
    )
    rec = _new_key(keys_env, "chat-fallback", monthly_limit_usd=5.0)
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat(model="shared-model-x")
    )
    assert resp.status_code == 402
    assert fake_forward["calls"] == []


# ── #2 inventory: real resolve_price on an empty grid ───────────────────────

_CLI_CLAUDE = {
    "id": "claude-cli-subprocess",
    "name": "Claude (CLI)",
    "base_url": "subprocess://claude",
    "bearer_token": "",
    "preset_key": "claude-cli-subprocess",
    "auth_source": "subprocess:claude",
    "default_model": "claude-haiku-4-5",
    "models": [{"id": "claude-haiku-4-5"}, {"id": "claude-unpriced-9-9"}],
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture
def real_prices(tmp_path, monkeypatch):
    """Real billing.resolve_price over an empty (or given) grid directory."""
    import plugboard.billing as b
    import plugboard.config as cfg
    from plugboard.router import invalidate_models_cache

    pricing = tmp_path / "pricing-v6"
    pricing.mkdir()
    monkeypatch.setattr(b, "_PRICING_DIR", pricing)
    cfg.PROVIDERS_FILE.write_text(
        json.dumps(
            {"providers": [PROVIDER_A, PROVIDER_B, PROVIDER_OLLAMA, _CLI_CLAUDE]}
        )
    )
    invalidate_models_cache()
    return pricing


@pytest.mark.parametrize(
    ("model", "budget", "status"),
    [
        ("ollama:llama3", 5.0, 200),  # local Ollama: explicit 0 ("local")
        ("ollama:gpt-oss:20b-cloud", 5.0, 200),  # cloud, priced (built-in)
        ("ollama:gemma4:31b-cloud", 5.0, 402),  # cloud, only the local 0
        ("ollama:brandnew9:cloud", 5.0, 402),  # cloud, no price at all
        ("ollama:gemma4:31b-cloud", 0.0, 200),  # no budget: unchanged
        ("claude-cli-subprocess:claude-haiku-4-5", 5.0, 200),  # priced (built-in)
        # Subscription model without an official price (V6b priced claude-fable-5-1).
        ("claude-cli-subprocess:claude-unpriced-9-9", 5.0, 402),
        ("claude-cli-subprocess:claude-unpriced-9-9", 0.0, 200),
    ],
)
async def test_price_classes_for_budgeted_keys(
    client, keys_env, fake_forward, real_prices, model, budget, status
):
    rec = _new_key(
        keys_env, f"inv-{abs(hash((model, budget)))}", monthly_limit_usd=budget
    )
    resp = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat(model=model)
    )
    assert resp.status_code == status, resp.text[:200]
    assert len(fake_forward["calls"]) == (1 if status == 200 else 0)


async def test_cloud_model_explicit_zero_grid_price_allowed(
    client, keys_env, fake_forward, real_prices
):
    grid = {
        "models": {},
        "models_by_provider": {
            "ollama": {
                "gemma4:31b-cloud": {
                    "input_price": 0.0,
                    "output_price": 0.0,
                    "source": "external",
                }
            }
        },
    }
    (real_prices / f"{_MONTH}.json").write_text(json.dumps(grid))
    rec = _new_key(keys_env, "inv-explicit-zero", monthly_limit_usd=5.0)
    resp = await client.post(
        "/v1/chat/completions",
        headers=_auth(rec),
        json=_chat(model="ollama:gemma4:31b-cloud"),
    )
    assert resp.status_code == 200, resp.text[:200]


# ── #3 CLI process lifecycle ─────────────────────────────────────────────────


class _BoomProc(_FakeProc):
    async def communicate(self, input=None):
        self.entered.set()
        raise RuntimeError("synthetic pipe failure")


def _kill_wait_release(events):
    kills = [
        i for i, e in enumerate(events) if isinstance(e, tuple) and e[0] == "killpg"
    ]
    assert kills and events[kills[0]][1:] == (_PID, signal.SIGKILL), events
    assert "wait" in events and kills[0] < events.index("wait"), events
    assert events.index("wait") < events.index("sem_release"), events


@pytest.mark.parametrize("kind", ["claude", "codex", "gemini", "agy"])
async def test_complete_generic_exception_kills_group_and_reraises(cli_env, kind):
    cli_env["proc"] = _BoomProc(cli_env["events"])
    with pytest.raises(RuntimeError, match="synthetic pipe failure"):
        await cli_subprocess.complete(kind, _body(kind))
    _kill_wait_release(cli_env["events"])


async def test_complete_tolerates_vanished_group(cli_env, monkeypatch):
    events = cli_env["events"]

    def gone(pgid, sig):
        events.append(("killpg", pgid, sig))
        raise ProcessLookupError(pgid)

    monkeypatch.setattr(os, "killpg", gone)
    cli_env["proc"] = _FakeProc(events, hang=True)
    with pytest.raises(CliSubprocessError):
        await cli_subprocess.complete("claude", _body("claude"), timeout_s=0.05)
    _kill_wait_release(events)


@pytest.mark.parametrize("kind", ["codex", "agy"])
async def test_complete_cancel_reraised_after_reaping(cli_env, kind):
    proc = _FakeProc(cli_env["events"], hang=True)
    cli_env["proc"] = proc
    task = asyncio.ensure_future(
        cli_subprocess.complete(kind, _body(kind), timeout_s=30)
    )
    await asyncio.wait_for(proc.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _kill_wait_release(cli_env["events"])


async def test_complete_semaphore_slot_returned_after_failure(cli_env):
    cli_env["proc"] = _BoomProc(cli_env["events"])
    with pytest.raises(RuntimeError):
        await cli_subprocess.complete("claude", _body("claude"))
    sem = cli_subprocess._get_semaphore("claude")
    assert sem._value == 4  # the recording semaphore was created with 4 slots


@pytest.mark.parametrize("kind", ["codex", "agy"])
async def test_complete_normal_run_never_kills(cli_env, kind):
    cli_env["proc"] = _FakeProc(cli_env["events"], stdout=_OK_STDOUT[kind])
    result = await cli_subprocess.complete(kind, _body(kind))
    assert result["choices"][0]["message"]["content"] == "OK"
    assert not any(isinstance(e, tuple) and e[0] == "killpg" for e in cli_env["events"])


async def test_list_agy_models_cancel_reraised_after_reaping(cli_env):
    proc = _FakeProc(cli_env["events"], hang=True)
    cli_env["proc"] = proc
    task = asyncio.ensure_future(cli_subprocess.list_agy_models(timeout_s=30))
    await asyncio.wait_for(proc.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events = cli_env["events"]
    kills = [e for e in events if isinstance(e, tuple) and e[0] == "killpg"]
    assert kills and "wait" in events and events.index(kills[0]) < events.index("wait")


async def test_list_agy_models_normal_run_never_kills(cli_env):
    cli_env["proc"] = _FakeProc(cli_env["events"], stdout=b"m-1\tModel one\n")
    assert await cli_subprocess.list_agy_models() == ["m-1"]
    assert not any(isinstance(e, tuple) and e[0] == "killpg" for e in cli_env["events"])


# ── #4 spawn log: kind, model, prompt length; never argv ─────────────────────


@pytest.mark.parametrize("kind", ["claude", "codex", "gemini", "agy"])
async def test_spawn_log_names_kind_model_and_length_only(cli_env, caplog, kind):
    cli_env["proc"] = _FakeProc(cli_env["events"], stdout=_OK_STDOUT[kind])
    body = _body(kind, "hello there")
    prompt_len = len(cli_subprocess.messages_to_prompt(body["messages"]))
    module_logger = logging.getLogger(cli_subprocess.__name__)
    module_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.DEBUG):
            module_logger.setLevel(logging.DEBUG)
            await cli_subprocess.complete(kind, body)
    finally:
        module_logger.removeHandler(caplog.handler)
    # The handler sits on the module logger and on root: one call, two records.
    spawn = sorted(
        {
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("spawn CLI")
        }
    )
    assert len(spawn) == 1, spawn
    assert kind in spawn[0] and "x-model" in spawn[0] and str(prompt_len) in spawn[0]
    argv = cli_env["exec"][0][0]
    options = [a for a in argv[1:] if a.startswith("-") and len(a) > 2]
    assert options and not any(o in spawn[0] for o in options), spawn[0]
