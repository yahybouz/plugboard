"""Bounded typed decisions and the official Jev transport; no chat dispatch."""

from __future__ import annotations

import asyncio
import json
import math
import os
from typing import Annotated, Iterable, Literal

import aiohttp
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

DECISION_MODELS = ("jev", "laya", "laya-multilingual")
JEV_URL = "https://api.typesafe.ai/v1/systemone"
MAX_RESPONSE_BYTES = 65_536


def is_decision_model(
    model: str, *, provider_ids: Iterable[str] = (), effective_name: bool = False
) -> bool:
    """Classify registered provider -> transport aliases -> namespace/name:tag@rev.

    Only a registered provider or a recognized routing alias consumes a colon
    prefix. Unknown prefixes remain model names, so their tags cannot acquire
    a decision identity. Effective names have already lost their provider ID.
    """
    if not isinstance(model, str):
        return False
    if not effective_name:
        for pid in sorted(provider_ids, key=len, reverse=True):
            if model.startswith(pid + ":"):
                model = model[len(pid) + 1 :]
                break

    # These are the transport/legacy prefixes accepted by router resolution
    # and _clean_model_id, rather than arbitrary user-chosen provider names.
    transport_aliases = {
        "ollama",
        "openclaw",
        "openclaw-llm-proxy",
        "anthropic",
        "claude",
        "claude-via-claude-cli",
        "claude-code",
        "openai",
        "codex",
        "chatgpt",
        "chatgpt-via-codex-cli",
        "google",
        "gemini",
        "google-gemini-cli",
        "google-gemini-via-gemini-cli",
        "gemini-cli",
        "gemini-cli-subprocess",
        "codex-cli",
        "codex-cli-subprocess",
        "claude-cli",
        "claude-cli-subprocess",
    }
    name = model.strip().lower()
    # Advance through the original string instead of copying the remaining
    # suffix twice per alias (quadratic work for a long caller-supplied ID).
    start = 0
    while True:
        end = name.find(":", start)
        if end < 0 or name[start:end] not in transport_aliases:
            break
        start = end + 1
    name = name[start:]
    # Split tags before interpreting wrappers: a tag may itself contain '/',
    # '--', or a decision family without changing the effective model identity.
    name = name.partition(":")[0].partition("@")[0]
    name = name.rsplit("/", 1)[-1].rsplit("--", 1)[-1]
    # Kev remains a rejection tombstone so retired IDs never fall back to chat.
    return any(
        name == family or name.startswith(family + "-")
        for family in ("jev", "laya", "kev")
    )


class DecisionUnavailable(Exception):
    def __init__(self):
        super().__init__("Decision engine is unavailable.")


class DecisionUpstreamError(Exception):
    """``charge``: what to bill when the engine took the call (TypeSafe
    answered 200 but the answer is rejected): ``{"usage": ...}`` as reported,
    ``{}`` for the ceiling. None: nothing was processed, nothing is billed."""

    def __init__(self, charge: dict | None = None):
        super().__init__("Decision engine returned an invalid response.")
        self.charge = charge


class DecisionTimeout(Exception):
    """``charge`` as for ``DecisionUpstreamError``: set once the request was sent."""

    def __init__(self, charge: dict | None = None):
        super().__init__("Decision engine timed out.")
        self.charge = charge


def _bounded_text(value: str, maximum: int) -> str:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("Text must be valid UTF-8.") from None
    if not size or size > maximum:
        raise ValueError("Text exceeds the supported byte limits.")
    return value


Identifier = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
]
Instructions = Annotated[str, AfterValidator(lambda v: _bounded_text(v, 512))]
Description = Annotated[str, AfterValidator(lambda v: _bounded_text(v, 512))]
OptionLabel = Annotated[str, AfterValidator(lambda v: _bounded_text(v, 128))]
State = Annotated[str, AfterValidator(lambda v: _bounded_text(v, 4000))]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChoiceQuestion(_StrictModel):
    type: Literal["choice"]
    instructions: Instructions
    criteria: Annotated[
        dict[OptionLabel, Description | None], Field(min_length=2, max_length=16)
    ]


class NoulQuestion(_StrictModel):
    type: Literal["noul"]
    instructions: Instructions


class ScoreQuestion(_StrictModel):
    type: Literal["score"]
    instructions: Instructions
    criteria: Annotated[list[Description], Field(min_length=2, max_length=16)]

    @model_validator(mode="after")
    def unique_levels(self):
        if len(set(self.criteria)) != len(self.criteria):
            raise ValueError("Score levels must be unique.")
        return self


Question = Annotated[
    ChoiceQuestion | NoulQuestion | ScoreQuestion, Field(discriminator="type")
]


class DecisionRequest(_StrictModel):
    model: Literal["jev", "laya", "laya-multilingual"]
    state: State
    questions: Annotated[dict[Identifier, Question], Field(min_length=1, max_length=8)]


def _number(value, low=0.0, high=1.0):
    if (
        type(value) not in (int, float)
        or not low <= value <= high
        or not math.isfinite(value)
    ):
        raise DecisionUpstreamError()
    return value


# Half of the 0.01 step to which Jev rounds scores and probabilities
# (observed on the live API, jev-1.14.0).
_ROUNDING = 0.005


