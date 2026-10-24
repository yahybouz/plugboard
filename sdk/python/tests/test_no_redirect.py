"""The SDK never follows a redirect: the bearer stays on the configured origin."""

from __future__ import annotations

import email.message
import io
import urllib.request
import urllib.response

import pytest

from plugboard_sdk import client as sdk

SECRET = "pb_synthetic_secret"
TARGET = "http://third-party.invalid/steal"
MESSAGES = [{"role": "user", "content": "synthetic"}]

OPERATIONS = {
    "chat": lambda c: c.chat("synthetic-model", MESSAGES),
    "chat_stream_raw": lambda c: list(c.chat_stream_raw("synthetic-model", MESSAGES)),
    "list_models": lambda c: c.list_models(),
    "usage": lambda c: c.usage(),
}


@pytest.fixture
def redirecting(monkeypatch):
    seen = []

    def fake_open(self, req, code):
        seen.append((req.host, req.get_method()))
        headers = email.message.Message()
        headers["Location"] = TARGET
        response = urllib.response.addinfourl(
            io.BytesIO(b"Moved to " + TARGET.encode()), headers, req.full_url, code
        )
        response.msg = "Redirect"
        return response

    def install(code):
        monkeypatch.setattr(
            urllib.request.HTTPHandler,
            "http_open",
            lambda self, req: fake_open(self, req, code),
        )
        return seen

    return install


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_sync_client_refuses_every_redirect(redirecting, operation, code):
    seen = redirecting(code)
    client = sdk.PlugboardClient(SECRET, base_url="http://origin.invalid")
    with pytest.raises(sdk.PlugboardError) as error:
        OPERATIONS[operation](client)
    assert [host for host, _ in seen] == ["origin.invalid"]
    assert error.value.status == code
    assert error.value.detail == "Redirect refused"
    assert SECRET not in str(error.value)
    assert "third-party" not in str(error.value)


async def test_async_client_does_not_follow_redirects(monkeypatch):
    import httpx

    from plugboard_sdk import PlugboardAsyncClient

    seen = []

    async def fake_send(self, request):
        seen.append((request.url.host, request.headers.get("Authorization")))
        return httpx.Response(302, headers={"Location": TARGET}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_send)
    async with PlugboardAsyncClient(SECRET, base_url="http://origin.invalid") as client:
        for call in (
            client.list_decision_models(),
            client.decide("jev", "state", {"q": {"type": "noul", "instructions": "x"}}),
        ):
            with pytest.raises(sdk.PlugboardError) as error:
                await call
            assert error.value.status == 302
            assert SECRET not in str(error.value)
    assert [host for host, _ in seen] == ["origin.invalid", "origin.invalid"]
