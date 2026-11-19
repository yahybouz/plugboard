"""Typed decisions sit behind PLUGBOARD_DECISIONS_ENABLED, off by default (PLU-35).

Off: the decision routes stay mounted and answer as for a key that holds no
decision engine (an empty list, then the documented 403), so the published
contract is unchanged. Every guard that keeps a decision name away from chat
holds whatever the switch says. On: 4.15.1 exactly, which the rest of the
suite proves since tests/conftest.py turns the switch on.
"""

from __future__ import annotations

import logging

import pytest

from plugboard import decisions

SWITCH = "PLUGBOARD_DECISIONS_ENABLED"
QUESTION = {
    "model": "jev",
    "state": "A synthetic intact parcel.",
    "questions": {"intact": {"type": "noul", "instructions": "Is it intact?"}},
}


def test_the_test_suite_runs_with_decisions_on():
    """tests/conftest.py turns the switch on for the 4.15.1 decision tests;
    removing that line would let them pass against a closed feature."""
    import os

    assert os.environ.get(SWITCH) == "1"
    assert decisions.enabled() is True


def test_decisions_are_off_by_default(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    assert decisions.enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_a_true_value_turns_decisions_on(monkeypatch, raw):
    monkeypatch.setenv(SWITCH, raw)
    assert decisions.enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_a_false_value_keeps_decisions_off(monkeypatch, raw):
    monkeypatch.setenv(SWITCH, raw)
    assert decisions.enabled() is False


def test_an_invalid_value_keeps_decisions_off_with_one_warning(monkeypatch, caplog):
    monkeypatch.setenv(SWITCH, "maybe-later")
    with caplog.at_level(logging.WARNING):
        assert decisions.enabled() is False
        assert decisions.enabled() is False
    warnings = [r for r in caplog.records if SWITCH in r.getMessage()]
    assert len(warnings) == 1


@pytest.fixture
def decisions_off(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


@pytest.fixture
def engine_never_called(monkeypatch):
    from plugboard import decision_api

    async def never(model, payload):
        pytest.fail("a closed decision feature must not reach an engine")

    monkeypatch.setattr(decision_api, "dispatch", never)


@pytest.mark.asyncio
async def test_off_the_catalogue_is_empty_for_a_key_with_every_engine(
    client, decisions_off, engine_never_called
):
    from plugboard import api_contract, apikeys

    record = apikeys.create_key(
        "switch-synthetic", allowed_decision_models=list(decisions.DECISION_MODELS)
    )
    response = await client.get(
        "/v1/decisions/models", headers={"Authorization": f"Bearer {record['key']}"}
    )
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": []}
    api_contract.DecisionModelList.model_validate(response.json())


@pytest.mark.asyncio
async def test_off_a_decision_gets_the_documented_403(
    client, decisions_off, engine_never_called
):
    from plugboard import apikeys

    record = apikeys.create_key(
        "switch-synthetic", allowed_decision_models=list(decisions.DECISION_MODELS)
    )
    response = await client.post(
        "/v1/decisions",
        headers={"Authorization": f"Bearer {record['key']}"},
        json=QUESTION,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Decision model not permitted"


@pytest.mark.asyncio
async def test_off_the_routes_stay_mounted_and_keep_their_key_check(
    client, decisions_off
):
    for method, path in (("get", "/v1/decisions/models"), ("post", "/v1/decisions")):
        response = await client.request(method, path, json=QUESTION)
        assert response.status_code == 401, (method, path)


@pytest.mark.parametrize("model", ["jev", "laya", "laya-multilingual", "kev-0.8b"])
def test_off_decision_names_stay_decision_names(decisions_off, model):
    assert decisions.is_decision_model(model, effective_name=True) is True


@pytest.mark.parametrize("model", ["jev", "laya", "laya-multilingual", "kev-0.8b"])
def test_off_the_router_still_refuses_a_decision_name(decisions_off, model):
    from plugboard import router

    with pytest.raises(ValueError, match="Use /v1/decisions"):
        router.resolve_provider_and_model(model)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["jev", "laya", "kev-0.8b", "ollama:laya"])
async def test_off_chat_keeps_its_400_for_a_decision_name(
    client, decisions_off, monkeypatch, model
):
    async def never(*args, **kwargs):
        pytest.fail("chat must never dispatch a decision name")

    monkeypatch.setattr("plugboard.app.forward", never)
    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer testkey"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Use /v1/decisions for decision models"
