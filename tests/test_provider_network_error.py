"""A network error toward an HTTP provider never reaches a client key.

Before the fix, a refused connection came back to every caller as
``[provider] network error: Cannot connect to host <host>:<port> ...``, and in
a stream as the bare aiohttp text: the address of the provider, an internal
one for a local Ollama or a private endpoint. A client key now gets a message
written by the gateway and an ``error_ref`` to find the full error in the
gateway log; the admin keeps the detail, as for an HTTP error (BUG-35).
"""

from __future__ import annotations

import json
import socket

import aiohttp
import pytest

from tests.factories import PROVIDER_A

_MODEL = "test-provider:fake:model-v1"
ADMIN = {"Authorization": "Bearer testkey"}  # the admin key in the fixture
_STREAM = pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])


def _closed_port() -> int:
    """A local port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def unreachable(_providers_file):
    """test-provider pointed at a closed port on 127.0.0.1; yields the port."""
    port = _closed_port()
    _providers_file.write_text(
        json.dumps(
            {"providers": [{**PROVIDER_A, "base_url": f"http://127.0.0.1:{port}"}]}
        )
    )
    from plugboard.router import invalidate_models_cache

    invalidate_models_cache()
    return port


@pytest.fixture
def client_key(tmp_path, monkeypatch):
    """Authorization header of a plain client key (not the admin)."""
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    rec = ak.create_key("network-error-client", "test")
    return {"Authorization": f"Bearer {rec['key']}"}


def _chat(stream: bool) -> dict:
    return {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
        "cache": False,
    }


@pytest.mark.asyncio
@_STREAM
async def test_refused_connection_gives_a_reference_to_a_client_key(
    client, client_key, unreachable, stream
):
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(stream)
    )

    assert resp.status_code == 502, resp.text
    for word in ("127.0.0.1", str(unreachable), "Cannot connect", "Connect call"):
        assert word not in resp.text, resp.text
    body = resp.json()
    assert body["error_ref"]
    assert "test-provider" in body["detail"]
    assert body["error_ref"] in body["detail"]


@pytest.mark.asyncio
@_STREAM
async def test_admin_keeps_the_network_error_detail(client, unreachable, stream):
    resp = await client.post("/v1/chat/completions", headers=ADMIN, json=_chat(stream))

    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert f"127.0.0.1:{unreachable}" in body["detail"]
    assert body["error_ref"]


class _ConnectTimeoutSession:
    """A pooled session whose connection times out, as aiohttp reports it."""

    def post(self, url, **kwargs):
        raise aiohttp.ConnectionTimeoutError(f"Connection timeout to host {url}")


@pytest.mark.asyncio
async def test_stream_connect_timeout_hides_the_provider_address(
    client, client_key, unreachable, monkeypatch
):
    # aiohttp names the whole URL; the word "timeout" sent it to the 503
    # branch with that text as the detail.
    async def _session():
        return _ConnectTimeoutSession()

    monkeypatch.setattr("plugboard.http_pool.get_session", _session)
    resp = await client.post(
        "/v1/chat/completions", headers=client_key, json=_chat(True)
    )

    assert resp.status_code == 503, resp.text
    assert "127.0.0.1" not in resp.text and str(unreachable) not in resp.text
    assert "test-provider" in resp.json()["detail"]
