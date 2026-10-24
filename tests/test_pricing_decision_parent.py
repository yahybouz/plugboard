"""Parent oracles for the user's pricing decision (2026-10-24, registre #111).

« on applique à laya un tarif 0 et à JEV le tarif officiel » — Jev official
price: $0.042 per million INPUT tokens, output free, billed from the API's
``usage.input_tokens`` (docs.typesafe.ai/models, docs.typesafe.ai/api/system-one).
Synthetic engine ports only; no network, no inference.
"""

import pytest

from tests.test_decision_api import ANSWERS, REQUEST

JEV_PER_MTOK = 0.042
JEV_MAX_REQUEST_TOKENS = 64_000  # official default per-request context


async def _key(monkeypatch, tmp_path, **kwargs):
    import plugboard.apikeys as keys

    monkeypatch.setattr(keys, "KEYS_FILE", tmp_path / "keys.json")
    monkeypatch.setattr(keys, "KEYS_LOCK", tmp_path / "keys.lock")
    keys._invalidate_cache()
    rec = keys.create_key("synthetic-pricing", **kwargs)
    return {"Authorization": f"Bearer {rec['key']}"}, rec


def _engine(monkeypatch, result, calls):
    from plugboard import decision_api

    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.delenv("PLUGBOARD_JEV_PRICE_PER_CALL_USD", raising=False)

    async def engine(model, payload):
        calls.append(model)
        return result

    monkeypatch.setattr(decision_api, "dispatch", engine)


def _rows(key_id):
    from plugboard import billing, usage

    return usage.get_usage_by_key(billing.current_pricing_month(), key_id)


def _laya_request(model):
    return {**REQUEST, "model": model}


async def test_jev_official_price_billed_from_input_tokens_without_operator_config(
    client, monkeypatch, tmp_path
):
    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    calls = []
    usage = {"input_tokens": 1_000_000, "output_tokens": 50_000}
    _engine(monkeypatch, {**ANSWERS, "usage": usage}, calls)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    assert calls == ["jev"]
    billing = response.json()["billing"]
    assert billing["status"] == "billed"
    assert billing["cost_usd"] == pytest.approx(JEV_PER_MTOK)  # output tokens are free
    rows = _rows(rec["key_id"])
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == pytest.approx(JEV_PER_MTOK)
    assert rows[0]["prompt_tokens"] == 1_000_000
    assert "typesafe" in str(rows[0].get("price_source", "")).lower()


async def test_jev_cost_scales_with_tokens_and_counts_against_budget(
    client, monkeypatch, tmp_path
):
    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=0.05, allowed_decision_models=["jev"]
    )
    calls = []
    _engine(
        monkeypatch,
        {**ANSWERS, "usage": {"input_tokens": 1_000_000, "output_tokens": 1}},
        calls,
    )
    assert (
        await client.post("/v1/decisions", headers=headers, json=REQUEST)
    ).status_code == 200
    assert (
        await client.post("/v1/decisions", headers=headers, json=REQUEST)
    ).status_code == 200
    third = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert third.status_code == 429  # 0.084 >= 0.05: budget enforced on official cost
    assert calls == ["jev", "jev"]


async def test_jev_small_call_costs_a_fraction_of_a_cent(client, monkeypatch, tmp_path):
    headers, _ = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    _engine(
        monkeypatch,
        {**ANSWERS, "usage": {"input_tokens": 120, "output_tokens": 12}},
        [],
    )
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200
    assert response.json()["billing"]["cost_usd"] == pytest.approx(
        120 * JEV_PER_MTOK / 1_000_000
    )


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": -5},
        {"input_tokens": "120"},
        {"input_tokens": 10**12},
        {"input_tokens": True},
    ],
)
async def test_jev_missing_or_invalid_usage_is_billed_at_conservative_ceiling(
    client, monkeypatch, tmp_path, usage
):
    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    result = dict(ANSWERS) if usage is None else {**ANSWERS, "usage": usage}
    _engine(monkeypatch, result, [])
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 200, response.text
    billing = response.json()["billing"]
    ceiling = JEV_MAX_REQUEST_TOKENS * JEV_PER_MTOK / 1_000_000
    assert billing["status"] == "estimated"
    assert billing["cost_usd"] == pytest.approx(ceiling)
    assert _rows(rec["key_id"])[0]["cost_usd"] == pytest.approx(ceiling)


async def test_jev_usage_not_leaked_as_answer(client, monkeypatch, tmp_path):
    headers, _ = await _key(monkeypatch, tmp_path, allowed_decision_models=["jev"])
    _engine(
        monkeypatch, {**ANSWERS, "usage": {"input_tokens": 7, "output_tokens": 1}}, []
    )
    body = (await client.post("/v1/decisions", headers=headers, json=REQUEST)).json()
    assert set(body["answers"]) == set(ANSWERS["answers"])


@pytest.mark.parametrize("model", ["laya", "laya-multilingual"])
async def test_laya_is_explicitly_free_and_traced(client, monkeypatch, tmp_path, model):
    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=[model]
    )
    calls = []
    _engine(monkeypatch, ANSWERS, calls)
    response = await client.post(
        "/v1/decisions", headers=headers, json=_laya_request(model)
    )
    assert response.status_code == 200, response.text
    assert calls == [model]
    assert response.json()["billing"] == {"status": "billed", "cost_usd": 0.0}
    rows = _rows(rec["key_id"])
    assert len(rows) == 1 and rows[0]["cost_usd"] == 0
    assert rows[0].get("price_source") not in (None, "", "unpriced")


async def test_failed_jev_call_still_not_billed(client, monkeypatch, tmp_path):
    from plugboard import decision_api
    from plugboard.decisions import DecisionUnavailable

    headers, rec = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    monkeypatch.setenv("PLUGBOARD_JEV_API_KEY", "synthetic-not-a-real-key")

    async def down(model, payload):
        raise DecisionUnavailable()

    monkeypatch.setattr(decision_api, "dispatch", down)
    assert (
        await client.post("/v1/decisions", headers=headers, json=REQUEST)
    ).status_code == 503
    assert _rows(rec["key_id"]) == []


async def test_jev_without_vendor_key_is_still_unavailable(
    client, monkeypatch, tmp_path
):
    headers, _ = await _key(
        monkeypatch, tmp_path, monthly_limit_usd=10.0, allowed_decision_models=["jev"]
    )
    monkeypatch.delenv("PLUGBOARD_JEV_API_KEY", raising=False)
    response = await client.post("/v1/decisions", headers=headers, json=REQUEST)
    assert response.status_code == 503


async def test_decision_models_list_shows_official_and_free_prices(
    client, monkeypatch, tmp_path
):
    headers, _ = await _key(
        monkeypatch,
        tmp_path,
        allowed_decision_models=["jev", "laya", "laya-multilingual"],
    )
    data = (await client.get("/v1/decisions/models", headers=headers)).json()["data"]
    pricing = {m["id"]: m.get("pricing") for m in data}
    assert pricing["jev"] == {
        "input_usd_per_mtok": JEV_PER_MTOK,
        "output_usd_per_mtok": 0.0,
        "unit": "token",
    }
    for model in ("laya", "laya-multilingual"):
        assert pricing[model] == {
            "input_usd_per_mtok": 0.0,
            "output_usd_per_mtok": 0.0,
            "unit": "token",
        }
