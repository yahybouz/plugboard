"""Jev answers recorded from the live TypeSafe API (2026-10-24, jev-1.14.0).

TypeSafe rounds the score and each probability to two decimals on its own,
so the reported score can differ from the expectation of the reported
probabilities by the rounding error. Those real answers must be accepted;
answers inconsistent beyond rounding are still rejected.
"""

import pytest

from plugboard.decisions import DecisionUpstreamError, validate_answers

CRITERIA = ["Calm", "Slightly annoyed", "Annoyed", "Angry", "Furious"]
LEGEND = {str(i): text for i, text in enumerate(CRITERIA)}
REQUEST = {
    "model": "jev",
    "state": "Le colis a du retard, c'est un peu agaçant mais bon.",
    "questions": {
        "f": {
            "type": "score",
            "instructions": "How frustrated is the customer?",
            "criteria": CRITERIA,
        }
    },
}


def _answer(score, probabilities):
    return {
        "model": "jev-1.14.0",
        "answers": {
            "f": {
                "type": "score",
                "score": score,
                "confidence": 0.97,
                "legend": LEGEND,
                "probabilities": probabilities,
            }
        },
        "usage": {"input_tokens": 333, "output_tokens": 19},
    }


LIVE_ROUNDED = {"0": 0.0, "1": 0.99, "2": 0.01, "3": 0.0, "4": 0.0}


def test_live_answer_rounded_by_typesafe_is_accepted():
    # Recorded 3 times out of 3 for this state: score 1.0, expectation 1.01.
    answers = validate_answers(REQUEST, _answer(1.0, LIVE_ROUNDED))
    assert answers["f"]["score"] == 1.0
    assert answers["f"]["probabilities"] == LIVE_ROUNDED


def test_live_exact_answer_still_accepted():
    probs = {"0": 0.0, "1": 0.0, "2": 0.03, "3": 0.92, "4": 0.05}
    assert validate_answers(REQUEST, _answer(3.02, probs))["f"]["score"] == 3.02


def test_probabilities_rounded_to_two_decimals_may_not_sum_to_one():
    probs = {"0": 0.33, "1": 0.33, "2": 0.33, "3": 0.0, "4": 0.0}
    assert validate_answers(REQUEST, _answer(1.0, probs))["f"]["score"] == 1.0


@pytest.mark.parametrize(
    "score, probs",
    [
        (2.0, LIVE_ROUNDED),  # score far from the distribution
        (1.0, {"0": 0.0, "1": 0.5, "2": 0.0, "3": 0.0, "4": 0.0}),  # sum 0.5
        (1.0, {"0": 0.0, "1": 0.99, "2": 0.11, "3": 0.0, "4": 0.0}),  # sum 1.1
    ],
)
def test_inconsistent_answers_are_still_rejected(score, probs):
    with pytest.raises(DecisionUpstreamError):
        validate_answers(REQUEST, _answer(score, probs))
