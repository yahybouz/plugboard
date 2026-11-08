"""A provider that fails at the network level hands over to the next one (BUG-3).

Before the fix, `forward()` caught only timeouts: a refused connection, a
dropped connection or a 200 with a body that is not JSON escaped the
provider loop, so the fallback provider was never tried and the circuit
breaker and provider alerts were not fed.
"""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from tests.factories import FAKE_COMPLETION

_A = "http://fake-llm.test/v1/chat/completions"
_B = "http://other-llm.test/v1/chat/completions"
# Unqualified on purpose: a provider-prefixed model is pinned to that provider
# (exact authorization) and has no fallback by design.
_BODY = {"model": "fake:model-v1", "messages": []}


@pytest.fixture
def no_retry_delay(monkeypatch):
    import plugboard.router as router

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])


@pytest.fixture
def a_first(monkeypatch):
    """Make test-provider the preferred candidate, other-provider the fallback."""
    import plugboard.router as router

    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 100, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 100, "errors": 0},
        },
    )
    monkeypatch.setattr(router, "_IN_FLIGHT_BY_PROVIDER", {})


@pytest.fixture
def failures(monkeypatch):
    import plugboard.router as router

    seen: list = []

    async def _record(provider_id):
        seen.append(provider_id)

    monkeypatch.setattr(router, "record_provider_failure", _record)
    return seen


def _calls(mock, url: str) -> int:
    return len(mock.requests.get(("POST", URL(url)), []))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientConnectionError("Connection refused"),
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientPayloadError("Response payload is not completed"),
    ],
    ids=["refused", "disconnected", "payload"],
)
async def test_network_error_falls_back_to_next_provider(
    _providers_file, a_first, no_retry_delay, failures, error
):
    from plugboard.router import forward, served_by

    with aioresponses() as mock:
        mock.post(_A, exception=error, repeat=True)
        mock.post(_B, payload=FAKE_COMPLETION, status=200)
        result = await forward(dict(_BODY))

        # A was tried once: after a network error the request may already
        # have reached it, so it is not replayed on the same provider.
        assert _calls(mock, _A) == 1
        assert _calls(mock, _B) == 1

    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert served_by()[0]["id"] == "other-provider"
    assert failures == ["test-provider"]


@pytest.mark.asyncio
async def test_non_json_answer_falls_back_to_next_provider(
    _providers_file, a_first, no_retry_delay, failures
):
    from plugboard.router import forward

    with aioresponses() as mock:
        mock.post(
            _A, status=200, body="<html>bad gateway</html>", content_type="text/html"
        )
        mock.post(_B, payload=FAKE_COMPLETION, status=200)
        result = await forward(dict(_BODY))

    assert result["choices"][0]["message"]["content"] == "Hello!"
    assert failures == ["test-provider"]


@pytest.mark.asyncio
async def test_all_providers_down_raises_a_gateway_error(
    _providers_file, a_first, no_retry_delay, failures
):
    from plugboard.router import forward

    with aioresponses() as mock:
        mock.post(_A, exception=aiohttp.ClientConnectionError("refused"), repeat=True)
        mock.post(_B, exception=aiohttp.ClientConnectionError("refused"), repeat=True)
        with pytest.raises(RuntimeError, match=r"\[other-provider\] network error"):
            await forward(dict(_BODY))

    assert failures == ["test-provider", "other-provider"]
