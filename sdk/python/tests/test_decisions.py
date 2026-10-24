"""Decision SDK boundary tests using a disposable local HTTP fixture, no ML."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plugboard_sdk import PlugboardClient

QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": "Pick route",
        "criteria": {"yes": "Proceed", "no": None},
    }
}
REPLY = {
    "model": "jev",
    "answers": {
        "route": {
            "type": "choice",
            "choice": "yes",
            "confidence": 0.8,
            "probabilities": {"yes": 0.8, "no": 0.2},
        }
    },
    "billing": {"status": "billed", "cost_usd": 5.04e-06},
}


@pytest.fixture
def gateway():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            body = (
                json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if self.command == "POST"
                else None
            )
            seen.append(
                (self.command, self.path, self.headers.get("Authorization"), body)
            )
            response = (
                {"data": [{"id": "jev", "available": False}]}
                if self.command == "GET"
                else REPLY
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_sync_decision_routes_preserve_named_questions_and_client_auth(gateway):
    url, seen = gateway
    client = PlugboardClient("pb_test_only", base_url=url)
    assert client.list_decision_models() == [{"id": "jev", "available": False}]
    assert client.decide("jev", "Synthetic state", QUESTIONS) == REPLY
    assert seen == [
        ("GET", "/v1/decisions/models", "Bearer pb_test_only", None),
        (
            "POST",
            "/v1/decisions",
            "Bearer pb_test_only",
            {"model": "jev", "state": "Synthetic state", "questions": QUESTIONS},
        ),
    ]


@pytest.mark.asyncio
async def test_async_decision_routes_preserve_named_questions_and_client_auth(gateway):
    import plugboard_sdk

    url, seen = gateway
    client_type = getattr(plugboard_sdk, "PlugboardAsyncClient", None)
    assert client_type is not None, "The SDK must expose its async decision client"
    async with client_type("pb_test_only", base_url=url) as client:
        assert await client.list_decision_models() == [
            {"id": "jev", "available": False}
        ]
        assert await client.decide("jev", "Synthetic state", QUESTIONS) == REPLY
    assert seen[1] == (
        "POST",
        "/v1/decisions",
        "Bearer pb_test_only",
        {"model": "jev", "state": "Synthetic state", "questions": QUESTIONS},
    )


@pytest.mark.parametrize("operation", ["decide", "list_decision_models"])
@pytest.mark.parametrize("phase", ["open", "read"])
def test_sync_decision_socket_timeout_is_closed_during_open_or_read(
    monkeypatch, operation, phase
):
    import socket
    from plugboard_sdk import client as sdk

    class TimedOutResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            raise socket.timeout("SYNTHETIC-PRIVATE-SOCKET-TEXT")

    def timeout(*args, **kwargs):
        if phase == "open":
            raise socket.timeout("SYNTHETIC-PRIVATE-SOCKET-TEXT")
        return TimedOutResponse()

    monkeypatch.setattr(sdk, "urlopen", timeout)
    client = sdk.PlugboardClient("synthetic")
    with pytest.raises(sdk.PlugboardError) as error:
        if operation == "decide":
            client.decide("jev", "Synthetic state", QUESTIONS)
        else:
            client.list_decision_models()
    assert error.value.status == 0
    assert error.value.detail == "Decision request timed out"
    assert "SYNTHETIC-PRIVATE" not in str(error.value)


def test_removed_kev_is_rejected_before_sync_sdk_transport(monkeypatch):
    client = PlugboardClient("synthetic")

    def never(*args, **kwargs):
        pytest.fail("Removed Kev must not reach the gateway")

    monkeypatch.setattr(client, "_post_json", never)
    with pytest.raises(ValueError, match="Unsupported decision model"):
        client.decide("kev-0.8b", "Synthetic state", QUESTIONS)


@pytest.mark.asyncio
async def test_removed_kev_is_rejected_before_async_sdk_transport(monkeypatch):
    from plugboard_sdk import PlugboardAsyncClient

    async with PlugboardAsyncClient("synthetic") as client:

        async def never(*args, **kwargs):
            pytest.fail("Removed Kev must not reach the gateway")

        monkeypatch.setattr(client, "_request", never)
        with pytest.raises(ValueError, match="Unsupported decision model"):
            await client.decide("kev-0.8b", "Synthetic state", QUESTIONS)


@pytest.mark.parametrize("model", ["jev", "laya", "laya-multilingual", "future-engine"])
def test_sync_sdk_preserves_other_decision_ids_for_gateway_validation(
    monkeypatch, model
):
    client = PlugboardClient("synthetic")
    seen = []

    def post(path, body):
        seen.append((path, body))
        return {**REPLY, "model": model}

    monkeypatch.setattr(client, "_post_json", post)
    assert client.decide(model, "Synthetic state", QUESTIONS)["model"] == model
    assert seen == [
        (
            "/v1/decisions",
            {"model": model, "state": "Synthetic state", "questions": QUESTIONS},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["jev", "laya", "laya-multilingual", "future-engine"])
async def test_async_sdk_preserves_other_decision_ids_for_gateway_validation(
    monkeypatch, model
):
    from plugboard_sdk import PlugboardAsyncClient

    seen = []
    async with PlugboardAsyncClient("synthetic") as client:

        async def request(method, path, **kwargs):
            seen.append((method, path, kwargs))
            return {**REPLY, "model": model}

        monkeypatch.setattr(client, "_request", request)
        assert (await client.decide(model, "Synthetic state", QUESTIONS))[
            "model"
        ] == model
    assert seen == [
        (
            "POST",
            "/v1/decisions",
            {
                "json": {
                    "model": model,
                    "state": "Synthetic state",
                    "questions": QUESTIONS,
                }
            },
        )
    ]
