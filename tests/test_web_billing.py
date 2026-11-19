"""A web run is billed on the tokens claude reports (BUG-159, D-84).

claude's stream-json ``result`` event carries ``modelUsage``: one entry per
model the run called, the page-summary helper included, fed on every API
call (read in the claude 2.1.328 binary: the cost ledger adds each call's
input, output, cache-read and cache-creation tokens to its model's entry).
Its ``usage`` block is the main model's only, so a web run is billed on the
sum of ``modelUsage``, at the price of the route that served the model
asked; a run stopped by ``error_max_turns`` is billed too, and is final:
502 web_budget_exhausted, no other claude route runs it again (BUG-160).
A run without web keeps its estimate.

Only the subprocess boundary is faked (no CLI starts, no inference, nothing
leaves the machine): the real claude wrapper, router, billing and SQLite
usage store run. The web keys are those of tests/test_web_access.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from plugboard import apikeys, cli_subprocess, router, usage, web_access, web_egress
from plugboard.tokenizer import estimate_completion_tokens, estimate_prompt_tokens
from tests import test_web_access as _base
from tests.test_agy_usage import _FakeProc, _sse_events
from tests.test_router_admission import CLI
from tests.test_web_access import MODEL, _auth

keys = _base.keys  # a key without the web, one with it (web_max_uses=4)

MONTH = "2026_03"
INPUT_PRICE = 3.0
OUTPUT_PRICE = 15.0
PROMPT_MARK = "question-marker-159"
MESSAGES = [{"role": "user", "content": f"{PROMPT_MARK} prix de l'essence ?"}]
ANSWER = "Le prix est de 14 dirhams, source https://example.com"

# What claude 2.1.328 writes per model in modelUsage (field names read in the
# binary); the helper is the Sonnet the web run is given.
MODEL_USAGE = {
    "claude-opus-5-5": {
        "inputTokens": 1200,
        "outputTokens": 800,
        "cacheReadInputTokens": 30000,
        "cacheCreationInputTokens": 5000,
        "cacheCreationEphemeral5mInputTokens": 5000,
        "thinkingTokens": 100,
        "webSearchRequests": 2,
        "costUSD": 0.25,
        "contextWindow": 200000,
        "maxOutputTokens": 32000,
    },
    "claude-sonnet-5-5": {
        "inputTokens": 9000,
        "outputTokens": 400,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 2000,
        "webSearchRequests": 0,
        "costUSD": 0.04,
        "contextWindow": 200000,
        "maxOutputTokens": 32000,
    },
}
# The main model only, as claude reports it next to modelUsage.
MAIN_USAGE = {
    "input_tokens": 1200,
    "cache_creation_input_tokens": 5000,
    "cache_read_input_tokens": 30000,
    "output_tokens": 800,
    "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
    "service_tier": "standard",
}
PROMPT = 1200 + 5000 + 30000 + 9000 + 2000 + 0
COMPLETION = 800 + 400
CACHED = 30000


def _cost(prompt: int, completion: int) -> float:
    return round(prompt / 1e6 * INPUT_PRICE + completion / 1e6 * OUTPUT_PRICE, 8)


def _tool_use_events() -> list:
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "WebSearch", "input": {"query": "q"}},
                    {"type": "tool_use", "name": "WebFetch", "input": {"url": "u"}},
                ]
            },
        }
    ]


def _delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _result(subtype: str = "success", **fields) -> dict:
    evt = {
        "type": "result",
        "subtype": subtype,
        "is_error": subtype != "success",
        "num_turns": 3,
        "total_cost_usd": 0.29,
        "usage": MAIN_USAGE,
        "modelUsage": MODEL_USAGE,
    }
    if subtype == "success":
        evt["result"] = ANSWER
    evt.update(fields)
    return {k: v for k, v in evt.items() if v is not None}


def _stdout(*events: dict) -> bytes:
    return "".join(json.dumps(e) + "\n" for e in events).encode()


def _answered(result: dict = None) -> bytes:
    """A web run that answers: tool calls, the answer streamed, its result."""
    return _stdout(
        *_tool_use_events(),
        _delta("Le prix est de 14 dirhams, "),
        _delta("source https://example.com"),
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": ANSWER}]},
        },
        _result() if result is None else result,
    )


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _billing_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Private SQLite usage store and a price for the model asked."""
    import plugboard.billing as b

    store = tmp_path / "usage-store"
    monkeypatch.setattr(usage, "_DATA_DIR", store)
    monkeypatch.setattr(usage, "_DB_PATH", store / "usage.db")
    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    monkeypatch.setattr(b, "current_pricing_month", lambda: MONTH)
    price = {"input_price": INPUT_PRICE, "output_price": OUTPUT_PRICE}
    b.save_pricing(
        MONTH, {"models": {}, "models_by_provider": {CLI["id"]: {"model-x": price}}}
    )


