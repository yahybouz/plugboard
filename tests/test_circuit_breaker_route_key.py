"""The circuit breaker reads and writes one key per route (BUG-32).

forward() recorded failures and successes under the native model name while
it read the breaker under the model string the client sent. A model asked by
its qualified name (``provider-id:native``) therefore never read its own
failures: its circuit was never seen open, the open native circuit it left
behind never closed again, and the LLM pricing pass, which reads
``provider-id:model``, kept choosing a provider whose calls failed.
"""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from tests.factories import FAKE_COMPLETION

_A = "http://fake-llm.test/v1/chat/completions"
_B = "http://other-llm.test/v1/chat/completions"
_PINNED = "test-provider:fake:model-v1"
_UNQUALIFIED = "fake:model-v1"


@pytest.fixture
def breaker(monkeypatch):
    """Fresh breaker state; record what forward() reads from it."""
    import plugboard.router as router
    from plugboard import circuit_breaker as cb

    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(router, "_IN_FLIGHT_BY_PROVIDER", {})
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 100, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 100, "errors": 0},
        },
    )

    async def _no_alert(_provider_id):
        return None

    monkeypatch.setattr(router, "record_provider_failure", _no_alert)

    reads: list = []
    check = cb.check_circuit

    def _spy(key):
        result = check(key)
        reads.append(result["state"])
        return result

    monkeypatch.setattr(cb, "check_circuit", _spy)
    return reads


async def _fail_three_times(model: str, mock) -> None:
    from plugboard.router import forward

    for _ in range(3):
        mock.post(_A, exception=aiohttp.ClientConnectionError("refused"))
        mock.post(_B, exception=aiohttp.ClientConnectionError("refused"))
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await forward({"model": model, "messages": []})


@pytest.mark.asyncio
async def test_pinned_model_reads_its_open_circuit(_providers_file, breaker):
    from plugboard import circuit_breaker as cb
    from plugboard.router import forward

    with aioresponses() as mock:
        await _fail_three_times(_PINNED, mock)
        mock.post(_A, payload=FAKE_COMPLETION)
        await forward({"model": _PINNED, "messages": []})

    assert breaker[-1] == cb.OPEN


@pytest.mark.asyncio
async def test_pinned_model_circuit_closes_after_cooldown(
    _providers_file, breaker, monkeypatch
):
    from plugboard import circuit_breaker as cb
    from plugboard.router import forward

    monkeypatch.setattr(cb, "_COOLDOWN_SECONDS", -1.0)
    with aioresponses() as mock:
        await _fail_three_times(_PINNED, mock)
        mock.post(_A, payload=FAKE_COMPLETION)
        await forward({"model": _PINNED, "messages": []})

    assert {c["state"] for c in cb.get_all_circuits().values()} == {cb.CLOSED}


@pytest.mark.asyncio
async def test_pricing_pass_skips_a_route_whose_circuit_is_open(
    _providers_file, breaker
):
    from plugboard.config import load_providers
    from plugboard.llm_pricing import _pricing_provider

    with aioresponses() as mock:
        await _fail_three_times(_PINNED, mock)

    assert _pricing_provider(load_providers())["id"] == "other-provider"


@pytest.mark.asyncio
async def test_unqualified_model_still_reads_its_open_circuit(_providers_file, breaker):
    from plugboard import circuit_breaker as cb
    from plugboard.router import forward

    with aioresponses() as mock:
        await _fail_three_times(_UNQUALIFIED, mock)
        mock.post(_A, payload=FAKE_COMPLETION)
        await forward({"model": _UNQUALIFIED, "messages": []})

    assert breaker[-1] == cb.OPEN


@pytest.mark.asyncio
async def test_one_open_route_does_not_pin_an_unqualified_model(
    _providers_file, breaker
):
    """While another route answers, the model's fallback does not kick in.

    ``gpt-5.5-pro`` has a FALLBACK_MAP entry (``gpt-5.5``) that the rewrite
    finds inside the same native name: an open preferred route must not pin
    the request to that route and drop the one that answers.
    """
    from plugboard.router import forward, served_by

    model = "openai/gpt-5.5-pro"
    with aioresponses() as mock:
        for _ in range(4):
            mock.post(_A, exception=aiohttp.ClientConnectionError("refused"))
            mock.post(_B, payload=FAKE_COMPLETION)
        for _ in range(4):
            await forward({"model": model, "messages": []})

    assert served_by()[0]["id"] == "other-provider"


@pytest.mark.asyncio
async def test_a_client_error_does_not_stop_other_callers(_providers_file, breaker):
    """Three requests refused for their own content leave the model usable."""
    from plugboard.router import forward

    with aioresponses() as mock:
        for _ in range(3):
            mock.post(
                _A,
                status=400,
                payload={"error": {"message": "prompt is too long"}},
            )
        mock.post(_A, payload=FAKE_COMPLETION)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="HTTP 400"):
                await forward({"model": _PINNED, "messages": []})
        result = await forward({"model": _PINNED, "messages": []})
        calls = len(mock.requests.get(("POST", URL(_A)), []))

    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert calls == 4


@pytest.mark.asyncio
async def test_client_errors_do_not_pin_other_callers_to_one_route(
    _providers_file, breaker
):
    """One caller's refused requests never narrow the next caller's routes.

    Three requests refused for their own content (HTTP 400) on both routes
    open both circuits. ``gpt-5.5-pro``'s FALLBACK_MAP entry, ``gpt-5.5``, is
    a substring of its native name: the rewrite took that for the fallback
    model and pinned the next caller to the first route, dropping the one
    that answers.
    """
    from plugboard.router import forward, served_by

    model = "openai/gpt-5.5-pro"
    refused = {"error": {"message": "prompt is too long"}}
    with aioresponses() as mock:
        for _ in range(3):
            mock.post(_A, status=400, payload=refused)
            mock.post(_B, status=400, payload=refused)
        for _ in range(3):
            with pytest.raises(RuntimeError, match="HTTP 400"):
                await forward({"model": model, "messages": []})
        mock.post(_A, exception=aiohttp.ClientConnectionError("refused"))
        mock.post(_B, payload=FAKE_COMPLETION)
        result = await forward({"model": model, "messages": []})

    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert served_by()[0]["id"] == "other-provider"
