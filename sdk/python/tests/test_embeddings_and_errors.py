"""SDK 0.2.0 (PLU-21): embeddings, and errors that carry the gateway's code
and Retry-After. A disposable HTTP server on 127.0.0.1, no real gateway."""

from __future__ import annotations

import json
import re
import threading
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import plugboard_sdk
from plugboard_sdk import PlugboardClient, PlugboardError, PlugboardTimeoutError

KEY = "pb_test_only"
VECTORS = {
    "object": "list",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
    "model": "ollama:nomic-embed-text",
    "usage": {"prompt_tokens": 2, "total_tokens": 2},
}
BUSY = {
    "detail": "Every provider that serves this model is busy: retry in 5 seconds.",
    "code": "providers_busy",
    "retry_after": 5,
}


class Gateway:
    """Answers each request with the next queued (status, body, headers, delay)."""

    def __init__(self):
        self.seen = []
        self.answers = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                gateway.seen.append(
                    (
                        self.path,
                        self.headers.get("Authorization"),
                        self.headers.get("User-Agent"),
                        json.loads(raw),
                    )
                )
                status, body, headers, delay = gateway.answers.pop(0)
                time.sleep(delay)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(data)))
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # the client gave up (timeout tests)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def answer(self, status=200, body=None, headers=None, delay=0.0):
        self.answers.append(
            (status, {} if body is None else body, headers or {}, delay)
        )


