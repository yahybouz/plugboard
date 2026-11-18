"""Correlation of a usage entry with its request (PLU-106).

Each entry of ``usage_entries`` keeps the ``X-Request-ID`` of the call that
was billed and the W3C trace id the caller sent (``trace_id``, NULL when
there was none): the cost of one call, or of one trace, can then be found
in ``usage.db`` without trusting the caller's own count.
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest
from aioresponses import aioresponses

from tests.factories import (
    FAKE_COMPLETION,
    FAKE_STREAM_CHUNK_1,
    FAKE_STREAM_CHUNK_2,
    FAKE_STREAM_DONE,
)

AUTH = {"Authorization": "Bearer testkey"}
_MODEL = "test-provider:fake:model-v1"
_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"

# usage_entries as the releases before PLU-106 created it.
_OLD_TABLE = """CREATE TABLE usage_entries (
    id TEXT PRIMARY KEY, ts TEXT NOT NULL, month TEXT NOT NULL,
    client_id TEXT NOT NULL, key_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL,
    provider_id TEXT NOT NULL, prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
    pricing_month TEXT NOT NULL, cost_usd REAL NOT NULL,
    price_key TEXT NOT NULL DEFAULT '', price_source TEXT NOT NULL DEFAULT '')"""


def _messages():
    """A prompt of its own: a cached answer would not be billed."""
    return [{"role": "user", "content": f"Hi {uuid.uuid4().hex}"}]


@pytest.fixture
def usage_db(tmp_path, monkeypatch):
    """A usage store of its own, priced in 2026_03."""
    import plugboard.billing as b
    import plugboard.usage as u

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")
    return tmp_path / "usage.db"


def test_old_database_gains_the_columns_and_keeps_its_rows(usage_db):
    from plugboard import usage

    conn = sqlite3.connect(usage_db)
    conn.execute(_OLD_TABLE)
    conn.execute(
        "INSERT INTO usage_entries VALUES ('old1', '2026-03-01T00:00:00.000Z', "
        "'2026_03', 'c', '', 'm', 'p', 1, 2, 3, '2026_03', 0.5, '', '')"
    )
    conn.commit()
    conn.close()

    usage.append_usage(
        "c", "m", "p", 4, 5, "2026_03", 0.25, request_id="rid-new", trace_id=_TRACE
    )

    rows = {row["id"]: row for row in usage.get_usage("2026_03")}
    assert len(rows) == 2
    old = rows.pop("old1")
    assert (old["cost_usd"], old["request_id"], old["trace_id"]) == (0.5, None, None)
    [new] = rows.values()
    assert (new["request_id"], new["trace_id"]) == ("rid-new", _TRACE)

    conn = sqlite3.connect(usage_db)
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(usage_entries)")}
    conn.close()
    assert "idx_usage_trace" in indexes


def test_an_entry_without_correlation_stores_null(usage_db):
    from plugboard import usage

    usage.append_usage("c", "m", "p", 1, 1, "2026_03", 0.0)

    [row] = usage.get_usage("2026_03")
    assert (row["request_id"], row["trace_id"]) == (None, None)


def test_a_lapsed_hold_never_takes_the_request_id_of_the_call_that_records_it(
    usage_db, monkeypatch
):
    """A lapsed hold is recorded during someone else's request: that
    request's id is not the hold's."""
    from plugboard import usage
    from plugboard.logging_config import _request_id_var

    usage.hold_budget(client_id="c", key_id="k", month="2026_03", limit=1.0, cost=0.3)
    monkeypatch.setattr(usage, "HOLD_TTL_S", -1.0)
    token = _request_id_var.set("someone-else")
    try:
        assert usage.record_lapsed_holds() == 1
    finally:
        _request_id_var.reset(token)

    [row] = usage.get_usage("2026_03")
    assert row["price_source"] == usage.LAPSED_SOURCE
    assert row["request_id"] is None


@pytest.mark.asyncio
async def test_a_chat_call_is_recorded_with_its_x_request_id(client, usage_db):
    from plugboard import usage

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json={"model": _MODEL, "messages": _messages()},
        )
    assert resp.status_code == 200
    rid = resp.headers["X-Request-ID"]

    [row] = usage.get_usage("2026_03")
    assert row["request_id"] == rid
    assert row["trace_id"] is None  # no traceparent sent


@pytest.mark.asyncio
async def test_a_streamed_chat_call_is_recorded_with_its_x_request_id(
    client, usage_db, monkeypatch
):
    from plugboard import usage

    async def fake_stream(body):
        for chunk in (FAKE_STREAM_CHUNK_1, FAKE_STREAM_CHUNK_2, FAKE_STREAM_DONE):
            yield chunk

    monkeypatch.setattr("plugboard.app.forward_stream", fake_stream)
    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": _MODEL,
            "messages": _messages(),
            "stream": True,
        },
    )
    assert resp.status_code == 200
    _ = resp.content

    [row] = usage.get_usage("2026_03")
    assert row["request_id"] == resp.headers["X-Request-ID"]


def _seed_old_and_new_rows():
    """One entry written before PLU-106 (NULL columns), one after."""
    from plugboard import usage

    usage.append_usage("admin", "m", "p", 1, 2, "2026_03", 0.5)
    usage.append_usage(
        "admin", "m", "p", 3, 4, "2026_03", 0.25, request_id="r1", trace_id=_TRACE
    )


@pytest.mark.asyncio
async def test_usage_me_reads_both_rows_and_keeps_its_fields(client, usage_db):
    """/usage/me answers as before: the correlation columns are not added
    to the client contract (PLU-106 changes no public answer)."""
    _seed_old_and_new_rows()

    resp = await client.get("/usage/me", headers=AUTH, params={"month": "2026_03"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 2
    for entry in body["entries"]:
        assert "request_id" not in entry and "trace_id" not in entry
    assert body["summary"]["total_cost_usd"] == pytest.approx(0.75)


# What /usage/me answered for an entry under contract 3.0.1.
_USAGE_ME_FIELDS = {
    "id",
    "ts",
    "month",
    "client_id",
    "key_id",
    "model",
    "provider_id",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "pricing_month",
    "cost_usd",
}


@pytest.mark.asyncio
async def test_usage_me_never_exposes_a_column_added_later(client, usage_db):
    """/usage/me lists the contract's fields by name: a column added to
    usage_entries later stays private until it is named there."""
    from plugboard import usage

    usage.append_usage("admin", "m", "p", 1, 2, "2026_03", 0.5)
    conn = usage._get_conn()
    try:
        conn.execute("ALTER TABLE usage_entries ADD COLUMN future_secret TEXT")
        conn.execute("UPDATE usage_entries SET future_secret = 'do-not-show'")
        conn.commit()
    finally:
        conn.close()

    resp = await client.get("/usage/me", headers=AUTH, params={"month": "2026_03"})

    assert resp.status_code == 200
    [entry] = resp.json()["entries"]
    assert set(entry) == _USAGE_ME_FIELDS
    assert "do-not-show" not in resp.text


@pytest.mark.asyncio
async def test_admin_exports_carry_the_correlation_columns(client, usage_db):
    _seed_old_and_new_rows()

    as_json = await client.get("/admin/usage/2026_03/export", headers=AUTH)
    as_csv = await client.get(
        "/admin/usage/2026_03/export", headers=AUTH, params={"format": "csv"}
    )

    assert as_json.status_code == 200
    ids = sorted((e["request_id"] or "") for e in as_json.json()["entries"])
    assert ids == ["", "r1"]
    assert as_csv.status_code == 200
    header, *lines = as_csv.text.strip().splitlines()
    assert "request_id" in header.split(",") and "trace_id" in header.split(",")
    assert len(lines) == 2
    assert any(_TRACE in line for line in lines)
