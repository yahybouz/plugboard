"""Chat answers that were avoidable 500s (BUG-48, BUG-49).

* BUG-48 (audit A-11): a body that is not JSON (``{mal``, empty, not UTF-8,
  nested past the parser's depth) is the client's error: ``400``, nothing
  called. It reached the generic ``ValueError`` handler and answered ``500``.
* BUG-49 (audit A-06): a response cache that cannot be read (database locked,
  file that is not a database, stored answer that is not JSON) no longer
  breaks the chat: the request is served without the cache and a warning is
  logged.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest
from aioresponses import aioresponses

from plugboard import cache
from tests.factories import FAKE_COMPLETION

AUTH = {"Authorization": "Bearer testkey"}
_PROVIDER_URL = "http://fake-llm.test/v1/chat/completions"
_CHAT = {
    "model": "test-provider:fake:model-v1",
    "messages": [{"role": "user", "content": "Hi"}],
}


@pytest.fixture
def cache_db(tmp_path, monkeypatch):
    """The response cache in a throwaway file."""
    path = tmp_path / "cache.db"
    monkeypatch.setattr(cache, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(cache, "_DB_PATH", path)
    return path


# ── BUG-48: a body that is not JSON ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v2/chat/completions"])
@pytest.mark.parametrize(
    "raw",
    [b"{mal", b"", b'{"model": "\xff"}', b"[" * 100_000],
    ids=["truncated", "empty", "not-utf8", "too-deep"],
)
async def test_body_that_is_not_json_is_a_400(client, cache_db, path, raw):
    with aioresponses() as mock:
        resp = await client.post(
            path,
            headers={**AUTH, "Content-Type": "application/json"},
            content=raw,
        )
        assert not mock.requests, "nothing may be called"

    assert resp.status_code == 400, resp.text
    assert "JSON" in resp.json()["detail"]


# ── BUG-49: a cache that cannot be read ─────────────────────────────────────


def _read_warnings(caplog) -> list:
    """The warnings of the cache lookup itself (not of the cache write)."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "plugboard.cache" and r.levelno == logging.WARNING
    ]


async def _chat_served_once(client, body=None) -> tuple:
    with aioresponses() as mock:
        mock.post(_PROVIDER_URL, payload=FAKE_COMPLETION)
        resp = await client.post(
            "/v1/chat/completions", headers=AUTH, json=body or _CHAT
        )
        calls = sum(len(v) for v in mock.requests.values())
    return resp, calls


@pytest.mark.asyncio
async def test_locked_cache_serves_the_chat_and_warns(
    client, cache_db, monkeypatch, caplog
):
    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cache, "_get_conn", locked)
    with caplog.at_level(logging.WARNING, logger="plugboard.cache"):
        resp, calls = await _chat_served_once(client)

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "Hello!"
    assert calls == 1
    assert _read_warnings(caplog) == [
        "Response cache unreadable (OperationalError), served without it: "
        "database is locked"
    ]


@pytest.mark.asyncio
async def test_cache_file_that_is_not_a_database_serves_the_chat(
    client, cache_db, caplog
):
    cache_db.write_bytes(b"this is not an SQLite database" * 100)
    with caplog.at_level(logging.WARNING, logger="plugboard.cache"):
        resp, calls = await _chat_served_once(client)

    assert resp.status_code == 200, resp.text
    assert calls == 1
    assert _read_warnings(caplog) == [
        "Response cache unreadable (DatabaseError), served without it: "
        "file is not a database"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "response = '{not json'",
        "response = '[1]'",
        "response = '\"text\"'",
        "ttl = 'soon'",
    ],
    ids=["not-json", "array", "string", "text-ttl"],
)
async def test_damaged_cache_row_is_a_miss(client, cache_db, caplog, damage):
    # The row is there but cannot be used: served as a miss, the provider
    # answers, and the lookup warns.
    resp, calls = await _chat_served_once(client)
    assert resp.status_code == 200 and calls == 1
    conn = sqlite3.connect(cache_db)
    conn.execute(f"UPDATE response_cache SET {damage}")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING, logger="plugboard.cache"):
        resp, calls = await _chat_served_once(client)

    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-cache") == "MISS"
    assert calls == 1
    assert len(_read_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_client_id_in_the_body_does_not_break_the_cache_key(client, cache_db):
    # A body field named client_id collided with compute_cache_key's own
    # argument (TypeError, 500).
    resp, calls = await _chat_served_once(client, {**_CHAT, "client_id": "x"})

    assert resp.status_code == 200, resp.text
    assert calls == 1


def test_client_id_in_the_body_never_selects_another_tenant():
    # The body's client_id is a request parameter, never the tenant.
    messages = [{"role": "user", "content": "Hi"}]
    as_a = cache.compute_cache_key(
        "m", messages, client_id="tenant-a", **cache.key_params({"client_id": "b"})
    )
    as_b = cache.compute_cache_key("m", messages, client_id="tenant-b")
    plain_a = cache.compute_cache_key("m", messages, client_id="tenant-a")
    assert as_a not in (as_b, plain_a)


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [["a"], {"a": 1}])
async def test_session_id_that_is_a_list_or_object_is_a_400(
    client, cache_db, session_id
):
    with aioresponses() as mock:
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={**_CHAT, "session_id": session_id},
        )
        assert not mock.requests, "nothing may be called"

    assert resp.status_code == 400, resp.text
    assert "session_id" in resp.json()["detail"]


def test_get_cached_still_refuses_a_missing_client_id_in_strict_mode(
    cache_db, monkeypatch
):
    # SEC-001 (ADR-032) is a caller bug, not an unreadable cache: it still
    # raises, and the chat answers 401 for it before reading the cache.
    monkeypatch.delenv("PLUGBOARD_ALLOW_PERMISSIVE", raising=False)
    monkeypatch.delenv("PLUGBOARD_STRICT_TENANT", raising=False)
    with pytest.raises(ValueError, match="SEC-001|client_id"):
        cache.get_cached("any-key", client_id="")
