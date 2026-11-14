"""BUG-52: a key with a monthly budget holds the estimated cost of each call
while it runs, so simultaneous calls cannot spend past the budget; the hold
becomes the recorded cost when the call is billed, and goes away when the
call fails. A budgeted key is refused (503 usage_unavailable) when usage
cannot be written (D-37); the other keys are still served."""

import asyncio
import sqlite3
import threading

import pytest
from aioresponses import CallbackResult, aioresponses

from plugboard import router, usage
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY

URL_A = "http://fake-llm.test/v1/chat/completions"
MODEL = "test-provider:fake:model-v1"
MONTH = "2026_11"
# Output at 3000 USD per million tokens: 100 tokens of max_tokens = 0.30 USD;
# the fake answer's 2 completion tokens cost 0.006 USD. Input is free.
PRICE = {"input_price": 0.0, "output_price": 3000.0}


@pytest.fixture
def store(tmp_path, monkeypatch):
    import plugboard.billing as b

    monkeypatch.setattr(usage, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "current_pricing_month", lambda: MONTH)
    return tmp_path


@pytest.fixture
def budget(client, store, monkeypatch):
    """Keys of the client "acme": one with 1 USD a month, one without."""
    import plugboard.apikeys as ak
    import plugboard.billing as b

    monkeypatch.setattr(ak, "_DATA_DIR", store)
    monkeypatch.setattr(ak, "KEYS_FILE", store / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", store / "api_keys.json.lock")
    match = b.PriceMatch(dict(PRICE), "fake:model-v1", "models")
    monkeypatch.setattr(b, "resolve_price", lambda *a, **k: match)
    monkeypatch.setattr(router, "_DELAYS", [])
    limited = ak.create_key("acme", "limited", monthly_limit_usd=1.0)
    free = ak.create_key("acme-free", "unlimited")
    return {
        "limited": {"Authorization": f"Bearer {limited['key']}"},
        "key_id": limited["key_id"],
        "free": {"Authorization": f"Bearer {free['key']}"},
    }


def _body(**extra):
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "cache": False,
        **extra,
    }


def _holds():
    conn = usage._get_conn()
    try:
        return conn.execute("SELECT * FROM budget_holds").fetchall()
    finally:
        conn.close()


async def _until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never met")


# ── The store ────────────────────────────────────────────────────────────────


def test_a_hold_fits_or_is_refused(store):
    first = usage.hold_budget(
        client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.6
    )
    assert isinstance(first, str)
    refused = usage.hold_budget(
        client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.6
    )
    assert isinstance(refused, usage.HoldRefused)
    assert (refused.spent, refused.held) == (0.0, 0.6)
    # Another key of the same client has its own budget.
    assert isinstance(
        usage.hold_budget(client_id="c", key_id="k2", month=MONTH, limit=1.0, cost=0.6),
        str,
    )
    assert usage.get_committed_cost(MONTH, key_id="k") == pytest.approx(0.6)


def test_settling_turns_the_hold_into_the_recorded_cost(store):
    hold = usage.hold_budget(
        client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.9
    )
    usage.settle_hold(
        hold,
        client_id="c",
        model="m",
        provider_id="p",
        prompt_tokens=3,
        completion_tokens=2,
        pricing_month=MONTH,
        cost_usd=0.01,
        key_id="k",
    )
    assert _holds() == []
    [row] = usage.get_usage(MONTH)
    assert (row["key_id"], row["cost_usd"]) == ("k", 0.01)
    assert usage.get_committed_cost(MONTH, key_id="k") == pytest.approx(0.01)


def test_a_released_hold_counts_no_more(store):
    hold = usage.hold_budget(
        client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.9
    )
    usage.release_hold(hold)
    assert usage.get_committed_cost(MONTH, key_id="k") == 0.0
    assert usage.get_usage(MONTH) == []