class _FakeClaude:
    """Each run writes ``stdout`` (exit 0), or the next of ``queue``:
    ``(stdout, returncode, stderr)``, one per run, in order."""

    def __init__(self) -> None:
        self.stdout = b""
        self.queue: list = []
        self.runs = 0

    async def exec(self, *argv, **kwargs):
        self.runs += 1
        if self.queue:
            return _FakeProc(*self.queue.pop(0))
        return _FakeProc(self.stdout)


@pytest.fixture
async def claude(_providers_file, tmp_path, monkeypatch):
    """The claude CLI route alone; its process is a fake writing ``stdout``."""
    _providers_file.write_text(json.dumps({"providers": [CLI]}))
    router.invalidate_models_cache()
    fake = _FakeClaude()
    monkeypatch.setattr(cli_subprocess, "_resolve_binary", lambda c: f"/fake/bin/{c}")
    monkeypatch.setattr(cli_subprocess.asyncio, "create_subprocess_exec", fake.exec)
    spawn = tmp_path / "spawn"
    spawn.mkdir()
    monkeypatch.setenv("PLUGBOARD_SUBPROCESS_CWD", str(spawn))
    monkeypatch.setattr(web_access, "_active", 0)
    yield fake
    await web_egress.stop()


async def _chat(client, key, *, web=True, stream=False, model=MODEL):
    return await client.post(
        "/v1/chat/completions",
        headers=_auth(key, web),
        json={"model": model, "messages": MESSAGES, "stream": stream, "cache": False},
    )


def _rows(key) -> list:
    return usage.get_usage_by_key(MONTH, key["key_id"])


def _only_row(key) -> dict:
    rows = _rows(key)
    assert len(rows) == 1, rows
    return rows[0]


def _estimates() -> tuple:
    prompt = estimate_prompt_tokens(MESSAGES)
    completion = estimate_completion_tokens(
        {"choices": [{"message": {"content": ANSWER}}]}
    )
    return prompt, completion


# ── 1, 2. a web run is billed on what claude reports, every model counted ────


def test_reported_usage_sums_every_model_of_model_usage():
    stdout = _answered().decode()
    assert cli_subprocess.claude_reported_usage(stdout) == {
        "prompt_tokens": PROMPT,
        "completion_tokens": COMPLETION,
        "total_tokens": PROMPT + COMPLETION,
        "prompt_tokens_details": {"cached_tokens": CACHED},
    }


