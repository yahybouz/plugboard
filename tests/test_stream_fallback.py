"""Streamed chats: the next provider before the first chunk, a readable end
after it (PLU-26).

A streamed request went to one route only: when that provider failed before
sending anything (an error status, a timeout, a dropped connection), the
client got the error although another provider could serve the model. Once
the stream has begun, a cut ended it with no word of explanation: the client
saw a truncated stream. Providers are faked with aioresponses.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from plugboard import circuit_breaker, router
from tests.factories import (
    FAKE_STREAM_BODY,
    FAKE_STREAM_CHUNK_1,
    PROVIDER_A,
    PROVIDER_B,
)

AUTH = {"Authorization": "Bearer testkey"}
URL_A = "http://fake-llm.test/v1/chat/completions"
URL_B = "http://other-llm.test/v1/chat/completions"
BARE = "fake:model-v1"  # both providers are candidates, A first


@pytest.fixture
def two_routes(_providers_file, monkeypatch):
    """Both providers serve the bare model; A comes first."""
    capped = {**PROVIDER_A, "max_concurrency": 1}
    _providers_file.write_text(json.dumps({"providers": [capped, PROVIDER_B]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 10, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 10, "errors": 0},
        },
    )
    monkeypatch.setattr(circuit_breaker, "_circuits", {})


def _calls(mock, url):
    return len(mock.requests.get(("POST", URL(url)), []))


def _stream(model=BARE):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "cache": False,
    }


@pytest.mark.parametrize(
    "failure",
    [
        {"status": 500, "body": "upstream down"},
        {"status": 429, "body": "rate limited"},
        {"exception": asyncio.TimeoutError()},
        {"exception": aiohttp.ClientConnectionError("refused")},
    ],
    ids=["500", "429", "timeout", "network"],
)
async def test_a_stream_failing_before_its_first_chunk_goes_to_the_next_provider(
    client, two_routes, failure
):
    with aioresponses() as mock:
        mock.post(URL_A, **failure)
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"content":"Hel"' in resp.text and "data: [DONE]" in resp.text
    assert calls == (1, 1)


async def test_a_stream_every_provider_fails_answers_the_last_error(client, two_routes):
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post(URL_B, status=502, body="down too")
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 502, resp.text
    assert calls == (1, 1)


async def test_a_stream_puts_an_open_circuit_last(client, two_routes):
    """BUG-51 with PLU-26: A's circuit is open, B is tried first."""
    for _ in range(3):
        circuit_breaker.record_failure(f"{PROVIDER_A['id']}:{BARE}")
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 200
    assert calls == (0, 1)


async def test_a_stream_with_a_provider_prefix_does_not_change_provider(
    client, two_routes
):
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=_stream(f"{PROVIDER_A['id']}:{BARE}"),
        )
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 502
    assert calls == (1, 0)


async def test_a_stream_cut_after_its_first_chunk_ends_with_a_readable_error(
    client, two_routes, monkeypatch
):
    """The first chunk went to the client: no other provider can take over.
    The stream ends with an error event and [DONE], not a bare cut."""
    import plugboard.app as app_mod

    async def cut_stream(body, **options):
        yield FAKE_STREAM_CHUNK_1
        raise router.ProviderNetworkError(
            "[test-provider] network error: reset by peer at 10.0.0.7:443",
            provider_id="test-provider",
        )

    monkeypatch.setattr(app_mod, "forward_stream", cut_stream)
    resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
    assert resp.status_code == 200
    events = [
        line.removeprefix("data: ")
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert json.loads(events[0])["choices"][0]["delta"]["content"] == "Hel"
    error = json.loads(events[-2])["error"]
    assert error["code"] == "stream_interrupted"
    assert error["message"]
    assert "10.0.0.7" not in resp.text  # never the provider's own text
    assert events[-1] == "[DONE]"


async def test_a_stream_passes_a_full_provider_to_the_next_at_once(client, two_routes):
    """D-23 for streams too, now that they have several routes: a full
    provider is skipped, not waited for, when the next one is free."""
    from plugboard import provider_admission

    capped = {**PROVIDER_A, "max_concurrency": 1}
    held = provider_admission.try_acquire(capped)
    try:
        with aioresponses() as mock:
            mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
            mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
            resp = await asyncio.wait_for(
                client.post("/v1/chat/completions", headers=AUTH, json=_stream()),
                2.0,
            )
            calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    finally:
        held.release()
    assert resp.status_code == 200
    assert calls == (0, 1)