def test_a_lapsed_hold_is_recorded_at_its_estimate(store, monkeypatch):
    """Its call was served but not recorded, or its process died: the
    estimate is counted, never forgotten."""
    usage.hold_budget(
        client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.9, model="m1"
    )
    monkeypatch.setattr(usage, "HOLD_TTL_S", -1.0)  # every hold is now stale
    refused = usage.hold_budget(
        client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.9
    )
    assert isinstance(refused, usage.HoldRefused) and refused.spent == 0.9
    assert _holds() == []
    [row] = usage.get_usage(MONTH)
    assert (row["key_id"], row["client_id"], row["model"]) == ("k", "c", "m1")
    assert (row["cost_usd"], row["price_source"]) == (0.9, usage.LAPSED_SOURCE)


def test_a_hold_counts_after_the_month_turns(store, monkeypatch):
    """Held in November, billed when it ends: December's checks count it."""
    usage.hold_budget(client_id="c", key_id="k", month="2026_11", limit=1.0, cost=0.9)
    refused = usage.hold_budget(
        client_id="c", key_id="k", month="2026_12", limit=1.0, cost=0.9
    )
    assert isinstance(refused, usage.HoldRefused) and refused.held == 0.9


def test_a_given_hold_id_is_used(store):
    assert (
        usage.hold_budget(
            client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.1, hold_id="abc"
        )
        == "abc"
    )


def test_a_client_wide_budget_counts_the_client(store):
    usage.append_usage("c", "m", "p", 1, 1, MONTH, 0.5, key_id="k1")
    refused = usage.hold_budget(
        client_id="c", key_id="", month=MONTH, limit=1.0, cost=0.6
    )
    assert isinstance(refused, usage.HoldRefused) and refused.spent == 0.5


def test_holds_from_many_threads_never_pass_the_limit(store):
    usage.get_usage(MONTH)  # create the schema first
    results = []

    def take():
        results.append(
            usage.hold_budget(
                client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.3
            )
        )

    threads = [threading.Thread(target=take) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(isinstance(r, str) for r in results) == 3
    assert usage.get_committed_cost(MONTH, key_id="k") == pytest.approx(0.9)


# ── The chat routes ──────────────────────────────────────────────────────────


async def test_simultaneous_calls_cannot_pass_the_budget(client, budget):
    """5 calls at once, 1 USD left, about 0.30 USD each: 3 go through."""
    gate = asyncio.Event()
    calls = []

    async def slow(url, **kwargs):
        calls.append(1)
        await gate.wait()
        return CallbackResult(status=200, payload=FAKE_COMPLETION)

    with aioresponses() as mock:
        mock.post(URL_A, callback=slow, repeat=True)
        tasks = [
            asyncio.create_task(
                client.post(
                    "/v1/chat/completions", headers=budget["limited"], json=_body()
                )
            )
            for _ in range(5)
        ]
        await _until(lambda: len(calls) == 3 and sum(t.done() for t in tasks) == 2)
        assert len(_holds()) == 3
        gate.set()
        answers = await asyncio.gather(*tasks)
    codes = sorted(a.status_code for a in answers)
    assert codes == [200, 200, 200, 429, 429]
    refused = next(a for a in answers if a.status_code == 429).json()["detail"]
    assert "held for calls in progress" in refused
    assert _holds() == []
    rows = usage.get_usage(MONTH)
    assert len(rows) == 3 and sum(r["cost_usd"] for r in rows) <= 1.0


async def test_each_call_frees_its_hold_for_the_next(client, budget):
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        for _ in range(6):  # 6 x 0.30 held one at a time: each fits
            resp = await client.post(
                "/v1/chat/completions", headers=budget["limited"], json=_body()
            )
            assert resp.status_code == 200, resp.text
    assert _holds() == [] and len(usage.get_usage(MONTH)) == 6


async def test_a_call_that_does_not_fit_calls_nothing(client, budget):
    usage.append_usage("acme", "m", "p", 1, 1, MONTH, 0.8, key_id=budget["key_id"])
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        resp = await client.post(
            "/v1/chat/completions", headers=budget["limited"], json=_body()
        )
        assert resp.status_code == 429
        assert not mock.requests
    assert "$0.8000 spent" in resp.json()["detail"]
    # A smaller call still fits.
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        small = await client.post(
            "/v1/chat/completions",
            headers=budget["limited"],
            json=_body(max_tokens=10),
        )
    assert small.status_code == 200


async def test_without_max_tokens_the_default_reserve_applies(
    client, budget, monkeypatch
):
    monkeypatch.setattr("plugboard.app._BUDGET_RESERVE_TOKENS", 400)  # 1.20 USD
    body = _body()
    del body["max_tokens"]
    resp = await client.post(
        "/v1/chat/completions", headers=budget["limited"], json=body
    )
    assert resp.status_code == 429
    monkeypatch.setattr("plugboard.app._BUDGET_RESERVE_TOKENS", 100)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions", headers=budget["limited"], json=body
        )
    assert resp.status_code == 200


