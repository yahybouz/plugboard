"""Review M1 of 4.14.4 (PLU-55): an HTTP provider's failures hide a model
from client catalogues only past the circuit breaker's threshold, and never
when the last one is a timeout or a quota (left to the route pauses and the
breaker). Providers are faked with aioresponses; nothing is probed.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aioresponses import aioresponses

from plugboard import call_outcome, circuit_breaker, router
from tests.factories import PROVIDER_A

URL_A = "http://fake-llm.test/v1/chat/completions"
MODEL = "fake:model-v1"

_FAILURES = {
    "500": {"status": 500, "body": "upstream down"},
    "429": {"status": 429, "body": "rate limited"},
    "timeout": {"exception": asyncio.TimeoutError()},
}


@pytest.fixture
def one_route(_providers_file, monkeypatch):
    _providers_file.write_text(json.dumps({"providers": [PROVIDER_A]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    monkeypatch.setattr(router, "_DELAYS", [])  # one attempt per call


async def _call(failure, *, stream):
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "cache": False,
    }
    with aioresponses() as mock:
        mock.post(URL_A, **failure)
        try:
            if stream:
                async for _ in router.forward_stream({**body, "stream": True}):
                    pass
            else:
                await router.forward(body)
        except Exception:  # noqa: BLE001 - the outcome is read from the breaker
            pass


def _hidden():
    (key,) = circuit_breaker._circuits  # the one route called
    provider_id, native = key.split(":", 1)
    seen = call_outcome.last_call(provider_id, native)
    return seen is not None and seen[1] is False


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", list(_FAILURES))
async def test_one_failure_does_not_hide(one_route, kind, stream):
    await _call(_FAILURES[kind], stream=stream)

    (circuit,) = circuit_breaker._circuits.values()
    assert circuit.failures == 1
    assert not _hidden()


@pytest.mark.parametrize("stream", [False, True])
async def test_provider_errors_up_to_the_threshold_hide(one_route, stream):
    for _ in range(circuit_breaker._FAILURE_THRESHOLD):
        await _call(_FAILURES["500"], stream=stream)

    assert _hidden()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["429", "timeout"])
async def test_a_timeout_or_a_quota_never_hides(one_route, kind, stream):
    for _ in range(circuit_breaker._FAILURE_THRESHOLD):
        await _call(_FAILURES[kind], stream=stream)

    (circuit,) = circuit_breaker._circuits.values()
    assert circuit.failures >= circuit_breaker._FAILURE_THRESHOLD
    assert not _hidden()


def test_the_breaker_counts_as_before():
    """The nature of the last failure is kept beside the count, which does not
    change: a transient failure still counts and still opens the circuit."""
    circuits = {}
    real = circuit_breaker._circuits
    circuit_breaker._circuits = circuits
    try:
        for _ in range(circuit_breaker._FAILURE_THRESHOLD):
            circuit_breaker.record_failure("p:m", transient=True)
        assert circuits["p:m"].state == circuit_breaker.OPEN
        assert call_outcome.last_call("p", "m") is None
        circuit_breaker.record_success("p:m")
        assert call_outcome.last_call("p", "m")[1] is True
    finally:
        circuit_breaker._circuits = real