@pytest.fixture
def gateway():
    gw = Gateway()
    worker = threading.Thread(
        target=gw.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    worker.start()
    try:
        yield gw
    finally:
        gw.server.shutdown()
        gw.server.server_close()
        worker.join()


# ── Embeddings ───────────────────────────────────────────────────────────────


def test_embeddings_posts_the_inputs_with_the_key(gateway):
    gateway.answer(body=VECTORS)
    gateway.answer(body=VECTORS)
    client = PlugboardClient(KEY, base_url=gateway.url)

    assert client.embeddings("ollama:nomic-embed-text", "hello") == VECTORS
    client.embeddings(
        "ollama:nomic-embed-text", ["a", "b"], encoding_format="base64", user="u1"
    )

    (path, auth, agent, body), (_, _, _, second) = gateway.seen
    assert (path, auth) == ("/v1/embeddings", f"Bearer {KEY}")
    assert agent == "plugboard-python-sdk/0.2"
    assert body == {"model": "ollama:nomic-embed-text", "input": "hello"}
    assert second == {
        "model": "ollama:nomic-embed-text",
        "input": ["a", "b"],
        "encoding_format": "base64",
        "user": "u1",
    }


def test_embeddings_refuses_a_redirect(gateway):
    gateway.answer(307, {}, {"Location": "http://third-party.invalid/steal"})
    with pytest.raises(PlugboardError) as error:
        PlugboardClient(KEY, base_url=gateway.url).embeddings("m", "x")
    assert (error.value.status, error.value.detail) == (307, "Redirect refused")


# ── Errors: code and Retry-After ─────────────────────────────────────────────


def test_a_busy_gateway_gives_code_and_retry_after(gateway):
    gateway.answer(503, BUSY, {"Retry-After": "5"})
    with pytest.raises(PlugboardError) as error:
        PlugboardClient(KEY, base_url=gateway.url).embeddings("m", "x")
    err = error.value
    assert (err.status, err.code, err.retry_after) == (503, "providers_busy", 5.0)
    assert "busy" in err.detail  # the body, as in 0.1.0
    assert str(err).startswith("HTTP 503: ")


@pytest.mark.parametrize(
    "body, headers, code, retry_after",
    [
        ({"detail": "slow down", "retry_after": 7}, {}, None, 7.0),
        (
            {"error": {"code": "rate_limit_exceeded", "message": "x"}},
            {},
            "rate_limit_exceeded",
            None,
        ),
        (
            {"detail": {"code": "quota", "msg": "x"}},
            {"Retry-After": "2.5"},
            "quota",
            2.5,
        ),
        (b"<html>bad gateway</html>", {"Retry-After": "soon"}, None, None),
        ({"code": 42, "retry_after": True}, {"Retry-After": "-3"}, None, None),
    ],
    ids=["body-retry", "openai-code", "detail-code", "not-json", "invalid"],
)
def test_error_fields_are_read_from_the_body_and_headers(
    gateway, body, headers, code, retry_after
):
    gateway.answer(429, body, headers)
    with pytest.raises(PlugboardError) as error:
        PlugboardClient(KEY, base_url=gateway.url).chat("m", [])
    assert (error.value.code, error.value.retry_after) == (code, retry_after)


def test_retry_after_as_an_http_date(gateway):
    later = datetime.now(timezone.utc) + timedelta(seconds=30)
    gateway.answer(
        429, {"detail": "x"}, {"Retry-After": format_datetime(later, usegmt=True)}
    )
    with pytest.raises(PlugboardError) as error:
        PlugboardClient(KEY, base_url=gateway.url).chat("m", [])
    assert 20 <= error.value.retry_after <= 31


def test_a_streamed_chat_refused_busy_gives_the_same_fields(gateway):
    gateway.answer(503, BUSY, {"Retry-After": "5"})
    client = PlugboardClient(KEY, base_url=gateway.url)
    with pytest.raises(PlugboardError) as error:
        list(client.chat_stream("m", [{"role": "user", "content": "hi"}]))
    assert (error.value.code, error.value.retry_after) == ("providers_busy", 5.0)


def test_decisions_keep_their_message_and_gain_the_fields(gateway):
    gateway.answer(
        429, {"detail": "private text", "code": "decision_quota"}, {"Retry-After": "9"}
    )
    with pytest.raises(PlugboardError) as error:
        PlugboardClient(KEY, base_url=gateway.url).decide("jev", "s", {})
    err = error.value
    assert (err.status, err.detail) == (429, "Decision request failed")
    assert (err.code, err.retry_after) == ("decision_quota", 9.0)


def test_errors_stay_positional_and_compatible():
    err = PlugboardError(500, "boom")
    assert (err.status, err.detail, err.code, err.retry_after) == (
        500,
        "boom",
        None,
        None,
    )
    assert str(err) == "HTTP 500: boom"
    assert plugboard_sdk.LLMEnablerError is PlugboardError


# ── Timeouts and connection failures ─────────────────────────────────────────


@pytest.mark.parametrize("operation", ["embeddings", "chat", "chat_stream_raw"])
def test_a_timeout_is_a_plugboard_error_and_a_timeout_error(gateway, operation):
    gateway.answer(200, VECTORS, delay=1.0)
    client = PlugboardClient(KEY, base_url=gateway.url, timeout=0.2)
    calls = {
        "embeddings": lambda: client.embeddings("m", "x"),
        "chat": lambda: client.chat("m", []),
        "chat_stream_raw": lambda: list(client.chat_stream_raw("m", [])),
    }
    with pytest.raises(PlugboardTimeoutError) as error:
        calls[operation]()
    err = error.value
    assert isinstance(err, PlugboardError) and isinstance(err, TimeoutError)
    assert (err.status, err.code) == (0, "timeout")
    assert KEY not in str(err)


def test_a_refused_connection_has_its_code(gateway):
    url = gateway.url
    gateway.server.shutdown()
    gateway.server.server_close()
    with pytest.raises(PlugboardError) as error:
        PlugboardClient(KEY, base_url=url).embeddings("m", "x")
    assert (error.value.status, error.value.code) == (0, "connection_failed")
    assert not isinstance(error.value, TimeoutError)


def test_decision_timeouts_keep_their_message(monkeypatch):
    from plugboard_sdk import client as sdk

    def hang(*args, **kwargs):
        raise TimeoutError("SYNTHETIC")

    monkeypatch.setattr(sdk, "urlopen", hang)
    with pytest.raises(PlugboardTimeoutError) as error:
        PlugboardClient(KEY).decide("jev", "s", {})
    assert error.value.detail == "Decision request timed out"


# ── Async client ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_embeddings_and_busy_fields(gateway):
    from plugboard_sdk import PlugboardAsyncClient

    gateway.answer(body=VECTORS)
    gateway.answer(503, BUSY, {"Retry-After": "5"})
    async with PlugboardAsyncClient(KEY, base_url=gateway.url) as client:
        assert await client.embeddings("m", ["a"], encoding_format="float") == VECTORS
        with pytest.raises(PlugboardError) as error:
            await client.embeddings("m", "x")
    err = error.value
    assert (err.status, err.code, err.retry_after) == (503, "providers_busy", 5.0)
    assert err.detail == "Embedding request failed"
    path, auth, agent, body = gateway.seen[0]
    assert (path, auth, body) == (
        "/v1/embeddings",
        f"Bearer {KEY}",
        {"model": "m", "input": ["a"], "encoding_format": "float"},
    )
    assert agent == "plugboard-python-sdk/0.2"


@pytest.mark.asyncio
async def test_async_decisions_keep_their_messages_and_gain_the_fields(gateway):
    from plugboard_sdk import PlugboardAsyncClient

    gateway.answer(429, {"code": "decision_quota"}, {"Retry-After": "9"})
    async with PlugboardAsyncClient(KEY, base_url=gateway.url) as client:
        with pytest.raises(PlugboardError) as error:
            await client.decide("jev", "s", {})
    err = error.value
    assert (err.detail, err.code, err.retry_after) == (
        "Decision request failed",
        "decision_quota",
        9.0,
    )


@pytest.mark.asyncio
async def test_async_timeout_is_a_timeout_error(gateway):
    from plugboard_sdk import PlugboardAsyncClient

    gateway.answer(200, VECTORS, delay=1.0)
    async with PlugboardAsyncClient(KEY, base_url=gateway.url, timeout=0.2) as client:
        with pytest.raises(PlugboardTimeoutError) as error:
            await client.embeddings("m", "x")
    assert error.value.detail == "Embedding request timed out"
    assert isinstance(error.value, TimeoutError)


# ── Version ──────────────────────────────────────────────────────────────────


def test_version_is_0_2_0_everywhere():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M)
    assert declared.group(1) == plugboard_sdk.__version__ == "0.2.0"
