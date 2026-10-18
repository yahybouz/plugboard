"""Minimal contract tests for the SDK — no live network."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from plugboard_sdk import LLMEnablerClient, LLMEnablerError


def _make_urlopen_mock(response_body: bytes, status: int = 200):
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = response_body
    cm.__enter__.return_value.getcode.return_value = status
    return MagicMock(return_value=cm)


def test_constructor_rejects_empty_key():
    with pytest.raises(ValueError):
        LLMEnablerClient(api_key="")


def test_chat_sends_bearer_and_model():
    client = LLMEnablerClient(api_key="lle_test", base_url="http://x")

    with patch("plugboard_sdk.client.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "hi"}}]}
        ).encode()
        reply = client.chat("m1", [{"role": "user", "content": "hi"}])
        assert reply["choices"][0]["message"]["content"] == "hi"

    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.full_url == "http://x/v1/chat/completions"
    assert sent_request.headers["Authorization"] == "Bearer lle_test"
    body = json.loads(sent_request.data)
    assert body["model"] == "m1"
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_includes_session_id_when_provided():
    client = LLMEnablerClient(api_key="lle_test", base_url="http://x")

    with patch("plugboard_sdk.client.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
        client.chat("m1", [{"role": "user", "content": "q"}], session_id="c42")

    body = json.loads(mock_urlopen.call_args[0][0].data)
    assert body["session_id"] == "c42"


def test_chat_raises_on_http_error():
    from urllib.error import HTTPError

    client = LLMEnablerClient(api_key="lle_test", base_url="http://x")
    with patch("plugboard_sdk.client.urlopen") as mock_urlopen:
        err = HTTPError(
            "http://x/v1/chat/completions", 429, "Too Many", {}, MagicMock()
        )
        err.read = lambda: b"rate limit"
        mock_urlopen.side_effect = err
        with pytest.raises(LLMEnablerError) as exc:
            client.chat("m1", [{"role": "user", "content": "q"}])
        assert exc.value.status == 429
        assert "rate limit" in exc.value.detail


def test_chat_stream_yields_content_deltas():
    """SSE-style payload: text deltas are yielded, `[DONE]` ends the loop."""
    client = LLMEnablerClient(api_key="lle_test", base_url="http://x")

    sse_lines = [
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
        b"data: [DONE]\n",
    ]

    response_mock = MagicMock()
    response_mock.__iter__.return_value = iter(sse_lines)

    cm = MagicMock()
    cm.__enter__.return_value = response_mock

    with patch("plugboard_sdk.client.urlopen", return_value=cm):
        out = list(client.chat_stream("m1", [{"role": "user", "content": "hi"}]))

    assert "".join(out) == "Hello"


def test_list_models_returns_data_array():
    client = LLMEnablerClient(api_key="lle_test", base_url="http://x")
    with patch("plugboard_sdk.client.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(
            {"data": [{"id": "m1"}, {"id": "m2"}]}
        ).encode()
        models = client.list_models()
    assert [m["id"] for m in models] == ["m1", "m2"]
