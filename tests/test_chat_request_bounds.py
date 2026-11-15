"""BUG-121: the chat routes refuse an output cap or a message role no provider
can honour with a 400 ``invalid_request``, before the budget hold and before
any provider call, for every caller (the admin key included), streamed or not.

Before: ``max_tokens: -5`` reached the provider (200), ``max_tokens: 1e12``
came back as a 429 from the budget hold, and an unknown role was answered.
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from plugboard import router, usage
from plugboard.api_contract import CHAT_ROLES, MAX_OUTPUT_TOKENS_CAP
from tests.factories import FAKE_COMPLETION

ADMIN = {"Authorization": "Bearer testkey"}  # the client fixture's admin key
URL = "http://fake-llm.test/v1/chat/completions"
MODEL = "test-provider:fake:model-v1"
PATHS = ("/v1/chat/completions", "/v2/chat/completions")


@pytest.fixture
def budgeted(client, tmp_path, monkeypatch):
    """A client key with a monthly budget, whose calls would take a hold."""
    import plugboard.apikeys as ak
    import plugboard.billing as b

    monkeypatch.setattr(usage, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    match = b.PriceMatch(
        {"input_price": 0.0, "output_price": 3000.0}, "fake:model-v1", "models"
    )
    monkeypatch.setattr(b, "resolve_price", lambda *a, **k: match)
    monkeypatch.setattr(router, "_DELAYS", [])
    key = ak.create_key("acme", "limited", monthly_limit_usd=1.0)
    return {"Authorization": f"Bearer {key['key']}"}


@pytest.fixture
def spies(monkeypatch):
    """Record any provider call and any budget hold; neither may happen."""
    import plugboard.app as app_mod

    calls = {"forward": 0, "forward_stream": 0, "hold": 0}

    async def forward(*a, **k):
        calls["forward"] += 1
        raise AssertionError("provider called")

    def forward_stream(*a, **k):
        calls["forward_stream"] += 1
        raise AssertionError("provider called")

    async def hold(*a, **k):
        calls["hold"] += 1

    monkeypatch.setattr(app_mod, "forward", forward)
    monkeypatch.setattr(app_mod, "forward_stream", forward_stream)
    monkeypatch.setattr(app_mod, "_hold_budget", hold)
    return calls


def _body(**extra):
    return {
        "model": MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "cache": False,
        **extra,
    }


async def _refused(client, headers, body, spies):
    """Each route, streamed or not: 400 invalid_request, nothing called."""
    out = []
    for path in PATHS:
        for stream in (False, True):
            resp = await client.post(
                path, json={**body, "stream": stream}, headers=headers
            )
            assert resp.status_code == 400, (path, stream, resp.text)
            payload = resp.json()
            assert payload["code"] == "invalid_request", payload
            out.append(payload["detail"])
    assert spies == {"forward": 0, "forward_stream": 0, "hold": 0}
    return out


BAD_CAPS = [
    -5,
    0,
    2.5,
    True,
    False,
    "100",
    [],
    {},
    MAX_OUTPUT_TOKENS_CAP + 1,
    1e12,
]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("value", BAD_CAPS, ids=repr)
async def test_an_impossible_output_cap_is_400(client, budgeted, spies, field, value):
    for headers in (budgeted, ADMIN):
        for detail in await _refused(client, headers, _body(**{field: value}), spies):
            assert detail == (
                f"{field} must be an integer from 1 to {MAX_OUTPUT_TOKENS_CAP}"
            )


@pytest.mark.asyncio
async def test_the_huge_cap_of_the_report_is_400_not_the_budget_429(
    client, budgeted, monkeypatch
):
    """The UAT report's case, without the spies: the real budget hold path."""
    with aioresponses() as mock:
        mock.post(URL, payload=FAKE_COMPLETION, repeat=True)
        resp = await client.post(
            "/v1/chat/completions", json=_body(max_tokens=1e12), headers=budgeted
        )
        assert not mock.requests
    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_request"
    conn = usage._get_conn()
    try:
        assert conn.execute("SELECT * FROM budget_holds").fetchall() == []
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_a_session_turn_checks_the_cap_before_its_history(client, spies):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [], "max_tokens": -5},
        headers={**ADMIN, "X-LLM-Session-ID": "s1"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"].startswith("max_tokens must be an integer")
    assert spies["forward"] == spies["hold"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caps",
    [
        {},
        {"max_tokens": None},
        {"max_tokens": 1},
        {"max_tokens": MAX_OUTPUT_TOKENS_CAP},
        {"max_completion_tokens": 1, "max_tokens": None},
        {"max_tokens": 64.0},  # a whole number, as JSON may carry it
    ],
    ids=repr,
)
async def test_a_possible_output_cap_still_reaches_the_provider(client, caps):
    with aioresponses() as mock:
        mock.post(URL, payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions", json=_body(**caps), headers=ADMIN
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["hacker", "System", "USER", "model", "bot "])
async def test_an_unknown_role_is_400_and_named(client, budgeted, spies, role):
    body = _body(
        messages=[
            {"role": "user", "content": "hi"},
            {"role": role, "content": "hi"},
        ]
    )
    for headers in (budgeted, ADMIN):
        for detail in await _refused(client, headers, body, spies):
            assert detail.startswith(f"messages[1].role '{role}' is not supported")
            for known in CHAT_ROLES:
                assert known in detail


@pytest.mark.asyncio
async def test_a_long_or_odd_role_is_not_echoed_raw(client, spies):
    role = "x" * 39 + "\nINJECTED" + "y" * 5000
    resp = await client.post(
        "/v1/chat/completions",
        json=_body(messages=[{"role": role, "content": "hi"}]),
        headers=ADMIN,
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "\n" not in detail and "INJECTED" not in detail
    assert "y" * 50 not in detail
    assert len(detail) < 200
    assert detail.startswith("messages[0].role '" + "x" * 39 + "?")


@pytest.mark.asyncio
async def test_existing_message_refusals_carry_the_same_code(client, spies):
    resp = await client.post(
        "/v1/chat/completions", json={"model": MODEL, "messages": []}, headers=ADMIN
    )
    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "detail": "messages must be a non-empty array",
        "code": "invalid_request",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("role", sorted(CHAT_ROLES))
async def test_every_openai_role_still_reaches_the_provider(client, role):
    assert set(CHAT_ROLES) == {
        "system",
        "developer",
        "user",
        "assistant",
        "tool",
        "function",
    }
    messages = [{"role": "user", "content": "hi"}]
    if role != "user":
        messages.append(
            {"role": role, "content": "x", "name": "f", "tool_call_id": "c"}
        )
    with aioresponses() as mock:
        mock.post(URL, payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions", json=_body(messages=messages), headers=ADMIN
        )
    assert resp.status_code == 200, resp.text
