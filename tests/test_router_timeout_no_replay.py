"""BUG-47 (audit A-02): a provider that exceeds its timeout is not called again.

The request already waited the whole timeout (up to 900 s by default); sent
again to the same route, it waited it up to four times before the next
provider was tried. A timed-out provider is now called once, and the next
candidate serves the request.
"""

from __future__ import annotations

import asyncio

import pytest
from aioresponses import aioresponses
from yarl import URL

from plugboard import alerts, circuit_breaker, metrics, router
from tests.factories import FAKE_COMPLETION

_SLOW = "http://fake-llm.test/v1/chat/completions"
_NEXT = "http://other-llm.test/v1/chat/completions"
# Unqualified: a provider-prefixed model is pinned to its provider, no fallback.
_BODY = {"model": "fake:model-v1", "messages": []}


def _calls(mock: aioresponses, url: str) -> int:
    return len(mock.requests.get(("POST", URL(url)), []))


@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch):
    """Metrics and provider-down counters start empty, and are put back."""
    monkeypatch.setattr(metrics, "_stats", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})


@pytest.fixture
def no_retry_wait(monkeypatch):
    """Keep the retry count, skip the waits between retries."""
    monkeypatch.setattr(router, "_DELAYS", [0.0] * len(router._DELAYS))


@pytest.fixture
def slow_first(monkeypatch):
    """test-provider is the preferred candidate, other-provider the next one."""
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 100, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 100, "errors": 0},
        },
    )


@pytest.fixture
def failures(monkeypatch):
    seen: list = []

    async def _record(provider_id):
        seen.append(provider_id)

    monkeypatch.setattr(router, "record_provider_failure", _record)
    return seen


@pytest.mark.asyncio
async def test_timed_out_provider_is_called_once_then_the_next_serves(
    _providers_file, no_retry_wait, slow_first, failures
):
    with aioresponses() as mock:
        mock.post(_SLOW, exception=asyncio.TimeoutError(), repeat=True)
        mock.post(_NEXT, payload=FAKE_COMPLETION, repeat=True)

        result = await router.forward(dict(_BODY), timeout=0.1)

        assert _calls(mock, _SLOW) == 1
        assert _calls(mock, _NEXT) == 1
    assert result["choices"][0]["message"]["content"] == "Hello!"
    provider, _model = router.served_by()
    assert provider["id"] == "other-provider"
    # The timeout still counts against the slow route, once (D-7): circuit,
    # provider-down alert and error metrics (it counted 5 errors before).
    assert circuit_breaker._circuits["test-provider:fake:model-v1"].failures == 1
    assert failures == ["test-provider"]
    assert metrics._stats["test-provider"]["requests"] == 1
    assert metrics._stats["test-provider"]["errors"] == 1


@pytest.mark.asyncio
async def test_every_provider_timing_out_is_called_once_and_reported_as_timeout(
    _providers_file, no_retry_wait, slow_first
):
    with aioresponses() as mock:
        mock.post(_SLOW, exception=asyncio.TimeoutError(), repeat=True)
        mock.post(_NEXT, exception=asyncio.TimeoutError(), repeat=True)

        with pytest.raises(RuntimeError, match="timeout"):
            await router.forward(dict(_BODY), timeout=0.1)

        assert _calls(mock, _SLOW) == 1
        assert _calls(mock, _NEXT) == 1


@pytest.mark.asyncio
async def test_a_5xx_is_still_retried_on_the_same_route(
    _providers_file, no_retry_wait, slow_first
):
    # Only the timeout stops replaying: a transient 503 keeps its retries.
    with aioresponses() as mock:
        mock.post(_SLOW, status=503, body="busy")
        mock.post(_SLOW, payload=FAKE_COMPLETION)

        result = await router.forward(dict(_BODY), timeout=0.1)

        assert _calls(mock, _SLOW) == 2
        assert _calls(mock, _NEXT) == 0
    assert result["choices"][0]["message"]["content"] == "Hello!"