async def test_a_streamed_call_holds_then_settles(client, budget):
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY)
        resp = await client.post(
            "/v1/chat/completions", headers=budget["limited"], json=_body(stream=True)
        )
    assert resp.status_code == 200 and "data: [DONE]" in resp.text
    assert _holds() == [] and len(usage.get_usage(MONTH)) == 1


async def test_a_failed_call_gives_its_hold_back(client, budget):
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down", repeat=True)
        resp = await client.post(
            "/v1/chat/completions", headers=budget["limited"], json=_body()
        )
    assert resp.status_code == 502
    assert _holds() == [] and usage.get_usage(MONTH) == []


def _read_only(monkeypatch, store):
    usage.get_usage(MONTH)  # the database and its schema exist

    def connect():
        conn = sqlite3.connect(f"file:{store / 'usage.db'}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(usage, "_get_conn", connect)


async def test_unwritable_usage_refuses_budgeted_keys_only(
    client, budget, store, monkeypatch
):
    _read_only(monkeypatch, store)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        refused = await client.post(
            "/v1/chat/completions", headers=budget["limited"], json=_body()
        )
        assert not mock.requests  # nothing called, nothing served uncounted
        served = await client.post(
            "/v1/chat/completions", headers=budget["free"], json=_body()
        )
    assert refused.status_code == 503
    body = refused.json()
    assert body["code"] == "usage_unavailable" and body["retry_after"] == 30
    assert refused.headers["Retry-After"] == "30"
    assert served.status_code == 200  # D-37: the others are served


async def test_unreadable_usage_refuses_budgeted_keys(client, budget, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(usage, "_get_conn", broken)
    resp = await client.post(
        "/v1/chat/completions", headers=budget["limited"], json=_body()
    )
    assert resp.status_code == 503 and resp.json()["code"] == "usage_unavailable"


async def test_a_failed_settlement_keeps_the_hold(client, budget, monkeypatch):
    """The call was served but not recorded: its estimate stays counted."""

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(usage, "settle_hold", broken)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions", headers=budget["limited"], json=_body()
        )
    assert resp.status_code == 200
    assert len(_holds()) == 1
    assert usage.get_committed_cost(MONTH, key_id=budget["key_id"]) == pytest.approx(
        0.3, abs=0.01
    )


async def test_the_unlimited_key_holds_nothing(client, budget):
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions", headers=budget["free"], json=_body()
        )
    assert resp.status_code == 200
    assert _holds() == [] and len(usage.get_usage(MONTH)) == 1


# ── Review follow-ups: the estimate ──────────────────────────────────────────


def _estimate(body):
    from plugboard.app import _estimate_call_cost

    return _estimate_call_cost({"model": MODEL, "messages": [], **body})


@pytest.mark.parametrize(
    "body, completion",
    [
        ({"max_tokens": 100}, 100),
        ({"max_tokens": 100, "n": 5}, 500),  # each answer is billed
        ({"max_tokens": 100, "best_of": 3, "n": 2}, 300),
        ({"max_tokens": 100000, "max_completion_tokens": 1}, 100000),  # the larger
        ({"max_tokens": 100.0}, 100),
        ({"max_tokens": "100"}, 100),
        ({"max_tokens": True}, 1024),  # not a count: the reserve
        ({"max_tokens": -5}, 1024),
    ],
)
def test_the_estimate_covers_what_the_provider_may_produce(budget, body, completion):
    assert _estimate(body) == pytest.approx(completion * 3000 / 1_000_000)