def _probabilities(raw, expected):
    if type(raw) is not dict or set(raw) != set(expected):
        raise DecisionUpstreamError()
    result = {key: _number(raw[key]) for key in expected}
    # Jev rounds each probability to two decimals on its own: the sum may be
    # off by half a hundredth per entry, never more.
    if abs(math.fsum(result.values()) - 1.0) > _ROUNDING * len(result) + 1e-9:
        raise DecisionUpstreamError()
    return result


def validate_answers(request: DecisionRequest | dict, upstream: dict) -> dict:
    """Return a bounded copy, preserving each engine's confidence statistic.

    Confidence is a reported engine-specific statistic, never recomputed here.
    Jev choice/score use distribution statistics; Laya uses normalized entropy.
    """
    if not isinstance(request, DecisionRequest):
        request = DecisionRequest.model_validate(request)
    if type(upstream) is not dict or type(upstream.get("answers")) is not dict:
        raise DecisionUpstreamError()
    raw_answers = upstream["answers"]
    if set(raw_answers) != set(request.questions):
        raise DecisionUpstreamError()
    answers = {}
    for identifier, question in request.questions.items():
        raw = raw_answers[identifier]
        if type(raw) is not dict or raw.get("type") != question.type:
            raise DecisionUpstreamError()
        if question.type == "noul":
            if set(raw) != {"type", "noul"}:
                raise DecisionUpstreamError()
            answers[identifier] = {"type": "noul", "noul": _number(raw["noul"])}
            continue
        if question.type == "choice":
            if set(raw) != {"type", "choice", "probabilities", "confidence"}:
                raise DecisionUpstreamError()
            probabilities = _probabilities(raw["probabilities"], question.criteria)
            selected = raw["choice"]
            if (
                type(selected) is not str
                or selected not in probabilities
                or probabilities[selected] != max(probabilities.values())
            ):
                raise DecisionUpstreamError()
            answers[identifier] = {
                "type": "choice",
                "choice": selected,
                "probabilities": probabilities,
                "confidence": _number(raw["confidence"]),
            }
            continue
        if set(raw) != {"type", "score", "legend", "probabilities", "confidence"}:
            raise DecisionUpstreamError()
        legend = {str(i): text for i, text in enumerate(question.criteria)}
        if type(raw["legend"]) is not dict or raw["legend"] != legend:
            raise DecisionUpstreamError()
        probabilities = _probabilities(raw["probabilities"], legend)
        score = _number(raw["score"], 0, len(legend) - 1)
        expectation = math.fsum(i * probabilities[str(i)] for i in range(len(legend)))
        # The score and the probabilities are rounded separately (two
        # decimals): bound the gap by the worst-case rounding of both.
        levels = range(len(legend))
        if abs(score - expectation) > _ROUNDING * (1 + sum(levels)) + 1e-9:
            raise DecisionUpstreamError()
        answers[identifier] = {
            "type": "score",
            "score": score,
            "legend": legend,
            "probabilities": probabilities,
            "confidence": _number(raw["confidence"]),
        }
    return answers


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise DecisionUpstreamError()
        obj[key] = value
    return obj


async def infer_jev(request_dict: dict) -> dict:
    """Call only TypeSafe's official TLS endpoint, never chat or a user URL.

    Primary references: docs.typesafe.ai/api/system-one, /primitives/score,
    /confidence. The common schema is deliberately narrower than the API.
    """
    request = DecisionRequest.model_validate(request_dict)
    if request.model != "jev":
        raise DecisionUnavailable()
    for question in request.questions.values():
        if question.type == "score" and len(question.criteria) > 10:
            raise ValueError("Jev supports at most 10 score levels.")
    key = os.environ.get("PLUGBOARD_JEV_API_KEY", "").strip()
    if not key:
        raise DecisionUnavailable()
    payload = request.model_dump()
    payload["model"] = "jev-latest"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=False
        ) as session:
            async with session.post(
                JEV_URL,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise DecisionUpstreamError()
                return await _answered(request, response)
    except (DecisionUpstreamError, DecisionTimeout):
        raise
    except aiohttp.ConnectionTimeoutError:
        raise DecisionTimeout() from None  # never connected: nothing was sent
    except (asyncio.TimeoutError, TimeoutError):
        # Sent without an answer in time: TypeSafe may have processed it.
        raise DecisionTimeout(charge={}) from None
    except (aiohttp.ClientError, ValueError, UnicodeError, RecursionError):
        raise DecisionUpstreamError() from None


async def _answered(request: DecisionRequest, response) -> dict:
    """Answers of a 200; a rejected 200 still carries what TypeSafe billed."""
    upstream = None
    try:
        body = bytearray()
        async for chunk in response.content.iter_chunked(8192):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise DecisionUpstreamError()
        upstream = json.loads(body, object_pairs_hook=_unique_object)
        answers = validate_answers(request, upstream)
    except (asyncio.TimeoutError, TimeoutError):
        raise DecisionTimeout(charge=_reported(upstream)) from None
    except Exception:  # noqa: BLE001 - any rejected answer, never echoed
        raise DecisionUpstreamError(charge=_reported(upstream)) from None
    result = {"answers": answers}
    # Token counts price the call: kept apart from the answers and validated
    # where the call is billed.
    if "usage" in upstream:
        result["usage"] = upstream["usage"]
    return result


def _reported(upstream) -> dict:
    """The usage TypeSafe reported, checked where billed; {} bills the ceiling."""
    if type(upstream) is dict and "usage" in upstream:
        return {"usage": upstream["usage"]}
    return {}
