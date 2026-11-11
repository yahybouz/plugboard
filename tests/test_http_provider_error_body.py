"""An HTTP provider's error body never reaches a client key (BUG-35).

Before the fix, a non-429 error from an HTTP provider (400, 401, 403, 500...)
reached POST /v1/chat/completions as ``[provider] HTTP <status>: <body>``,
streamed or not: the provider's own text, which can name the account or the
organisation, went to every caller. A client key now gets a status that says
whose fault it is (400 when the provider rejected the request itself, 502
otherwise), a reason written by the gateway and an ``error_ref`` to find the
full error in the gateway log; the admin keeps the upstream detail, as for a
CLI provider (4.5.0).

A context too long is the request's fault whatever the provider's format
(BUG-40): llama.cpp, Ollama, Gemini or a 5xx, by the circuit breaker's own
rule, so the client gets 400 where the breaker does not count the failure.

The provider is a real HTTP server on 127.0.0.1, so the whole path runs:
router, retries, the chat route and its error handling.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web

from tests.factories import PROVIDER_A

_MODEL = "test-provider:fake:model-v1"
_SECRET = "upstream-secret org-42 acct_7731"
ADMIN = {"Authorization": "Bearer testkey"}  # the admin key in the fixture


def _upstream_body(
    message: str = _SECRET,
    type_: str = "invalid_request_error",
    code: str | None = None,
) -> str:
    """An OpenAI-style error body (``error.type``, ``error.code``)."""
    return json.dumps({"error": {"message": message, "type": type_, "code": code}})


def _gemini_body(message: str, status: str) -> str:
    """The error body of Gemini's OpenAI-compatible endpoint: a list, no type."""
    return json.dumps(
        [{"error": {"code": 400, "message": f"{message} {_SECRET}", "status": status}}]
    )


@pytest.fixture
def no_retry_delay(monkeypatch):
    import plugboard.router as router

    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])


@pytest.fixture
async def provider(_providers_file, monkeypatch):
    """test-provider served by a local HTTP server answering `state`."""
    state = {"status": 500, "body": _upstream_body(), "calls": 0}

    async def handle(request: web.Request) -> web.Response:
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
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    _providers_file.write_text(
        json.dumps(
            {"providers": [{**PROVIDER_A, "base_url": f"http://127.0.0.1:{site.port}"}]}
        )
    )
    from plugboard.router import invalidate_models_cache

    invalidate_models_cache()
    try:
        yield state
    finally:
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
    rec = ak.create_key("http-error-client", "test")
    return {"Authorization": f"Bearer {rec['key']}"}


def _chat(stream: bool) -> dict:
    return {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        "cache": False,
    }


def _assert_no_upstream_text(resp) -> None:
    for word in ("upstream-secret", "org-42", "acct_7731", "invalid_request"):
        assert word not in resp.text, resp.text


_STREAM = pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])


@pytest.mark.asyncio
@_STREAM
@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
async def test_provider_failure_gives_502_and_a_reference_to_a_client_key(
    client, client_key, provider, no_retry_delay, status, stream
):
    provider["status"] = status
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream)
    )

    assert provider["calls"] >= 1
    assert resp.status_code == 502, resp.text
    _assert_no_upstream_text(resp)
    body = resp.json()
    assert body["error_ref"]
    assert "test-provider" in body["detail"]
    assert body["error_ref"] in body["detail"]


@pytest.mark.asyncio
@_STREAM
async def test_upstream_timeout_text_is_not_sent_to_a_client_key(
    client, client_key, provider, stream
):
    # The word "timeout" in the provider's body sent it to the 503 branch
    # meant for the gateway's own timeout, with the body as the detail.
    provider["status"] = 504
    provider["body"] = _upstream_body(f"gateway timeout, {_SECRET}")
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream)
    )

    assert resp.status_code == 502, resp.text
    _assert_no_upstream_text(resp)
    assert resp.json()["error_ref"]


_CONTEXT_TOO_LONG = _upstream_body(
    f"This model's maximum context length is 8192 tokens. {_SECRET}",
    code="context_length_exceeded",
)


@pytest.mark.asyncio
@_STREAM
@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, _CONTEXT_TOO_LONG),
        # Only the code names the request (no OpenAI type).
        (400, _upstream_body(_SECRET, type_="", code="context_length_exceeded")),
        (400, _upstream_body(_SECRET, code="unsupported_parameter")),
        (422, _CONTEXT_TOO_LONG),
        # 413 names the request by its status, whatever the body.
        (413, f"<html>413 Request Entity Too Large {_SECRET}</html>"),
    ],
    ids=["400-context", "400-code-only", "400-param", "422", "413-html"],
)
async def test_request_rejected_by_the_provider_gives_400_to_a_client_key(
    client, client_key, provider, status, body, stream
):
    provider["status"] = status
    provider["body"] = body
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream)
    )

    assert provider["calls"] == 1
    assert resp.status_code == 400, resp.text
    _assert_no_upstream_text(resp)
    assert "maximum context length" not in resp.text
    body = resp.json()
    assert body["error_ref"]
    detail = body["detail"]
    assert "test-provider" in detail and "rejected" in detail
    assert "context" in detail  # tells the client what to look at