def test_images_tools_and_tool_calls_count_in_the_prompt(budget, monkeypatch):
    import plugboard.billing as b

    match = b.PriceMatch(
        {"input_price": 1_000_000.0, "output_price": 0.0}, "m", "models"
    )
    monkeypatch.setattr(b, "resolve_price", lambda *a, **k: match)
    text_only = _estimate({"messages": [{"role": "user", "content": "hi"}]})
    image = {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + "A" * 9000},
    }
    with_image = _estimate(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}, image]}
            ]
        }
    )
    assert with_image >= text_only + 1600  # one image, at the prudent rate
    tools = [
        {"type": "function", "function": {"name": "f", "parameters": {"a": "x" * 400}}}
    ]
    assert (
        _estimate({"messages": [], "tools": tools}) > _estimate({"messages": []}) + 50
    )


async def test_an_answer_longer_than_its_hold_is_billed_and_logged(
    client, budget, caplog
):
    long_answer = dict(
        FAKE_COMPLETION, usage={"prompt_tokens": 5, "completion_tokens": 200}
    )
    with aioresponses() as mock, caplog.at_level("WARNING"):
        mock.post(URL_A, payload=long_answer)
        resp = await client.post(
            "/v1/chat/completions", headers=budget["limited"], json=_body(max_tokens=10)
        )
    assert resp.status_code == 200
    [row] = usage.get_usage(MONTH)
    assert row["cost_usd"] == pytest.approx(0.6)
    assert any("overrun" in r.getMessage() for r in caplog.records)


# ── Review follow-ups: every path holds and settles ──────────────────────────


async def test_v2_and_sessions_hold_and_settle(client, budget):
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        for path, extra in (
            ("/v2/chat/completions", {}),
            ("/v1/chat/completions", {"session_id": "s1"}),
            ("/v1/chat/completions", {"session_id": "s1"}),
        ):
            resp = await client.post(
                path, headers=budget["limited"], json=_body(**extra)
            )
            assert resp.status_code == 200, resp.text
    assert _holds() == [] and len(usage.get_usage(MONTH)) == 3


async def test_a_cli_route_asked_to_stream_holds_and_settles(
    client, budget, _providers_file, monkeypatch
):
    import json as _json

    from plugboard import cli_subprocess
    from tests.factories import PROVIDER_A, PROVIDER_B

    cli = {
        "id": "pbtest-cli",
        "name": "CLI test",
        "base_url": "subprocess://claude",
        "bearer_token": "(via CLI)",
        "default_model": "model-x",
        "auth_source": "subprocess:claude",
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    _providers_file.write_text(
        _json.dumps({"providers": [PROVIDER_A, PROVIDER_B, cli]})
    )
    router.invalidate_models_cache()

    async def complete(kind, body, *, timeout_s=None, on_delta=None):
        return _json.loads(_json.dumps(FAKE_COMPLETION))

    monkeypatch.setattr(cli_subprocess, "complete", complete)
    resp = await client.post(
        "/v1/chat/completions",
        headers=budget["limited"],
        json=_body(model="pbtest-cli:model-x", stream=True),
    )
    assert resp.status_code == 200 and "data: [DONE]" in resp.text
    assert _holds() == [] and len(usage.get_usage(MONTH)) == 1


async def test_a_billing_error_keeps_the_hold(client, budget, monkeypatch):
    """Served but not billed: the estimate stays counted, never given back."""
    from plugboard import chat_billing

    def broken(*args, **kwargs):
        raise RuntimeError("pricing grid unreadable")

    monkeypatch.setattr(chat_billing, "charge_completion", broken)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions", headers=budget["limited"], json=_body()
        )
    assert resp.status_code == 200
    assert len(_holds()) == 1


async def test_the_hold_is_kept_on_the_request_before_it_is_written(budget):
    """A request cancelled during the write still gives its hold back."""
    from plugboard import app as app_module

    seen = []

    def hold_budget(**kwargs):
        seen.append(kept[0].id == kwargs["hold_id"])
        return kwargs["hold_id"]

    kept = [None]
    import plugboard.apikeys as ak

    record = ak.get_key_record(budget["limited"]["Authorization"].split()[1])
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(usage, "hold_budget", hold_budget)
        hold = await app_module._take_budget_hold(
            "acme", record, _body(), lambda h: kept.__setitem__(0, h)
        )
    assert seen == [True] and kept[0] is hold


