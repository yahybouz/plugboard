"""A context too long is not sent again to the same provider (PLU-5, D-14).

forward() retried an HTTP 5xx saying the context or the message is too long
(``circuit_breaker.is_request_too_long``) on the same provider, like any
transient error: the long request went out four times and, with the default
delays (2, 5 and 15 s), the client waited about 22 s before the 400 of BUG-40.
The same request fails the same way, so it now goes straight to the next
candidate provider, and the client gets 400 when no candidate answers. Any
other retryable error is still retried. A streamed request was never retried.

The providers are real HTTP servers on 127.0.0.1 that count their calls, so
the whole path runs: the chat route, the router, its retries and fallback.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web

from tests.factories import FAKE_COMPLETION, PROVIDER_A, PROVIDER_B

# A provider-qualified model has one candidate; an unqualified model is tried
# on every active provider, in turn.
_ONE_ROUTE = "test-provider:fake:model-v1"
_ANY_ROUTE = "pbtest-shared-model"

# llama.cpp before its exceed_context_size_error 400: a 500 server_error.
_TOO_LONG = json.dumps(
    {
        "error": {
            "code": 500,
            "message": "the request exceeds the available context size. "
            "try increasing the context size or enable context shift",
            "type": "server_error",
        }
    }
)

_STREAM = pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])


@pytest.fixture
def isolated(monkeypatch):
    """No retry delay; fresh breaker, alerts and provider order for the test."""
    import plugboard.alerts as alerts
    import plugboard.metrics as metrics
    import plugboard.router as router
    from plugboard import circuit_breaker as cb

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    monkeypatch.setattr(alerts, "_provider_alerted", {})
    # Equal load-balancing scores keep the candidates in providers.json order.
    monkeypatch.setattr(metrics, "_stats", {})
    return router


@pytest.fixture
async def serve(_providers_file):
    """Serve each provider record from its own local HTTP server.

    Returns one state per provider: the status and body it answers, and the
    number of calls it received.
    """
    from plugboard.router import invalidate_models_cache

    runners: list[web.AppRunner] = []

    async def start(*providers: dict) -> list[dict]:
        records, states = [], []
        for record in providers:
            state = {"status": 200, "body": json.dumps(FAKE_COMPLETION), "calls": 0}

            async def handle(request: web.Request, state=state) -> web.Response:
                state["calls"] += 1
                return web.Response(
                    status=state["status"],
                    text=state["body"],
                    content_type="application/json",
                )

            app = web.Application()
            app.router.add_post("/v1/chat/completions", handle)
            runner = web.AppRunner(app)
            await runner.setup()
            runners.append(runner)
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            records.append({**record, "base_url": f"http://127.0.0.1:{site.port}"})
            states.append(state)
        _providers_file.write_text(json.dumps({"providers": records}))
        invalidate_models_cache()
        return states

    try:
        yield start
    finally:
        for runner in runners:
            await runner.cleanup()


@pytest.fixture
def client_key(tmp_path, monkeypatch):
    """Authorization header of a plain client key (not the admin)."""
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    rec = ak.create_key("no-retry-client", "test")
    return {"Authorization": f"Bearer {rec['key']}"}


def _chat(model: str, stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        "cache": False,
    }


@pytest.mark.asyncio
@_STREAM
@pytest.mark.parametrize("status", [500, 502, 503, 529])
async def test_a_context_too_long_is_sent_once_then_answers_400(
    client, client_key, serve, isolated, status, stream
):
    (provider,) = await serve(PROVIDER_A)
    provider["status"] = status
    provider["body"] = _TOO_LONG

    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(_ONE_ROUTE, stream)
    )

    assert provider["calls"] == 1
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "test-provider" in detail and "rejected" in detail


@pytest.mark.asyncio
async def test_a_context_too_long_goes_straight_to_the_next_candidate(
    client, client_key, serve, isolated
):
    first, second = await serve(PROVIDER_A, PROVIDER_B)
    first["status"] = 500
    first["body"] = _TOO_LONG

    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(_ANY_ROUTE)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"
    assert first["calls"] == 1
    assert second["calls"] == 1


@pytest.mark.asyncio
async def test_a_context_too_long_on_every_candidate_answers_400(
    client, client_key, serve, isolated
):
    first, second = await serve(PROVIDER_A, PROVIDER_B)
    for provider in (first, second):
        provider["status"] = 500
        provider["body"] = _TOO_LONG

    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(_ANY_ROUTE)
    )

    assert resp.status_code == 400, resp.text
    assert "rejected" in resp.json()["detail"]
    assert (first["calls"], second["calls"]) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (500, json.dumps({"error": {"message": "worker crashed"}}), 502),
        (503, "Service Unavailable", 502),
        (429, json.dumps({"error": {"message": "rate limited"}}), 429),
    ],
    ids=["500", "503-text", "429"],
)
async def test_another_retryable_error_is_still_retried(
    client, client_key, serve, isolated, status, body, expected
):
    (provider,) = await serve(PROVIDER_A)
    provider["status"] = status
    provider["body"] = body

    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(_ONE_ROUTE)
    )

    assert provider["calls"] == 1 + len(isolated._DELAYS)
    assert resp.status_code == expected, resp.text
