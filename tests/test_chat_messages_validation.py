"""POST /v1/chat/completions refuses unusable messages with a 400.

Before: the request reached the provider and the caller got a 502 with the
upstream parser error (Ollama) or the CLI's "Empty prompt" message.
"""

from __future__ import annotations

import pytest

AUTH = {"Authorization": "Bearer testkey"}
MODEL = "test-provider:fake:model-v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages, detail",
    [
        ([], "messages must be a non-empty array"),
        ("bad", "messages must be a non-empty array"),
        (None, "messages must be a non-empty array"),
        (["hi"], "messages[0] must be an object"),
        ([{"content": "hi"}], "messages[0].role must be a non-empty string"),
        ([{"role": "user", "content": 3}], "messages[0].content must be a string"),
    ],
)
async def test_bad_messages_are_400(client, messages, detail):
    body = {"model": MODEL}
    if messages is not None:
        body["messages"] = messages
    for path in ("/v1/chat/completions", "/v2/chat/completions"):
        resp = await client.post(path, json=body, headers=AUTH)
        assert resp.status_code == 400, (path, resp.text)
        assert resp.json()["detail"].startswith(detail)


@pytest.mark.asyncio
async def test_valid_messages_still_reach_the_provider(client):
    from aioresponses import aioresponses

    from tests.factories import FAKE_COMPLETION

    with aioresponses() as mock:
        mock.post("http://fake-llm.test/v1/chat/completions", payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hi"}]}
                ],
            },
            headers=AUTH,
        )
    assert resp.status_code == 200, resp.text