# BUG-40: a context too long in another format than OpenAI's (no
# invalid_request_error type, no context_length_exceeded code on a 400, or a
# 5xx). The circuit breaker already judged them caused by the request; the
# client got 502 "The X provider failed".
_CONTEXT_TOO_LONG_ANY_FORMAT = [
    pytest.param(
        400,
        json.dumps(
            {
                "error": {
                    "code": 400,
                    "message": "the request exceeds the available context size, "
                    f"try increasing it {_SECRET}",
                    "type": "exceed_context_size_error",
                    "n_prompt_tokens": 9000,
                    "n_ctx": 8192,
                }
            }
        ),
        id="llamacpp-400",
    ),
    pytest.param(
        500,
        json.dumps(
            {
                "error": {
                    "code": 500,
                    "message": "the request exceeds the available context size. "
                    f"try increasing the context size or enable context shift {_SECRET}",
                    "type": "server_error",
                }
            }
        ),
        id="llamacpp-500",
    ),
    pytest.param(
        400, json.dumps({"error": f"prompt is too long {_SECRET}"}), id="ollama-400"
    ),
    pytest.param(
        400,
        json.dumps(
            {
                "error": "prompt too long; exceeded max context length by 812 "
                f"tokens {_SECRET}"
            }
        ),
        id="ollama-400-exceeded",
    ),
    pytest.param(
        500,
        _upstream_body(
            f"This model's maximum context length is 8192 tokens. {_SECRET}",
            type_="server_error",
            code="context_length_exceeded",
        ),
        id="500-context-length-exceeded",
    ),
    pytest.param(
        400,
        _gemini_body(
            "The input token count (9000) exceeds the maximum number of tokens "
            "allowed (8192).",
            "INVALID_ARGUMENT",
        ),
        id="gemini-400-tokens",
    ),
    pytest.param(400, f"prompt is too long {_SECRET}", id="text-400"),
]


@pytest.fixture
def breaker(monkeypatch):
    """A fresh circuit breaker and provider alert count for the test."""
    import plugboard.alerts as alerts
    from plugboard import circuit_breaker as cb

    monkeypatch.setattr(cb, "_circuits", {})
    monkeypatch.setattr(alerts, "_provider_failures", {})
    return cb


def _circuit_failures(cb) -> int:
    circuit = cb._circuits.get(_MODEL)
    return circuit.failures if circuit else 0


@pytest.mark.asyncio
@_STREAM
@pytest.mark.parametrize(("status", "body"), _CONTEXT_TOO_LONG_ANY_FORMAT)
async def test_context_too_long_in_any_format_gives_400_to_a_client_key(
    client, client_key, provider, no_retry_delay, breaker, status, body, stream
):
    provider["status"] = status
    provider["body"] = body
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream)
    )

    assert provider["calls"] >= 1
    assert resp.status_code == 400, resp.text
    _assert_no_upstream_text(resp)
    for upstream in ("context size", "too long", "maximum number of tokens"):
        assert upstream not in resp.text, resp.text
    body = resp.json()
    assert body["error_ref"]
    detail = body["detail"]
    assert "test-provider" in detail and "rejected" in detail
    assert "context" in detail
    # One rule for both: the route's circuit does not count it either.
    assert _circuit_failures(breaker) == 0


@pytest.mark.asyncio
@_STREAM
async def test_a_fragment_added_to_the_breaker_rule_reaches_the_client_status(
    client, client_key, provider, breaker, monkeypatch, stream
):
    # The router has no list of its own: the breaker's is the only one.
    monkeypatch.setattr(
        breaker, "_REQUEST_TOO_LONG", (*breaker._REQUEST_TOO_LONG, "pbtest-oversize")
    )
    provider["status"] = 400
    provider["body"] = json.dumps({"error": f"pbtest-oversize {_SECRET}"})
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream)
    )

    assert resp.status_code == 400, resp.text
    _assert_no_upstream_text(resp)
    assert _circuit_failures(breaker) == 0


@pytest.mark.asyncio
@_STREAM
@pytest.mark.parametrize(
    ("status", "body"),
    [
        # Gemini answers 400 for its own key and for a refused region.
        (400, _gemini_body("API key not valid.", "INVALID_ARGUMENT")),
        (400, _gemini_body("User location is not supported.", "FAILED_PRECONDITION")),
        (400, f"Bad Request {_SECRET}"),
        (400, _upstream_body(_SECRET, code="invalid_api_key")),
        (422, json.dumps({"detail": [{"msg": _SECRET}]})),
    ],
    ids=["gemini-key", "gemini-region", "text", "openai-key", "422-untyped"],
)
async def test_a_4xx_that_does_not_name_the_request_is_a_provider_failure(
    client, client_key, provider, breaker, status, body, stream
):
    # The status alone does not say whose fault it is: without an error type
    # or code naming the request, the client is not told to fix its request
    # (a 400 is not retried by the OpenAI SDKs).
    provider["status"] = status
    provider["body"] = body
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream)
    )

    assert provider["calls"] == 1
    assert resp.status_code == 502, resp.text
    _assert_no_upstream_text(resp)
    assert "API key" not in resp.text and "location" not in resp.text
    detail = resp.json()["detail"]
    assert "rejected" not in detail and "context" not in detail
    assert resp.json()["error_ref"] in detail
    # A 4xx still does not count against the route's circuit (PLU-3).
    assert _circuit_failures(breaker) == 0


@pytest.mark.asyncio
@_STREAM
@pytest.mark.parametrize(("status", "expected"), [(401, 502), (400, 400)])
async def test_admin_keeps_the_upstream_detail(
    client, provider, no_retry_delay, status, expected, stream
):
    provider["status"] = status
    resp = await client.post("/v1/chat/completions", headers=ADMIN, json=_chat(stream))

    assert resp.status_code == expected, resp.text
    body = resp.json()
    assert "upstream-secret org-42" in body["detail"]
    assert f"HTTP {status}" in body["detail"]
    assert body["error_ref"]
