"""Strict decision contract and hostile model-output tests (no model inference)."""

import copy
import math

import pytest
from pydantic import ValidationError

from plugboard import decisions


def payload():
    return {
        "model": "jev",
        "state": "Customer requests a refund.",
        "questions": {
            "route": {
                "type": "choice",
                "instructions": "Route?",
                "criteria": {"billing": None, "technical": "Errors"},
            },
            "urgent": {"type": "noul", "instructions": "Urgent?"},
            "severity": {
                "type": "score",
                "instructions": "Severity?",
                "criteria": ["low", "high"],
            },
        },
    }


def response():
    return {
        "answers": {
            "route": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.75, "technical": 0.25},
                "confidence": 0.5,
            },
            "urgent": {"type": "noul", "noul": 0.8},
            "severity": {
                "type": "score",
                "score": 0.75,
                "legend": {"0": "low", "1": "high"},
                "probabilities": {"0": 0.25, "1": 0.75},
                "confidence": 0.5,
            },
        }
    }


def test_decision_request_preserves_typed_questions():
    request = decisions.DecisionRequest.model_validate(payload())
    assert request.model_dump() == payload()


@pytest.mark.parametrize("model", ["kev-0.8b", "kev", "jaredpalmer/kev-0.8b"])
def test_removed_kev_models_are_rejected_by_decision_schema(model):
    with pytest.raises(ValidationError):
        decisions.DecisionRequest.model_validate({**payload(), "model": model})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(model="chat-model"),
        lambda p: p.update(state={"nested": "not allowed"}),
        lambda p: p.update(state="é" * 2001),
        lambda p: p.update(questions={}),
        lambda p: p.update(
            questions={
                str(i): {"type": "noul", "instructions": "Yes?"} for i in range(9)
            }
        ),
        lambda p: p["questions"].update({"bad/key": p["questions"].pop("route")}),
        lambda p: p["questions"]["urgent"].update(criteria={"true": "yes"}),
        lambda p: p["questions"]["urgent"].update(instructions="é" * 257),
        lambda p: p["questions"]["route"].update(criteria={"only": None}),
        lambda p: p["questions"]["route"].update(
            criteria={"a": {"nested": "no"}, "b": None}
        ),
        lambda p: p["questions"]["severity"].update(criteria=["same", "same"]),
        lambda p: p["questions"]["severity"].update(criteria=["low", 2]),
        lambda p: p.update(url="https://attacker.invalid"),
    ],
)
def test_decision_request_rejects_unbounded_coerced_and_extra_inputs(mutate):
    data = payload()
    mutate(data)
    with pytest.raises(ValidationError):
        decisions.DecisionRequest.model_validate(data)


def test_validator_preserves_engine_specific_confidence_and_copies_only_answers():
    raw = response()
    raw["usage"] = {"private": "drop me"}
    raw["answers"]["route"]["confidence"] = (
        0.75  # Laya uses top probability, not Jev's formula.
    )
    answers = decisions.validate_answers(payload(), raw)
    assert answers["route"]["confidence"] == 0.75
    assert answers["severity"]["score"] == 0.75
    assert answers["urgent"] == {"type": "noul", "noul": 0.8}
    raw["answers"]["route"]["choice"] = "technical"
    assert answers["route"]["choice"] == "billing"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["answers"].pop("urgent"),
        lambda r: r["answers"].update(extra={"type": "noul", "noul": 0.5}),
        lambda r: r["answers"]["route"].update(choice="technical"),
        lambda r: r["answers"]["route"].update(choice="attacker"),
        lambda r: r["answers"]["route"]["probabilities"].update(billing=math.nan),
        lambda r: r["answers"]["route"]["probabilities"].update(billing=math.inf),
        lambda r: r["answers"]["route"]["probabilities"].update(billing=True),
        lambda r: r["answers"]["route"]["probabilities"].update(billing="0.75"),
        lambda r: r["answers"]["route"]["probabilities"].update(billing=0.5),
        lambda r: r["answers"]["route"]["probabilities"].update(extra=0.0),
        lambda r: r["answers"]["route"].update(confidence=2),
        lambda r: r["answers"]["urgent"].update(noul=-0.1),
        lambda r: r["answers"]["urgent"].update(noul=True),
        lambda r: r["answers"]["urgent"].update(type="choice"),
        lambda r: r["answers"]["urgent"].update(secret="UNTRUSTED BODY"),
        lambda r: r["answers"]["severity"].update(score=0.5),
        lambda r: r["answers"]["severity"].update(legend={"0": "high", "1": "low"}),
    ],
)
def test_validator_rejects_hostile_or_inconsistent_answers_without_disclosure(mutate):
    raw = copy.deepcopy(response())
    mutate(raw)
    with pytest.raises(decisions.DecisionUpstreamError) as error:
        decisions.validate_answers(payload(), raw)
    assert str(error.value) == "Decision engine returned an invalid response."


def test_probability_tolerance_accepts_float_roundoff_without_fabrication():
    raw = response()
    raw["answers"]["route"]["probabilities"]["billing"] = 0.750001
    assert (
        decisions.validate_answers(payload(), raw)["route"]["probabilities"]["billing"]
        == 0.750001
    )


def test_choice_labels_accept_bounded_human_text_including_french():
    data = payload()
    data["questions"]["route"]["criteria"] = {
        "équipe facturation": None,
        "service technique": "Incidents",
    }
    request = decisions.DecisionRequest.model_validate(data)
    assert "équipe facturation" in request.questions["route"].criteria


@pytest.mark.parametrize("label", ["é" * 65, "", "x" * 129])
def test_choice_labels_have_utf8_byte_limit(label):
    data = payload()
    data["questions"]["route"]["criteria"] = {label: None, "other": None}
    with pytest.raises(ValidationError):
        decisions.DecisionRequest.model_validate(data)


def test_probability_huge_integer_is_normalized_error():
    raw = response()
    raw["answers"]["urgent"]["noul"] = 10**1000
    with pytest.raises(decisions.DecisionUpstreamError):
        decisions.validate_answers(payload(), raw)