async def test_web_run_is_billed_on_the_reported_tokens(client, keys, claude):
    claude.stdout = _answered()
    resp = await _chat(client, keys["web"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["choices"][0]["message"]["content"] == ANSWER
    row = _only_row(keys["web"])
    assert (row["prompt_tokens"], row["completion_tokens"]) == (PROMPT, COMPLETION)
    assert (row["web"], row["web_uses"]) == (1, 2)
    assert row["model"] == "model-x"
    assert row["cost_usd"] == pytest.approx(_cost(PROMPT, COMPLETION))
    assert body["usage"]["prompt_tokens"] == PROMPT
    assert body["usage"]["completion_tokens"] == COMPLETION
    assert body["usage"]["prompt_tokens_details"] == {"cached_tokens": CACHED}
    assert body["billing"]["cost_usd"] == pytest.approx(_cost(PROMPT, COMPLETION))


async def test_streamed_web_run_is_billed_on_the_reported_tokens(client, keys, claude):
    claude.stdout = _answered()
    resp = await _chat(client, keys["web"], stream=True)
    assert resp.status_code == 200, resp.text
    events = _sse_events(resp.text)
    text = "".join(
        e["choices"][0]["delta"].get("content", "") for e in events if e.get("choices")
    )
    assert text == ANSWER
    (last,) = [e["usage"] for e in events if e.get("usage")]
    assert (last["prompt_tokens"], last["completion_tokens"]) == (PROMPT, COMPLETION)
    row = _only_row(keys["web"])
    assert (row["prompt_tokens"], row["completion_tokens"]) == (PROMPT, COMPLETION)
    assert (row["web"], row["web_uses"]) == (1, 2)
    assert row["cost_usd"] == pytest.approx(_cost(PROMPT, COMPLETION))


# ── 3. a claude run without web keeps its estimate ───────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_claude_run_without_web_keeps_its_estimate(client, keys, claude, stream):
    claude.stdout = _answered()
    resp = await _chat(client, keys["web"], web=False, stream=stream)
    assert resp.status_code == 200, resp.text
    row = _only_row(keys["web"])
    assert (row["prompt_tokens"], row["completion_tokens"]) == _estimates()
    assert (row["web"], row["web_uses"]) == (0, 0)


# ── 4. usage absent or malformed: the estimate, the answer served ────────────

_BAD_MODEL_USAGE = {
    "negative": {**MODEL_USAGE, "claude-sonnet-5-5": {"inputTokens": -1}},
    "string": {
        **MODEL_USAGE,
        "claude-sonnet-5-5": {**MODEL_USAGE["claude-sonnet-5-5"], "outputTokens": "4"},
    },
    "float": {
        "claude-opus-5-5": {**MODEL_USAGE["claude-opus-5-5"], "inputTokens": 1.5}
    },
    "bool": {
        "claude-opus-5-5": {**MODEL_USAGE["claude-opus-5-5"], "outputTokens": True}
    },
    "one_negative_among_full_counters": {
        **MODEL_USAGE,
        "claude-sonnet-5-5": {
            **MODEL_USAGE["claude-sonnet-5-5"],
            "cacheCreationInputTokens": -1,
        },
    },
    "past_the_ceiling": {
        **MODEL_USAGE,
        "claude-opus-5-5": {**MODEL_USAGE["claude-opus-5-5"], "inputTokens": 10**30},
    },
    "just_past_10_12": {
        "claude-opus-5-5": {
            **MODEL_USAGE["claude-opus-5-5"],
            "outputTokens": 10**12 + 1,
        }
    },
    "not_an_object": ["claude-opus-5-5"],
    "entry_not_an_object": {"claude-opus-5-5": 12},
    "empty": {},
}


@pytest.mark.parametrize("case", sorted(_BAD_MODEL_USAGE))
async def test_malformed_reported_usage_falls_back_to_the_estimate(
    client, keys, claude, caplog, case
):
    claude.stdout = _answered(_result(modelUsage=_BAD_MODEL_USAGE[case]))
    caplog.set_level(logging.DEBUG)
    resp = await _chat(client, keys["web"])
    assert resp.status_code == 200, resp.text
    row = _only_row(keys["web"])
    assert (row["prompt_tokens"], row["completion_tokens"]) == _estimates()
    assert row["web"] == 1
    warned = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "reported usage" in r.getMessage()
    ]
    assert warned, "a warning says the usage was not used"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert PROMPT_MARK not in logged and "dirhams" not in logged


async def test_without_result_event_the_web_run_is_estimated(client, keys, claude):
    claude.stdout = _answered(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": ""}]}}
    )
    resp = await _chat(client, keys["web"])
    assert resp.status_code == 200, resp.text
    row = _only_row(keys["web"])
    assert (row["prompt_tokens"], row["completion_tokens"]) == _estimates()


async def test_main_usage_alone_is_used_when_model_usage_is_absent(
    client, keys, claude
):
    claude.stdout = _answered(_result(modelUsage=None))
    resp = await _chat(client, keys["web"])
    assert resp.status_code == 200, resp.text
    row = _only_row(keys["web"])
    assert (row["prompt_tokens"], row["completion_tokens"]) == (36200, 800)


# ── 5. 502 web_budget_exhausted is billed, its answer unchanged ──────────────


def _exhausted(*before: dict) -> bytes:
    return _stdout(
        *_tool_use_events(),
        *before,
        _result("error_max_turns", errors=["Reached maximum number of turns (6)"]),
    )


@pytest.fixture
def budget_key(keys):
    rec = apikeys.create_key(
        "webby-budget",
        "web, budget",
        monthly_limit_usd=50.0,
        web_access=True,
        web_max_uses=4,
    )
    apikeys._invalidate_cache()
    return rec


async def test_web_budget_exhausted_is_billed_and_counted_on_the_budget(
    client, budget_key, claude
):
    claude.stdout = _exhausted()
    resp = await _chat(client, budget_key)
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["code"] == "web_budget_exhausted"
    assert set(body) == {"detail", "error_ref", "provider_name", "model_id", "code"}
    assert "web tool calls without a final answer" in body["detail"]
    row = _only_row(budget_key)
    assert row["key_id"] == budget_key["key_id"]
    assert (row["web"], row["web_uses"]) == (1, 2)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (PROMPT, COMPLETION)
    assert row["cost_usd"] == pytest.approx(_cost(PROMPT, COMPLETION))
    cost = _cost(PROMPT, COMPLETION)
    assert usage.get_total_cost_by_key(MONTH, budget_key["key_id"]) == pytest.approx(
        cost
    )
    # The hold became the entry: nothing left held, the cost is on the budget.
    assert usage.get_held_cost(MONTH, key_id=budget_key["key_id"]) == 0.0
    assert usage.get_committed_cost(
        MONTH, key_id=budget_key["key_id"]
    ) == pytest.approx(cost)
    assert claude.runs == 1


async def test_streamed_web_budget_exhausted_before_any_text_is_billed_once(
    client, budget_key, claude
):
    claude.stdout = _exhausted()
    resp = await _chat(client, budget_key, stream=True)
    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "web_budget_exhausted"
    row = _only_row(budget_key)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (PROMPT, COMPLETION)
    assert (row["web"], row["web_uses"]) == (1, 2)
    assert usage.get_held_cost(MONTH, key_id=budget_key["key_id"]) == 0.0


async def test_streamed_web_budget_exhausted_after_text_bills_the_reported_tokens(
    client, budget_key, claude
):
    """Text already sent, then error_max_turns: one entry, the tokens claude
    reported, not the estimate of the partial answer added to them."""
    claude.stdout = _exhausted(_delta("Je cherche encore, "))
    resp = await _chat(client, budget_key, stream=True)
    assert resp.status_code == 200, resp.text  # the stream had begun
    assert "Je cherche encore" in resp.text
    row = _only_row(budget_key)
    assert (row["prompt_tokens"], row["completion_tokens"]) == (PROMPT, COMPLETION)
    assert (row["web"], row["web_uses"]) == (1, 2)
    assert row["cost_usd"] == pytest.approx(_cost(PROMPT, COMPLETION))
    assert usage.get_held_cost(MONTH, key_id=budget_key["key_id"]) == 0.0


async def test_web_budget_exhausted_without_reported_usage_is_estimated(
    client, keys, claude
):
    claude.stdout = _stdout(
        *_tool_use_events(), _result("error_max_turns", modelUsage=None, usage=None)
    )
    resp = await _chat(client, keys["web"])
    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "web_budget_exhausted"
    row = _only_row(keys["web"])
    assert (row["prompt_tokens"], row["completion_tokens"]) == (
        estimate_prompt_tokens(MESSAGES),
        0,
    )
    assert (row["web"], row["web_uses"]) == (1, 2)


# ── 6. never counted twice ───────────────────────────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_web_run_is_recorded_once(client, budget_key, claude, stream):
    claude.stdout = _answered()
    resp = await _chat(client, budget_key, stream=stream)
    assert resp.status_code == 200, resp.text
    assert len(_rows(budget_key)) == 1
    cost = _cost(PROMPT, COMPLETION)
    assert usage.get_committed_cost(
        MONTH, key_id=budget_key["key_id"]
    ) == pytest.approx(cost)


def test_main_usage_is_not_added_to_model_usage():
    """``usage`` repeats the main model's entry of ``modelUsage``: never summed."""
    got = cli_subprocess.claude_reported_usage(_answered().decode())
    assert got["prompt_tokens"] == PROMPT  # not PROMPT + 36200
    assert got["completion_tokens"] == COMPLETION  # not COMPLETION + 800


def test_counters_up_to_10_12_are_kept():
    big = {
        "claude-opus-5-5": {
            **MODEL_USAGE["claude-opus-5-5"],
            "inputTokens": 10**12,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "outputTokens": 0,
        }
    }
    got = cli_subprocess.claude_reported_usage(
        _answered(_result(modelUsage=big)).decode()
    )
    assert got["prompt_tokens"] == 10**12


# ── 7. several claude routes: a run that spent its turns is final (BUG-160) ──
#
# A web run that used its turns without answering ends the request with 502
# web_budget_exhausted: the next claude route would run the same web search
# again, billed again. The run is billed once, at the price of the route that
# ran it, and that route is not penalised (no circuit count, no error metric,
# no provider_down alert, no pause).

ROUTE_A = {**CLI, "id": "pbtest-claude-a", "name": "claude A"}
ROUTE_B = {**CLI, "id": "pbtest-claude-b", "name": "claude B"}
PRICE_A = (3.0, 15.0)
PRICE_B = (5.0, 25.0)
B_USAGE = {
    "claude-opus-5-5": {
        "inputTokens": 700,
        "outputTokens": 300,
        "cacheReadInputTokens": 1000,
        "cacheCreationInputTokens": 0,
    }
}


def _priced(price: tuple, prompt: int, completion: int) -> float:
    return round(prompt / 1e6 * price[0] + completion / 1e6 * price[1], 8)


@pytest.fixture
def two_routes(claude, _providers_file, monkeypatch):
    """Two claude CLI routes for ``model-x``, A tried first, priced apart."""
    import plugboard.billing as b

    _providers_file.write_text(json.dumps({"providers": [ROUTE_A, ROUTE_B]}))
    router.invalidate_models_cache()
    order = {ROUTE_A["id"]: 0.0, ROUTE_B["id"]: 1.0}
    monkeypatch.setattr(router, "_provider_lb_score", lambda pid, _s: order[pid])
    b.save_pricing(
        MONTH,
        {
            "models": {},
            "models_by_provider": {
                ROUTE_A["id"]: {
                    "model-x": {"input_price": PRICE_A[0], "output_price": PRICE_A[1]}
                },
                ROUTE_B["id"]: {
                    "model-x": {"input_price": PRICE_B[0], "output_price": PRICE_B[1]}
                },
            },
        },
    )
    return claude


@pytest.fixture
def penalties(monkeypatch):
    """What would penalise a route, recorded: circuit failures, error
    metrics, provider_down alerts, route pauses; fresh circuits."""
    from plugboard import circuit_breaker, metrics, route_pauses

    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    seen: dict = {"circuit": [], "errors": [], "alerts": [], "pauses": []}
    real_failure = circuit_breaker.record_failure
    real_record = metrics.record

    def failure(model, *, transient=False):
        seen["circuit"].append(model)
        return real_failure(model, transient=transient)

    def record(provider_id, latency, *, error=False):
        if error:
            seen["errors"].append(provider_id)
        return real_record(provider_id, latency, error=error)

    async def alert(provider_id):
        seen["alerts"].append(provider_id)

    def pause(key, *args, **kwargs):
        seen["pauses"].append(key)

    monkeypatch.setattr(circuit_breaker, "record_failure", failure)
    monkeypatch.setattr(metrics, "record", record)
    monkeypatch.setattr(router, "record_provider_failure", alert)
    monkeypatch.setattr(route_pauses, "pause", pause)
    return seen


_NEXT_ROUTE = {
    "fails": (b"", 1, b"API Error: 529 overloaded"),
    "answers": (_answered(_result(modelUsage=B_USAGE)), 0, b""),
    "exhausts": (_exhausted(), 0, b""),
}


def _billed_once_on_a(key) -> None:
    """One entry, route A's, on the tokens claude reported; nothing held."""
    row = _only_row(key)
    assert row["provider_id"] == ROUTE_A["id"]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (PROMPT, COMPLETION)
    assert (row["web"], row["web_uses"]) == (1, 2)
    cost = _priced(PRICE_A, PROMPT, COMPLETION)
    assert row["cost_usd"] == pytest.approx(cost)
    assert usage.get_held_cost(MONTH, key_id=key["key_id"]) == 0.0
    assert usage.get_committed_cost(MONTH, key_id=key["key_id"]) == pytest.approx(cost)


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
@pytest.mark.parametrize("next_route", sorted(_NEXT_ROUTE))
async def test_exhausted_run_is_final_and_billed_once(
    client, budget_key, two_routes, penalties, stream, next_route
):
    """Whatever route B would do (fail, answer, spend its turns too), it never
    runs: the 502 is the answer and route A's run is the only entry."""
    two_routes.queue = [(_exhausted(), 0, b""), _NEXT_ROUTE[next_route]]
    resp = await _chat(client, budget_key, stream=stream, model="model-x")
    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "web_budget_exhausted"
    assert two_routes.runs == 1
    _billed_once_on_a(budget_key)
    assert penalties == {"circuit": [], "errors": [], "alerts": [], "pauses": []}


def _answer_billing_fails(monkeypatch):
    """The answer's billing raises (its route cannot be read back); the web
    runs' billing does not use it."""
    import plugboard.app as app_module

    def boom():
        raise RuntimeError("served_by unreadable")

    monkeypatch.setattr(app_module, "_served_by", boom)


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_spent_run_billing_never_reads_the_route_that_served(
    client, budget_key, two_routes, monkeypatch, stream
):
    """No answer is billed here (B never runs): what this proves is that the
    spent run's billing, on the error exit of forward() (plain) and of
    forward_stream() (stream), never reads the route that served the
    request (served_by raising): the run carries route A and its price.
    Reading it would raise, drop the entry and keep the hold."""
    two_routes.queue = [
        (_exhausted(), 0, b""),
        (_answered(_result(modelUsage=B_USAGE)), 0, b""),
    ]
    _answer_billing_fails(monkeypatch)
    resp = await _chat(client, budget_key, stream=stream, model="model-x")
    assert resp.status_code == 502, resp.text
    assert resp.json()["code"] == "web_budget_exhausted"
    assert two_routes.runs == 1
    # The hold was settled into A's entry, not kept.
    _billed_once_on_a(budget_key)


async def test_exhausted_run_is_billed_when_a_cut_stream_billing_fails(
    client, budget_key, two_routes, monkeypatch
):
    """A sends its first words, then spends its turns: the stream had begun,
    so it ends there (no other route) and the partial answer's billing
    raising never drops A's entry."""
    two_routes.queue = [
        (_exhausted(_delta("Le prix est ")), 0, b""),
        (_answered(_result(modelUsage=B_USAGE)), 0, b""),
    ]
    _answer_billing_fails(monkeypatch)
    resp = await _chat(client, budget_key, stream=True, model="model-x")
    assert resp.status_code == 200, resp.text
    assert "Le prix est" in resp.text
    assert two_routes.runs == 1
    _billed_once_on_a(budget_key)


# ── 8. the rare exits bill the spent runs too (PLU-171, review I5) ──────────
#
# forward() raises no HTTPException today and an empty stream after a run
# is rare; a run already kept on the request's web policy is billed on
# these exits all the same, never dropped.

_SPENT = {
    "prompt_tokens": PROMPT,
    "completion_tokens": COMPLETION,
    "total_tokens": PROMPT + COMPLETION,
}


def _keep_spent_run() -> None:
    """What the router does for a run stopped by its turns (_mark_web_run)."""
    policy = web_access.current()
    assert policy is not None, "a web request"
    policy.exhausted.append(
        web_access.ExhaustedRun(
            provider=CLI, real_model="model-x", usage=dict(_SPENT), uses=2
        )
    )


def _billed_once(key) -> None:
    row = _only_row(key)
    assert row["provider_id"] == CLI["id"]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (PROMPT, COMPLETION)
    assert (row["web"], row["web_uses"]) == (1, 2)
    cost = _cost(PROMPT, COMPLETION)
    assert row["cost_usd"] == pytest.approx(cost)
    assert usage.get_held_cost(MONTH, key_id=key["key_id"]) == 0.0
    assert usage.get_committed_cost(MONTH, key_id=key["key_id"]) == pytest.approx(cost)


@pytest.mark.parametrize("path", ["plain", "stream", "stream_whole"])
async def test_spent_run_is_billed_when_the_call_raises_an_http_error(
    client, budget_key, claude, monkeypatch, path
):
    from fastapi import HTTPException

    import plugboard.app as app_module

    async def forward(body, *args, **kwargs):
        _keep_spent_run()
        raise HTTPException(status_code=503, detail="refused")

    async def forward_stream(body, *args, **kwargs):
        _keep_spent_run()
        raise HTTPException(status_code=503, detail="refused")
        yield b""  # pragma: no cover - an async generator

    monkeypatch.setattr(app_module, "forward", forward)
    monkeypatch.setattr(app_module, "forward_stream", forward_stream)
    if path == "stream_whole":
        # The CLI run whole for a stream (a CLI that cannot stream).
        monkeypatch.setattr(cli_subprocess, "can_stream", lambda kind: False)
    resp = await _chat(client, budget_key, stream=path != "plain")
    assert resp.status_code == 503, resp.text
    _billed_once(budget_key)


async def test_spent_run_is_billed_when_the_stream_is_empty(
    client, budget_key, claude, monkeypatch
):
    import plugboard.app as app_module

    async def forward_stream(body, *args, **kwargs):
        _keep_spent_run()
        return
        yield b""  # pragma: no cover - an async generator

    monkeypatch.setattr(app_module, "forward_stream", forward_stream)
    resp = await _chat(client, budget_key, stream=True)
    assert resp.status_code == 200, resp.text
    assert resp.text == ""
    _billed_once(budget_key)


# ── 9. a web request holds what its turns may cost (BUG-161) ─────────────────
#
# A web run is billed on every turn's tokens (section 1): far more than the
# prompt's estimate the hold took. A budgeted web request now holds its
# prompt plus PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN input tokens (default 20000)
# for each of the run's --max-turns (web_max_uses + 2), priced as the rest;
# the billing settles that hold to the real cost. 0 turns it off.

PER_TURN = 20000
TURNS = 4 + web_access.EXTRA_TURNS  # budget_key: web_max_uses=4


@pytest.fixture
def holds(monkeypatch):
    """The cost of each budget hold taken, the real hold kept."""
    taken: list = []
    real = usage.hold_budget

    def hold_budget(**kwargs):
        taken.append(kwargs["cost"])
        return real(**kwargs)

    monkeypatch.setattr(usage, "hold_budget", hold_budget)
    return taken


def _hold_for(extra_prompt: int) -> float:
    import plugboard.app as app_module

    return _cost(
        estimate_prompt_tokens(MESSAGES) + extra_prompt,
        app_module._BUDGET_RESERVE_TOKENS,
    )


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_web_hold_covers_the_turns_and_settles_to_the_real_cost(
    client, budget_key, claude, holds, monkeypatch, stream
):
    monkeypatch.delenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", raising=False)
    claude.stdout = _answered()
    resp = await _chat(client, budget_key, stream=stream)
    assert resp.status_code == 200, resp.text
    assert holds == [pytest.approx(_hold_for(TURNS * PER_TURN))]
    real = _cost(PROMPT, COMPLETION)
    assert holds[0] > real  # the hold was oversized: billing settles it
    row = _only_row(budget_key)
    assert row["cost_usd"] == pytest.approx(real)
    assert usage.get_held_cost(MONTH, key_id=budget_key["key_id"]) == 0.0
    assert usage.get_committed_cost(
        MONTH, key_id=budget_key["key_id"]
    ) == pytest.approx(real)


async def test_web_hold_is_settled_to_a_spent_run(client, budget_key, claude, holds):
    claude.stdout = _exhausted()
    resp = await _chat(client, budget_key)
    assert resp.status_code == 502, resp.text
    assert holds == [pytest.approx(_hold_for(TURNS * PER_TURN))]
    assert usage.get_held_cost(MONTH, key_id=budget_key["key_id"]) == 0.0
    assert usage.get_committed_cost(
        MONTH, key_id=budget_key["key_id"]
    ) == pytest.approx(_cost(PROMPT, COMPLETION))


async def test_web_hold_per_turn_is_a_setting(
    client, budget_key, claude, holds, monkeypatch
):
    monkeypatch.setenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", "5000")
    claude.stdout = _answered()
    resp = await _chat(client, budget_key)
    assert resp.status_code == 200, resp.text
    assert holds == [pytest.approx(_hold_for(TURNS * 5000))]


async def test_web_hold_off_holds_the_prompt_estimate_as_before(
    client, budget_key, claude, holds, monkeypatch
):
    monkeypatch.setenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", "0")
    claude.stdout = _answered()
    resp = await _chat(client, budget_key)
    assert resp.status_code == 200, resp.text
    assert holds == [pytest.approx(_hold_for(0))]


async def test_a_request_without_web_holds_as_before(
    client, budget_key, claude, holds, monkeypatch
):
    monkeypatch.delenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", raising=False)
    claude.stdout = _answered()
    resp = await _chat(client, budget_key, web=False)
    assert resp.status_code == 200, resp.text
    assert holds == [pytest.approx(_hold_for(0))]


@pytest.mark.parametrize("raw", ["abc", "-5", "1.5"])
async def test_invalid_web_hold_setting_gives_the_default_with_one_warning(
    client, budget_key, claude, holds, monkeypatch, caplog, raw
):
    from plugboard import envcompat

    monkeypatch.setattr(envcompat, "_bad_numbers", set())
    monkeypatch.setenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", raw)
    caplog.set_level(logging.WARNING)
    claude.stdout = _answered()
    for _ in range(2):
        resp = await _chat(client, budget_key)
        assert resp.status_code == 200, resp.text
    assert holds == [pytest.approx(_hold_for(TURNS * PER_TURN))] * 2
    warned = [
        r.getMessage()
        for r in caplog.records
        if "PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN" in r.getMessage()
    ]
    assert len(warned) == 1, warned


async def test_a_web_request_its_budget_cannot_hold_is_refused_before_any_run(
    client, keys, claude, holds, monkeypatch
):
    """$0.20 fits the prompt's estimate, not the run's turns: 429, nothing
    run nor billed; the same request without the web is served."""
    monkeypatch.delenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", raising=False)
    rec = apikeys.create_key(
        "webby-tight",
        "web, tight budget",
        monthly_limit_usd=0.20,
        web_access=True,
        web_max_uses=4,
    )
    apikeys._invalidate_cache()
    assert _hold_for(0) < 0.20 < _hold_for(TURNS * PER_TURN)
    claude.stdout = _answered()
    resp = await _chat(client, rec)
    assert resp.status_code == 429, resp.text
    assert claude.runs == 0
    assert _rows(rec) == []
    resp = await _chat(client, rec, web=False)
    assert resp.status_code == 200, resp.text
    assert claude.runs == 1


@pytest.mark.parametrize(
    "raw", ["1000001", str(10**300), str(10**400)], ids=["1e6+1", "1e300", "1e400"]
)
async def test_web_hold_setting_above_a_million_gives_the_default_with_one_warning(
    client, budget_key, claude, holds, monkeypatch, caplog, raw
):
    """A per-turn count past 1,000,000 is a mistake: priced, 10**400 tokens
    would raise OverflowError; the default is held, with one warning."""
    from plugboard import envcompat

    monkeypatch.setattr(envcompat, "_bad_numbers", set())
    monkeypatch.setenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", raw)
    caplog.set_level(logging.WARNING)
    claude.stdout = _answered()
    for _ in range(2):
        resp = await _chat(client, budget_key)
        assert resp.status_code == 200, resp.text
    assert holds == [pytest.approx(_hold_for(TURNS * PER_TURN))] * 2
    warned = [
        r.getMessage()
        for r in caplog.records
        if "PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN" in r.getMessage()
    ]
    assert len(warned) == 1, warned


async def test_web_hold_setting_of_a_million_is_kept(
    client, keys, claude, holds, monkeypatch
):
    monkeypatch.setenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", "1000000")
    rec = apikeys.create_key(
        "webby-rich", "web, large budget", monthly_limit_usd=1000.0, web_access=True
    )
    apikeys._invalidate_cache()
    claude.stdout = _answered()
    resp = await _chat(client, rec)
    assert resp.status_code == 200, resp.text
    turns = rec["web_max_uses"] + web_access.EXTRA_TURNS
    assert holds == [pytest.approx(_hold_for(turns * 1_000_000))]


# The enlarged hold is given back when the request ends without a run billed:
# a client gone mid-call, an unexpected error (BUG-161).


async def test_enlarged_web_hold_is_given_back_on_an_error(
    client, budget_key, claude, holds, monkeypatch
):
    import plugboard.app as app_module

    monkeypatch.delenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", raising=False)

    async def forward(body, *args, **kwargs):
        assert usage.get_held_cost(MONTH, key_id=budget_key["key_id"]) > 0
        raise RuntimeError("unexpected")

    monkeypatch.setattr(app_module, "forward", forward)
    resp = await _chat(client, budget_key)
    assert resp.status_code >= 500, resp.text
    assert holds == [pytest.approx(_hold_for(TURNS * PER_TURN))]
    assert usage.get_held_cost(MONTH, key_id=budget_key["key_id"]) == 0.0
    assert _rows(budget_key) == []


async def _hang_up_mid_call(key: dict, stream: bool) -> None:
    """One web chat through the ASGI app; the client hangs up while the
    call runs (the call waits for it), as in test_budget_holds."""
    import asyncio

    import plugboard.app as app_module

    started = asyncio.Event()

    async def forward(body, *args, **kwargs):
        started.set()
        await asyncio.sleep(30)

    async def forward_stream(body, *args, **kwargs):
        started.set()
        await asyncio.sleep(30)
        yield b""  # pragma: no cover - an async generator

    raw = json.dumps(
        {"model": MODEL, "messages": MESSAGES, "stream": stream, "cache": False}
    ).encode()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    headers = [(k.lower().encode(), v.encode()) for k, v in _auth(key, True).items()]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
            *headers,
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module, "forward", forward)
        mp.setattr(app_module, "forward_stream", forward_stream)
        await asyncio.wait_for(app_module.app(scope, receive, send), timeout=10)
    assert started.is_set()


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_enlarged_web_hold_is_given_back_when_the_client_hangs_up(
    client, budget_key, claude, holds, monkeypatch, stream
):
    monkeypatch.delenv("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", raising=False)
    await _hang_up_mid_call(budget_key, stream)
    assert holds == [pytest.approx(_hold_for(TURNS * PER_TURN))]
    assert usage.get_held_cost(MONTH, key_id=budget_key["key_id"]) == 0.0
    assert _rows(budget_key) == []
