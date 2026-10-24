"""V6b pricing: Jev official per-token price, Laya free, queue-bounded timeout.

Engine ports and the Jev HTTP transport are fakes; no network, no inference.
"""

import asyncio
import copy
import json
import logging

import pytest

from tests.test_decision_api import ANSWERS, REQUEST
from tests.test_decision_jev import Response, fake_http

JEV_PER_MTOK = 0.042
JEV_URL = "https://docs.typesafe.ai/models#current-models"
OBSOLETE = "PLUGBOARD_JEV_PRICE_PER_CALL_USD"


@pytest.fixture
def keys(monkeypatch, tmp_path):
    import plugboard.apikeys as apikeys

    monkeypatch.setattr(apikeys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(apikeys, "KEYS_LOCK", tmp_path / "keys.lock")
    apikeys._invalidate_cache()

    def make(client_id="synthetic-v6b", **kwargs):
        kwargs.setdefault("allowed_decision_models", ["jev"])
        rec = apikeys.create_key(client_id, **kwargs)
        return {"Authorization": f"Bearer {rec['key']}"}, rec

    yield make
    apikeys._invalidate_cache()


@pytest.fixture
def jev_key(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.delenv(OBSOLETE, raising=False)


def _rows(key_id):
    from plugboard import billing, usage

    return usage.get_usage_by_key(billing.current_pricing_month(), key_id)


def _engine(monkeypatch, result):
    from plugboard import decision_api

    async def engine(model, payload):
        return copy.deepcopy(result)

    monkeypatch.setattr(decision_api, "dispatch", engine)


async def _until(condition, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


# --- Jev usage from the official response -----------------------------------

OFFICIAL_BODY = {
    "answers": {"ok": {"type": "noul", "noul": 0.8}},
    "usage": {"input_tokens": 321, "output_tokens": 9},
}


async def test_infer_jev_returns_official_usage_beside_answers(monkeypatch):
    from plugboard import decisions

    fake_http(monkeypatch, Response(json.dumps(OFFICIAL_BODY).encode()))
    result = await decisions.infer_jev(
        {
            "model": "jev",
            "state": "Synthetic state",
            "questions": {"ok": {"type": "noul", "instructions": "Synthetic"}},
        }
    )
    assert result == {
        "answers": {"ok": {"type": "noul", "noul": 0.8}},
        "usage": {"input_tokens": 321, "output_tokens": 9},
    }


async def test_official_usage_flows_from_transport_to_ledger(client, keys, monkeypatch):
    """Real dispatch and infer_jev; only the aiohttp session is fake."""
    headers, rec = keys(monthly_limit_usd=10.0)
    upstream = {
        "answers": {"route": ANSWERS["answers"]["route"]},
        "usage": {"input_tokens": 2_000, "output_tokens": 50},
    }
    fake_http(monkeypatch, Response(json.dumps(upstream).encode()))
    monkeypatch.delenv(OBSOLETE, raising=False)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answers"] == ANSWERS["answers"]
    assert "usage" not in body
    assert body["billing"] == {
        "status": "billed",
        "cost_usd": pytest.approx(2_000 * JEV_PER_MTOK / 1_000_000),
    }
    [row] = _rows(rec["key_id"])
    assert row["model"] == "decision:jev"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (2_000, 50)
    assert row["cost_usd"] == pytest.approx(0.000084)
    assert JEV_URL in row["price_source"]


@pytest.mark.parametrize(
    ("input_tokens", "status", "billed_tokens"),
    [
        (1_048_576, "billed", 1_048_576),
        (1_048_577, "estimated", 64_000),
        (0, "billed", 0),
    ],
)
async def test_usage_plausibility_bound(
    client, keys, jev_key, monkeypatch, input_tokens, status, billed_tokens
):
    headers, rec = keys(monthly_limit_usd=10.0)
    _engine(monkeypatch, {**ANSWERS, "usage": {"input_tokens": input_tokens}})
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    assert response.json()["billing"] == {
        "status": status,
        "cost_usd": pytest.approx(billed_tokens * JEV_PER_MTOK / 1_000_000),
    }
    [row] = _rows(rec["key_id"])
    assert row["prompt_tokens"] == billed_tokens


async def test_estimated_row_records_ceiling_and_invalid_output_as_zero(
    client, keys, jev_key, monkeypatch
):
    headers, rec = keys(monthly_limit_usd=10.0)
    _engine(monkeypatch, {**ANSWERS, "usage": {"output_tokens": -3}})
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.json()["billing"]["status"] == "estimated"
    [row] = _rows(rec["key_id"])
    assert (row["prompt_tokens"], row["completion_tokens"]) == (64_000, 0)
    assert JEV_URL in row["price_source"]


async def test_exhausted_budget_refuses_jev_before_the_call(
    client, keys, jev_key, monkeypatch
):
    from plugboard import decision_api

    headers, _ = keys(monthly_limit_usd=0.0001)
    calls = []

    async def engine(model, payload):
        calls.append(model)
        return {**ANSWERS, "usage": {"input_tokens": 10_000}}

    monkeypatch.setattr(decision_api, "dispatch", engine)
    first = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert first.status_code == 200  # 0.00042 spent: over the 0.0001 limit
    second = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert second.status_code == 429
    assert calls == ["jev"]


# --- Obsolete per-call price variable ---------------------------------------


async def test_obsolete_per_call_price_is_ignored_with_one_warning(
    client, keys, jev_key, monkeypatch, caplog
):
    from plugboard import decision_api

    monkeypatch.setattr(decision_api, "_obsolete_warned", False)
    monkeypatch.setenv(OBSOLETE, "7.77")
    headers, rec = keys(monthly_limit_usd=10.0)
    _engine(monkeypatch, {**ANSWERS, "usage": {"input_tokens": 1_000_000}})
    caplog.set_level(logging.WARNING)
    for _ in range(2):
        response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
        assert response.status_code == 200, response.text
        assert response.json()["billing"]["cost_usd"] == pytest.approx(JEV_PER_MTOK)
    warnings = [r for r in caplog.records if OBSOLETE in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "7.77" not in caplog.text


async def test_invalid_obsolete_value_no_longer_refuses_jev(
    client, keys, jev_key, monkeypatch
):
    monkeypatch.setenv(OBSOLETE, "abc")
    headers, _ = keys(monthly_limit_usd=10.0)
    _engine(monkeypatch, {**ANSWERS, "usage": {"input_tokens": 10}})
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text


# --- F1: queue wait counts against the published API timeout ----------------


async def test_queue_wait_is_deducted_from_the_engine_timeout(
    client, keys, jev_key, monkeypatch
):
    from plugboard import decision_api

    monkeypatch.setattr(decision_api, "TIMEOUT", 0.5)
    monkeypatch.setattr(decision_api, "QUEUE_TIMEOUT", 2.0)
    head_a, _ = keys("client-a")
    head_b, rec_b = keys("client-b")
    release = asyncio.Event()
    started = []

    async def engine(model, payload):
        started.append(payload["state"])
        if payload["state"] == "a":
            await release.wait()
        else:
            await asyncio.sleep(0.35)  # fits 0.5 alone, not after 0.3 queued
        return {**ANSWERS, "usage": {"input_tokens": 10}}

    monkeypatch.setattr(decision_api, "dispatch", engine)
    first = asyncio.create_task(
        client.post("/v1/decisions", headers=head_a, json={**REQUEST, "state": "a"})
    )
    await _until(lambda: started == ["a"])
    second = asyncio.create_task(
        client.post("/v1/decisions", headers=head_b, json={**REQUEST, "state": "b"})
    )
    await _until(lambda: len(decision_api._SLOT._waiters) == 1)
    await asyncio.sleep(0.3)
    release.set()
    assert (await first).status_code == 200
    late = await second
    assert late.status_code == 504
    assert _rows(rec_b["key_id"]) == []


async def test_holder_gateway_timeout_frees_slot_and_spent_waiter_gets_504(
    client, keys, jev_key, monkeypatch
):
    from plugboard import decision_api

    monkeypatch.setattr(decision_api, "TIMEOUT", 0.3)
    monkeypatch.setattr(decision_api, "QUEUE_TIMEOUT", 2.0)
    head_a, rec_a = keys("client-a")
    head_b, rec_b = keys("client-b")
    started = []

    async def engine(model, payload):
        started.append(payload["state"])
        await asyncio.sleep(5 if payload["state"] == "a" else 0.2)
        return {**ANSWERS, "usage": {"input_tokens": 10}}

    monkeypatch.setattr(decision_api, "dispatch", engine)
    first = asyncio.create_task(
        client.post("/v1/decisions", headers=head_a, json={**REQUEST, "state": "a"})
    )
    await _until(lambda: started == ["a"])
    await asyncio.sleep(0.1)  # B queues ~0.2 s: ~0.1 s left, its engine needs 0.2
    second = asyncio.create_task(
        client.post("/v1/decisions", headers=head_b, json={**REQUEST, "state": "b"})
    )
    await _until(lambda: len(decision_api._SLOT._waiters) == 1)
    assert (await first).status_code == 504
    assert (await second).status_code == 504  # its envelope was mostly spent queued
    assert _rows(rec_a["key_id"]) == [] and _rows(rec_b["key_id"]) == []
    assert not decision_api._SLOT._busy and not decision_api._active_keys
    monkeypatch.setattr(decision_api, "TIMEOUT", 45.0)
    started.clear()
    again = await client.post(
        "/v1/decisions", headers=head_b, json={**REQUEST, "state": "b2"}
    )
    assert again.status_code == 200


# --- I2: ledger failure after an engine success -----------------------------


async def test_ledger_failure_after_success_returns_unrecorded(
    client, keys, jev_key, monkeypatch, caplog
):
    from plugboard import api_contract, usage

    headers, rec = keys(monthly_limit_usd=10.0)
    _engine(monkeypatch, {**ANSWERS, "usage": {"input_tokens": 10}})
    original = usage.append_usage

    def broken(**kwargs):
        raise OSError("synthetic ledger failure")

    monkeypatch.setattr(usage, "append_usage", broken)
    caplog.set_level(logging.ERROR)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answers"] == ANSWERS["answers"]
    assert body["billing"] == {"status": "unrecorded", "cost_usd": None}
    schema = api_contract.RESPONSE_MODELS[("POST", "/v1/decisions")]
    assert schema.model_validate(body).model_dump() == body
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "an unrecorded success must be logged"
    assert REQUEST["state"] not in caplog.text
    assert "Which team?" not in caplog.text
    monkeypatch.setattr(usage, "append_usage", original)
    # V9: the usage breaker refuses this budgeted key until a write succeeds;
    # once its retry interval has passed, the next call is the probe.
    from time import monotonic

    from plugboard import decision_api

    refused = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert refused.status_code == 503
    monkeypatch.setattr(
        decision_api,
        "_usage_down_since",
        monotonic() - decision_api.USAGE_RETRY_SECONDS - 1,
    )
    again = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert again.status_code == 200, "slot and key must be released"
    assert again.json()["billing"]["status"] == "billed"
    assert len(_rows(rec["key_id"])) == 1


# --- Published contract ------------------------------------------------------


async def test_served_model_list_matches_contract_and_pricing_is_required(
    client, keys, monkeypatch
):
    from plugboard import api_contract

    headers, _ = keys(allowed_decision_models=["jev", "laya", "laya-multilingual"])
    body = (await client.get("/v1/decisions/models", headers=headers)).json()
    schema = api_contract.RESPONSE_MODELS[("GET", "/v1/decisions/models")]
    assert schema.model_validate(body).model_dump() == body
    stripped = copy.deepcopy(body)
    del stripped["data"][0]["pricing"]
    with pytest.raises(ValueError):
        schema.model_validate(stripped)
    document = api_contract.build_openapi()
    entry = document["components"]["schemas"]["DecisionModelEntry"]
    assert "pricing" in entry["required"]


@pytest.mark.parametrize(
    "billing",
    [
        {"status": "billed", "cost_usd": 0.0},
        {"status": "estimated", "cost_usd": 0.002688},
        {"status": "unrecorded", "cost_usd": None},
    ],
)
def test_decision_response_accepts_each_served_billing(billing):
    from plugboard import api_contract

    schema = api_contract.RESPONSE_MODELS[("POST", "/v1/decisions")]
    body = {"model": "jev", "answers": ANSWERS["answers"], "billing": billing}
    assert schema.model_validate(body).model_dump() == body


@pytest.mark.parametrize(
    "billing",
    [
        {"status": "unknown", "cost_usd": None},
        {"status": "unpriced", "cost_usd": None},
        {"status": "estimated", "cost_usd": None},
        {"status": "unrecorded", "cost_usd": 0.1},
    ],
)
def test_decision_response_rejects_retired_or_inconsistent_billing(billing):
    from plugboard import api_contract

    schema = api_contract.RESPONSE_MODELS[("POST", "/v1/decisions")]
    body = {"model": "jev", "answers": ANSWERS["answers"], "billing": billing}
    with pytest.raises(ValueError):
        schema.model_validate(body)


def test_decision_contract_no_longer_documents_a_jev_402():
    from plugboard import api_contract

    operation = api_contract.build_openapi()["paths"]["/v1/decisions"]["post"]
    assert "402" not in operation["responses"]


# --- Chat grid: dated prices and the zero-by-name rule ----------------------


@pytest.fixture
def grid_dir(tmp_path, monkeypatch):
    from plugboard import billing

    pricing = tmp_path / "pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)
    return pricing


AGY = {
    "id": "agy-cli-subprocess",
    "preset_key": "agy-cli-subprocess",
    "auth_source": "subprocess:agy",
}


def test_dated_google_price_change_is_applied_through_the_month_grid(grid_dir):
    """The built-in table has no effective dates; an operator grid for the
    month of the change overrides it (documented remedy)."""
    from plugboard import billing

    december = {
        "models": {"gemini-4-argon-medium": {"input_price": 4.0, "output_price": 20.0}}
    }
    (grid_dir / "2026_12.json").write_text(json.dumps(december))
    november = billing.resolve_price("gemini-4-argon-medium", "2026_11", provider=AGY)
    assert (november.entry["input_price"], november.entry["output_price"]) == (
        2.0,
        10.0,
    )
    later = billing.resolve_price("gemini-4-argon-medium", "2026_12", provider=AGY)
    assert (later.entry["input_price"], later.entry["output_price"]) == (4.0, 20.0)


def test_builtin_local_zero_never_prices_an_ollama_cloud_model(grid_dir):
    from plugboard import billing

    ollama = {"id": "ollama", "base_url": "http://localhost:11434"}
    match = billing.resolve_price("gemma4:31b-cloud", "2026_10", provider=ollama)
    free = (
        match is not None
        and not match.entry.get("input_price")
        and not match.entry.get("output_price")
    )
    assert not free


def test_self_hosted_ollama_on_another_host_stays_free(grid_dir):
    from plugboard import billing

    # V9: a private IP literal; a DNS name is never trusted (test_final_audit_v9).
    remote = {"id": "ollama-gpu", "base_url": "http://10.0.0.7:11434"}
    match = billing.resolve_price("deepseek-r1", "2026_10", provider=remote)
    assert match is not None
    assert match.entry["input_price"] == 0 and match.entry["output_price"] == 0


def test_local_name_on_a_paid_api_is_not_free(grid_dir):
    from plugboard import billing

    api = {"id": "deepseek-api", "base_url": "https://api.deepseek.test/v1"}
    match = billing.resolve_price("deepseek-r1", "2026_10", provider=api)
    assert match is None or match.entry.get("input_price")