async def test_the_spending_alert_counts_the_spend_not_the_holds(
    client, budget, monkeypatch
):
    usage.hold_budget(
        client_id="acme", key_id=budget["key_id"], month=MONTH, limit=1.0, cost=0.6
    )
    totals = []
    monkeypatch.setattr(
        "plugboard.app._send_spending_alert",
        lambda *a, **k: totals.append(k["current_total_usd"]),
    )
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions", headers=budget["limited"], json=_body()
        )
    assert resp.status_code == 200
    assert totals == [pytest.approx(0.006)]


async def test_the_early_refusal_names_the_holds(client, budget):
    usage.hold_budget(
        client_id="acme", key_id=budget["key_id"], month=MONTH, limit=1.0, cost=1.0
    )
    resp = await client.post(
        "/v1/chat/completions", headers=budget["limited"], json=_body()
    )
    assert resp.status_code == 429
    assert "with the calls in progress" in resp.json()["detail"]


# ── Review follow-ups: the preflight ─────────────────────────────────────────


def _preflight():
    return {"items": [{"provider": "test-provider", "model": "fake:model-v1"}]}


async def test_preflight_pings_hold_and_settle(client, budget):
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/preflight", headers=budget["limited"], json=_preflight()
        )
    assert resp.json()["results"][0]["ok"] is True
    assert _holds() == [] and len(usage.get_usage(MONTH)) == 1


async def test_preflight_refuses_a_budgeted_key_when_usage_cannot_be_written(
    client, budget, store, monkeypatch
):
    _read_only(monkeypatch, store)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        resp = await client.post(
            "/v1/preflight", headers=budget["limited"], json=_preflight()
        )
        assert not mock.requests
    assert resp.status_code == 503 and resp.json()["code"] == "usage_unavailable"


async def test_preflight_refuses_a_budgeted_key_when_usage_cannot_be_read(
    client, budget, monkeypatch
):
    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(usage, "_get_conn", broken)
    resp = await client.post(
        "/v1/preflight", headers=budget["limited"], json=_preflight()
    )
    assert resp.status_code == 503 and resp.json()["code"] == "usage_unavailable"


async def test_a_failed_ping_gives_its_hold_back(client, budget):
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down", repeat=True)
        resp = await client.post(
            "/v1/preflight", headers=budget["limited"], json=_preflight()
        )
    assert resp.json()["results"][0]["error"] == "upstream_error"
    assert _holds() == [] and usage.get_usage(MONTH) == []


# ── PLU-27 review: a client that hangs up while the stream is billed ─────────


async def test_hanging_up_during_stream_billing_keeps_the_order(
    client, budget, monkeypatch
):
    """The usage is written before the hold could be given back and before
    the session's turn ends, and the turn is recorded (BUG-52, D-24)."""
    import json as _json
    import time as _time

    from plugboard import chat_billing
    from plugboard.app import app

    loop = asyncio.get_running_loop()
    billing_started = asyncio.Event()
    events = []
    real_record = chat_billing.record

    def slow_record(*args, **kwargs):
        loop.call_soon_threadsafe(billing_started.set)
        _time.sleep(0.3)  # the client leaves meanwhile
        real_record(*args, **kwargs)
        events.append("usage written")

    real_release = usage.release_hold

    def release(hold_id):
        events.append("hold given back")
        real_release(hold_id)

    monkeypatch.setattr(chat_billing, "record", slow_record)
    monkeypatch.setattr(usage, "release_hold", release)
    monkeypatch.setattr(
        "plugboard.app._record_session_turn",
        lambda *a, **k: events.append("turn recorded"),
    )
    raw = _json.dumps(_body(stream=True)).encode()
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await billing_started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    token = budget["limited"]["Authorization"].encode()
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
            (b"authorization", token),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY)
        await asyncio.wait_for(app(scope, receive, send), timeout=10)
    assert events == ["usage written", "turn recorded"]
    assert _holds() == [] and len(usage.get_usage(MONTH)) == 1
