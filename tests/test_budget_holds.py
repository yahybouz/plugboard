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


def test_a_released_or_expired_hold_counts_no_more(store, monkeypatch):
    hold = usage.hold_budget(
        client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.9
    )
    usage.release_hold(hold)
    assert usage.get_committed_cost(MONTH, key_id="k") == 0.0
    usage.hold_budget(client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.9)
    monkeypatch.setattr(usage, "HOLD_TTL_S", -1.0)  # every hold is now stale
    assert usage.get_committed_cost(MONTH, key_id="k") == 0.0
    assert isinstance(
        usage.hold_budget(client_id="c", key_id="k", month=MONTH, limit=1.0, cost=0.9),
        str,
    )
    assert len(_holds()) == 1  # the stale one was purged


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
