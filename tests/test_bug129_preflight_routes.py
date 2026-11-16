"""BUG-129: /v1/preflight checks every route chat would use for the model.

The preflight checked a key's allowed_models on the first route the model
resolved to, as the info page did before PLU-60. A spelling that reaches
other providers first (an upper-case alias, a namespace no provider owns)
passed that check: the item then went through the price check and the rpm
count, and the key's refusal came only from the ping. A refused item spent
the key's rate limit, and the next item of the batch was refused for it.

Now the key's check reads the routes chat would use, in its order (the
first resolved route, then router.chat_routes): a refused item is answered
``forbidden_model`` at the key's check, before the price, the rpm count, the
budget hold and any provider. The answer's shape is unchanged.
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from tests.factories import FAKE_COMPLETION
from tests.test_allowed_models_published_id import (  # noqa: F401 (fixtures)
    _THREE_CLIS,
    _auth,
    _chat,
    cli_calls,
    keys,
    registry,
)

ALLOWED = ["claude-code:claude-sonnet-4-6"]

# (provider, model) as a preflight item names them. The upper-case alias and
# the namespace no provider owns resolve to the Claude CLI first, but chat
# sends them to test-provider first (it takes any name): chat refuses them.
_PAIRS = [
    ("claude-code", "claude-sonnet-4-6"),
    ("claude-via-claude-cli", "claude-sonnet-4-6"),
    ("CLAUDE-CODE", "claude-sonnet-4-6"),
    ("claude-code", "CLAUDE-SONNET-4-6"),
    ("anthropic", "claude-sonnet-4-6"),
    ("Claude", "claude-sonnet-4-6"),
    ("test-provider", "claude-sonnet-4-6"),
    ("other-provider", "claude-sonnet-4-6"),
    ("Codex", "gpt-6.1-sol"),
]


def _limited_key(ak, *, rpm_limit=0):
    rec = ak.create_key(
        "limited-client", "synthetic", allowed_models=ALLOWED, rpm_limit=rpm_limit
    )
    ak._invalidate_cache()
    return rec


def _mock_http(mock):
    for host in ("fake-llm.test", "other-llm.test"):
        mock.post(
            f"http://{host}/v1/chat/completions", payload=FAKE_COMPLETION, repeat=True
        )


async def _preflight(client, key, *pairs):
    resp = await client.post(
        "/v1/preflight",
        headers=_auth(key),
        json={"items": [{"provider": p, "model": m} for p, m in pairs]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["results"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pair", _PAIRS, ids=lambda p: f"{p[0]}:{p[1]}")
async def test_preflight_answers_as_chat(client, registry, keys, cli_calls, pair):  # noqa: F811
    registry(_THREE_CLIS)
    rec = _limited_key(keys)
    with aioresponses() as mock:
        _mock_http(mock)
        chat = await _chat(client, rec["key"], f"{pair[0]}:{pair[1]}")
        (result,) = await _preflight(client, rec["key"], pair)

    if chat.status_code == 200:
        assert result["ok"] is True, result
    else:
        assert chat.status_code == 403, chat.text
        assert result["ok"] is False
        assert result["error"] == "forbidden_model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refused",
    [("CLAUDE-CODE", "claude-sonnet-4-6"), ("anthropic", "claude-sonnet-4-6")],
    ids=lambda p: f"{p[0]}:{p[1]}",
)
async def test_a_refused_item_spends_no_rate_limit(
    client,
    registry,  # noqa: F811
    keys,  # noqa: F811
    cli_calls,  # noqa: F811
    refused,
):
    """The refused item no longer counts against rpm_limit: the next item of
    the batch is pinged."""
    registry(_THREE_CLIS)
    rec = _limited_key(keys, rpm_limit=1)
    with aioresponses() as mock:
        _mock_http(mock)
        results = await _preflight(
            client, rec["key"], refused, ("claude-code", "claude-sonnet-4-6")
        )

    assert [(r["ok"], r.get("error")) for r in results] == [
        (False, "forbidden_model"),
        (True, None),
    ]
    assert cli_calls == [("claude", "claude-sonnet-4-6")]


@pytest.mark.asyncio
async def test_a_refused_item_calls_no_provider(client, registry, keys, cli_calls):  # noqa: F811
    registry(_THREE_CLIS)
    rec = _limited_key(keys)
    with aioresponses() as mock:
        (result,) = await _preflight(
            client, rec["key"], ("CLAUDE-CODE", "claude-sonnet-4-6")
        )
        assert not mock.requests
    assert result["error"] == "forbidden_model"
    assert cli_calls == []
