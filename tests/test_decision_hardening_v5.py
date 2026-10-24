"""Decision billing and slot fairness; engine ports are fakes, never inference."""

import asyncio

import pytest
from fastapi import HTTPException

from tests.test_decision_api import ANSWERS, REQUEST


@pytest.fixture
def keys(monkeypatch, tmp_path):
    import plugboard.apikeys as apikeys

    monkeypatch.setattr(apikeys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(apikeys, "KEYS_LOCK", tmp_path / "keys.lock")
    apikeys._invalidate_cache()

    def make(client_id="synthetic-client", **kwargs):
        kwargs.setdefault("allowed_decision_models", ["jev"])
        rec = apikeys.create_key(client_id, **kwargs)
        return {"Authorization": f"Bearer {rec['key']}"}, rec

    yield make
    apikeys._invalidate_cache()


@pytest.fixture
def jev_key(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")


def _rows(key_id):
    from plugboard import billing, usage

    return usage.get_usage_by_key(billing.current_pricing_month(), key_id)


def _request(state):
    return {**REQUEST, "state": state}


async def _until(condition, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


def _queued():
    from plugboard import decision_api

    return len(decision_api._SLOT._waiters)


class GatedEngine:
    """Records the order engines start in; the first call can be held open."""

    def __init__(self, hold_first=True):
        self.started = []
        self.release = asyncio.Event()
        self.hold_first = hold_first

    async def __call__(self, model, payload):
        self.started.append(payload["state"])
        if self.hold_first and len(self.started) == 1:
            await self.release.wait()
        return ANSWERS


# --- Billing ---------------------------------------------------------------


class UsageEngine(GatedEngine):
    """Never held; answers with the official Jev usage block."""

    def __init__(self, usage):
        super().__init__(hold_first=False)
        self.usage = usage

    async def __call__(self, model, payload):
        await super().__call__(model, payload)
        return {**ANSWERS, "usage": self.usage}


async def test_jev_success_row_carries_official_usage(
    client, keys, jev_key, monkeypatch
):
    from plugboard import decision_api

    headers, rec = keys(monthly_limit_usd=500.0)
    engine = UsageEngine({"input_tokens": 5_000, "output_tokens": 7})
    monkeypatch.setattr(decision_api, "dispatch", engine)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    assert response.json()["billing"] == {"status": "billed", "cost_usd": 0.00021}
    [row] = _rows(rec["key_id"])
    assert row["model"] == "decision:jev"
    assert row["client_id"] == "synthetic-client"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (5_000, 7)
    assert row["cost_usd"] == pytest.approx(0.00021)
    assert (
        row["price_source"] == "official:https://docs.typesafe.ai/models#current-models"
    )


@pytest.mark.parametrize(
    ("model", "usage"),
    [("jev", {"input_tokens": 30}), ("jev", None), ("laya", None)],
)
async def test_served_billing_matches_the_published_contract(
    client, keys, jev_key, monkeypatch, model, usage
):
    from plugboard import api_contract, decision_api

    headers, _ = keys(allowed_decision_models=[model])
    engine = UsageEngine(usage) if usage else GatedEngine(hold_first=False)
    monkeypatch.setattr(decision_api, "dispatch", engine)
    response = await client.post(
        "/v1/decisions", headers=headers, json={**REQUEST, "model": model}
    )
    assert response.status_code == 200, response.text
    schema = api_contract.RESPONSE_MODELS[("POST", "/v1/decisions")]
    assert schema.model_validate(response.json()).model_dump() == response.json()
    bad = {**response.json(), "billing": {"status": "billed", "cost_usd": None}}
    with pytest.raises(ValueError):
        schema.model_validate(bad)


async def test_jev_without_usage_row_has_explicit_estimate_source(
    client, keys, jev_key, monkeypatch
):
    from plugboard import decision_api

    headers, rec = keys(monthly_limit_usd=0.0)
    monkeypatch.setattr(decision_api, "dispatch", GatedEngine(hold_first=False))
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    assert response.json()["billing"] == {"status": "estimated", "cost_usd": 0.002688}
    [row] = _rows(rec["key_id"])
    assert row["model"] == "decision:jev"
    assert row["cost_usd"] == pytest.approx(0.002688)
    assert row["price_source"] == (
        "official-estimate:https://docs.typesafe.ai/models#current-models"
    )


async def test_unconfigured_jev_stays_503_for_budgeted_key(client, keys, monkeypatch):
    headers, rec = keys(monthly_limit_usd=10.0)
    monkeypatch.delenv("PLUGBOARD_JEV_API_KEY", raising=False)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 503
    assert _rows(rec["key_id"]) == []


async def test_laya_success_is_billed_free_with_explicit_source(
    client, keys, monkeypatch
):
    from plugboard import decision_api

    headers, rec = keys(monthly_limit_usd=5.0, allowed_decision_models=["laya"])
    monkeypatch.setattr(decision_api, "dispatch", GatedEngine(hold_first=False))
    response = await client.post(
        "/v1/decisions", headers=headers, json={**REQUEST, "model": "laya"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["billing"] == {"status": "billed", "cost_usd": 0.0}
    [row] = _rows(rec["key_id"])
    assert row["model"] == "decision:laya"
    assert row["cost_usd"] == 0
    assert row["price_source"] == "builtin:laya-free"


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (RuntimeError("SYNTHETIC"), 502),
        ("unavailable", 503),
        ("timeout", 504),
        (HTTPException(429, "Decision capacity reached"), 429),
        ("invalid", 502),
    ],
)
async def test_engine_failure_is_unbilled_and_frees_the_slot(
    client, keys, jev_key, monkeypatch, failure, status
):
    from plugboard import decision_api
    from plugboard.decisions import DecisionTimeout, DecisionUnavailable

    headers_a, rec_a = keys("client-a", monthly_limit_usd=10.0)
    headers_b, rec_b = keys("client-b", monthly_limit_usd=10.0)
    monkeypatch.setattr(decision_api, "TIMEOUT", 1.0)
    started = []
    release = asyncio.Event()

    async def engine(model, payload):
        started.append(payload["state"])
        if payload["state"] == "a":
            await release.wait()
            if failure == "unavailable":
                raise DecisionUnavailable()
            if failure == "timeout":
                # The engine's own timeout: a gateway timeout of A would also
                # spend B's queued envelope (see test_pricing_v6b).
                raise DecisionTimeout()
            if failure == "invalid":
                return {"answers": {}}
            if isinstance(failure, BaseException):
                raise failure
        return ANSWERS

    monkeypatch.setattr(decision_api, "dispatch", engine)
    first = asyncio.create_task(
        client.post("/v1/decisions", headers=headers_a, json=_request("a"))
    )
    await _until(lambda: started == ["a"])
    second = asyncio.create_task(
        client.post("/v1/decisions", headers=headers_b, json=_request("b"))
    )
    await _until(lambda: _queued() == 1)
    release.set()
    assert (await first).status_code == status
    assert (await second).status_code == 200
    assert started == ["a", "b"]
    assert _rows(rec_a["key_id"]) == []
    # No usage in the fake reply: B is billed at the default-context estimate.
    assert [r["cost_usd"] for r in _rows(rec_b["key_id"])] == [pytest.approx(0.002688)]
    again = await client.post("/v1/decisions", headers=headers_a, json=_request("a2"))
    assert again.status_code == 200, "A failed call must not leave its key in flight"


# --- Slot fairness ---------------------------------------------------------


async def test_three_keys_are_served_in_arrival_order(
    client, keys, jev_key, monkeypatch
):
    from plugboard import decision_api

    engine = GatedEngine()
    monkeypatch.setattr(decision_api, "dispatch", engine)
    heads = [keys(f"client-{name}")[0] for name in "abc"]
    tasks = [
        asyncio.create_task(
            client.post("/v1/decisions", headers=heads[0], json=_request("a"))
        )
    ]
    await _until(lambda: engine.started == ["a"])
    for index, name in ((1, "b"), (2, "c")):
        tasks.append(
            asyncio.create_task(
                client.post("/v1/decisions", headers=heads[index], json=_request(name))
            )
        )
        await _until(lambda index=index: _queued() == index)
    duplicate = await client.post(
        "/v1/decisions", headers=heads[1], json=_request("b2")
    )
    assert duplicate.status_code == 429, "A waiting key already holds its one place"
    engine.release.set()
    assert [(await t).status_code for t in tasks] == [200, 200, 200]
    assert engine.started == ["a", "b", "c"]


async def test_full_queue_rejects_immediately(client, keys, jev_key, monkeypatch):
    from plugboard import decision_api

    engine = GatedEngine()
    monkeypatch.setattr(decision_api, "dispatch", engine)
    heads = [keys(f"client-{i}")[0] for i in range(10)]
    holder = asyncio.create_task(
        client.post("/v1/decisions", headers=heads[0], json=_request("0"))
    )
    await _until(lambda: engine.started == ["0"])
    waiters = []
    for i in range(1, 9):
        waiters.append(
            asyncio.create_task(
                client.post("/v1/decisions", headers=heads[i], json=_request(str(i)))
            )
        )
        await _until(lambda i=i: _queued() == i)
    overflow = await asyncio.wait_for(
        client.post("/v1/decisions", headers=heads[9], json=_request("9")), 1.0
    )
    assert overflow.status_code == 429
    engine.release.set()
    assert (await holder).status_code == 200
    assert [(await t).status_code for t in waiters] == [200] * 8
    assert engine.started == [str(i) for i in range(9)]


async def test_queue_wait_is_bounded_then_429(client, keys, jev_key, monkeypatch):
    from plugboard import decision_api

    engine = GatedEngine()
    monkeypatch.setattr(decision_api, "dispatch", engine)
    monkeypatch.setattr(decision_api, "QUEUE_TIMEOUT", 0.05)
    head_a, _ = keys("client-a")
    head_b, rec_b = keys("client-b")
    holder = asyncio.create_task(
        client.post("/v1/decisions", headers=head_a, json=_request("a"))
    )
    await _until(lambda: engine.started == ["a"])
    late = await client.post("/v1/decisions", headers=head_b, json=_request("b"))
    assert late.status_code == 429
    assert _queued() == 0
    engine.release.set()
    assert (await holder).status_code == 200
    retry = await client.post("/v1/decisions", headers=head_b, json=_request("b2"))
    assert retry.status_code == 200
    assert engine.started == ["a", "b2"]
    assert len(_rows(rec_b["key_id"])) == 1


async def test_cancelled_waiter_leaves_no_phantom(client, keys, jev_key, monkeypatch):
    from plugboard import decision_api

    engine = GatedEngine()
    monkeypatch.setattr(decision_api, "dispatch", engine)
    head_a, _ = keys("client-a")
    head_b, _ = keys("client-b")
    head_c, _ = keys("client-c")
    holder = asyncio.create_task(
        client.post("/v1/decisions", headers=head_a, json=_request("a"))
    )
    await _until(lambda: engine.started == ["a"])
    cancelled = asyncio.create_task(
        client.post("/v1/decisions", headers=head_b, json=_request("b"))
    )
    await _until(lambda: _queued() == 1)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert _queued() == 0
    follower = asyncio.create_task(
        client.post("/v1/decisions", headers=head_c, json=_request("c"))
    )
    await _until(lambda: _queued() == 1)
    engine.release.set()
    assert (await holder).status_code == 200
    assert (await follower).status_code == 200
    assert engine.started == ["a", "c"], "The cancelled waiter must never run"
    again = await client.post("/v1/decisions", headers=head_b, json=_request("b2"))
    assert again.status_code == 200, "Cancellation must release the key"


async def test_cancelled_holder_hands_the_slot_on(client, keys, jev_key, monkeypatch):
    from plugboard import decision_api

    engine = GatedEngine()
    monkeypatch.setattr(decision_api, "dispatch", engine)
    head_a, _ = keys("client-a")
    head_b, _ = keys("client-b")
    holder = asyncio.create_task(
        client.post("/v1/decisions", headers=head_a, json=_request("a"))
    )
    await _until(lambda: engine.started == ["a"])
    waiter = asyncio.create_task(
        client.post("/v1/decisions", headers=head_b, json=_request("b"))
    )
    await _until(lambda: _queued() == 1)
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder
    assert (await waiter).status_code == 200
    assert engine.started == ["a", "b"]
    assert (
        await client.post("/v1/decisions", headers=head_a, json=_request("a2"))
    ).status_code == 200


async def test_handoff_racing_a_waiter_cancellation_is_not_lost():
    """A waiter granted the slot as it is cancelled passes the slot on."""
    from plugboard import decision_api

    slot = decision_api._FairSlot()
    await slot.acquire()
    waiter = asyncio.create_task(slot.acquire())
    await _until(lambda: len(slot._waiters) == 1)
    follower = asyncio.create_task(slot.acquire())
    await _until(lambda: len(slot._waiters) == 2)
    # Grant the first waiter and cancel it before it can resume.
    slot.release()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(follower, 1.0)
    slot.release()
    await asyncio.wait_for(slot.acquire(), 0.1)
    slot.release()
