"""BUG-124: a key's rpm_limit counts the calls to a model, never the reads.

rpm_limit bounds a client's inference: every route that calls a model takes
one from it (chat v1 and v2, each preflight ping, embeddings, decisions).
The reads (model lists and profiles, /v1/me, /usage/me, /health...) stay
under the global per-key limit only (RATE_LIMIT, BUG-119): an SDK that lists
the models before each call must not be refused on its chat.

Embeddings and decisions are covered where they live:
``tests/test_embeddings_api.py::test_rpm_limit`` and
``tests/test_decision_api.py::test_admission_limits_before_dispatch``; each
preflight ping by ``tests/test_inherited_runtime_v6.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import test_inherited_runtime_parent as _parent
from tests.test_inherited_runtime_parent import (
    _KNOWN_PRICE,
    _auth,
    _new_key,
    _set_price,
)

fake_forward = _parent.fake_forward
keys_env = _parent.keys_env

ROOT = Path(__file__).resolve().parents[1]
MODEL = "test-provider:fake:model-v1"

READS = [
    "/v1/me",
    "/v1/me/keys",
    "/usage/me",
    "/v1/models",
    "/v2/models",
    "/v1/models/benchmarks",
    "/v2/models/benchmarks",
    f"/v1/models/{MODEL}/info",
    "/v1/embeddings/models",
    "/v1/decisions/models",
    "/v1/inflight",
    "/health",
    "/health/ready",
]


def _chat_body():
    return {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture(autouse=True)
def _fresh_rpm_counts(tmp_path, monkeypatch):
    """Empty rpm counts, and a response cache of its own: an answer cached by
    another test for the same client and prompt would skip the provider."""
    import plugboard.cache as response_cache
    import plugboard.ratelimit as rl

    monkeypatch.setattr(response_cache, "_DATA_DIR", tmp_path / "cache")
    monkeypatch.setattr(response_cache, "_DB_PATH", tmp_path / "cache" / "cache.db")
    rl._client_buckets.clear()
    yield
    rl._client_buckets.clear()


async def test_reads_leave_the_rpm_limit_to_the_model_calls(
    client, keys_env, fake_forward, monkeypatch
):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "rpm-reads", rpm_limit=1)

    for path in READS:
        for _ in range(3):
            resp = await client.get(path, headers=_auth(rec))
            assert resp.status_code != 429, (path, resp.text)

    first = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat_body()
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat_body()
    )
    assert second.status_code == 429
    assert "Client rate limit exceeded" in second.text
    assert len(fake_forward["calls"]) == 1


@pytest.mark.parametrize(
    "first_path", ["/v1/chat/completions", "/v2/chat/completions", "/v1/preflight"]
)
async def test_every_chat_route_takes_from_the_same_count(
    client, keys_env, fake_forward, monkeypatch, first_path
):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "rpm-chat", rpm_limit=1)
    body = (
        {"items": [{"provider": "test-provider", "model": "fake:model-v1"}]}
        if first_path == "/v1/preflight"
        else _chat_body()
    )

    first = await client.post(first_path, headers=_auth(rec), json=body)
    assert first.status_code == 200, first.text
    if first_path == "/v1/preflight":
        assert first.json()["results"][0]["ok"] is True
    for path in ("/v1/chat/completions", "/v2/chat/completions"):
        refused = await client.post(path, headers=_auth(rec), json=_chat_body())
        assert refused.status_code == 429, (path, refused.text)
    assert len(fake_forward["calls"]) == 1


def test_the_guide_says_what_rpm_limit_counts():
    guide = (ROOT / "docs" / "guide" / "clients-and-keys.md").read_text("utf-8")
    row = next(line for line in guide.splitlines() if line.startswith("| `rpm_limit`"))
    for route in (
        "/v1/chat/completions",
        "/v2/chat/completions",
        "/v1/preflight",
        "/v1/embeddings",
        "/v1/decisions",
    ):
        assert f"`{route}`" in row, route
    assert "reads" in row and "`RATE_LIMIT`" in row


def test_the_contract_says_what_rpm_limit_counts():
    from plugboard.api_contract import ClientInfo

    description = ClientInfo.model_fields["rpm_limit"].description
    assert "call a model" in description and "0 = none" in description


async def test_a_cached_answer_counts_too(client, keys_env, fake_forward, monkeypatch):
    _set_price(monkeypatch, _KNOWN_PRICE)
    rec = _new_key(keys_env, "rpm-cache", rpm_limit=2)

    for _ in range(2):
        resp = await client.post(
            "/v1/chat/completions", headers=_auth(rec), json=_chat_body()
        )
        assert resp.status_code == 200, resp.text
    assert len(fake_forward["calls"]) == 1  # the second answer came from the cache
    third = await client.post(
        "/v1/chat/completions", headers=_auth(rec), json=_chat_body()
    )
    assert third.status_code == 429
