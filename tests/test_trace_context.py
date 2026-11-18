"""The incoming W3C ``traceparent`` (PLU-107).

The gateway reads and validates ``traceparent`` (and bounds ``tracestate``)
in the request-id middleware, keeps the trace id and the caller's span id in
a ContextVar, and records the trace id on the call's usage entry. An invalid
or missing header is ignored: no 4xx, no trace id invented, and the raw value
is never written to a log or an answer.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from aioresponses import aioresponses

from plugboard import trace_context as tc
from tests.factories import (
    FAKE_COMPLETION,
    FAKE_STREAM_CHUNK_1,
    FAKE_STREAM_CHUNK_2,
    FAKE_STREAM_DONE,
)

AUTH = {"Authorization": "Bearer testkey"}
_MODEL = "test-provider:fake:model-v1"
_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
_SPAN = "00f067aa0ba902b7"
_VALID = f"00-{_TRACE}-{_SPAN}-01"


# ── Parsing ──────────────────────────────────────────────────────────────────


def test_a_valid_version_00_header_is_read():
    parsed = tc.parse_traceparent(_VALID)
    assert parsed is not None
    assert (parsed.trace_id, parsed.span_id) == (_TRACE, _SPAN)


def test_surrounding_whitespace_is_ignored():
    assert tc.parse_traceparent(f"  {_VALID}\t").trace_id == _TRACE


@pytest.mark.parametrize(
    "value",
    [
        "",
        _VALID[:-1],  # too short
        _VALID + "0",  # version 00 has nothing after the flags
        _VALID + "-extra",  # same, even after a dash
        f"00-{_TRACE[:-1]}g-{_SPAN}-01",  # not hex
        f"00-{_TRACE.upper()}-{_SPAN}-01",  # upper case is not allowed
        f"00-{'0' * 32}-{_SPAN}-01",  # all-zero trace id
        f"00-{_TRACE}-{'0' * 16}-01",  # all-zero span id
        f"ff-{_TRACE}-{_SPAN}-01",  # version ff is forbidden
        f"0g-{_TRACE}-{_SPAN}-01",  # version not hex
        f"00-{_TRACE}-{_SPAN}-1",  # flags are two hex digits
        f"00_{_TRACE}_{_SPAN}_01",  # wrong separators
        f"00-{_TRACE}-{_SPAN}-0z",
        "x" * 10_000,
    ],
)
def test_an_invalid_header_is_ignored(value):
    assert tc.parse_traceparent(value) is None


def test_a_future_version_is_read_by_its_version_00_fields():
    """W3C forward compatibility: a version above 00 (not ff) is parsed as
    00, and what follows the flags after a dash is ignored."""
    assert tc.parse_traceparent(f"01-{_TRACE}-{_SPAN}-01").trace_id == _TRACE
    assert tc.parse_traceparent(f"cc-{_TRACE}-{_SPAN}-01-future").trace_id == _TRACE
    assert tc.parse_traceparent(f"01-{_TRACE}-{_SPAN}-01future") is None


def test_a_future_version_longer_than_the_bound_is_ignored():
    value = f"01-{_TRACE}-{_SPAN}-01-" + "a" * tc.MAX_TRACEPARENT_LEN
    assert tc.parse_traceparent(value) is None


def test_from_headers_reads_one_traceparent_and_a_bounded_tracestate():
    ctx = tc.from_headers([_VALID], ["vendor=abc", "other=1"])
    assert (ctx.trace_id, ctx.span_id, ctx.tracestate) == (
        _TRACE,
        _SPAN,
        "vendor=abc,other=1",
    )


def test_from_headers_ignores_several_traceparents():
    assert tc.from_headers([_VALID, f"00-{'a' * 32}-{_SPAN}-01"], []) is None


@pytest.mark.parametrize(
    "tracestate",
    [["v=" + "a" * tc.MAX_TRACESTATE_LEN], ["v=a\nb"], ["v=\x00"], ["v=é"]],
)
def test_a_tracestate_too_long_or_not_printable_is_dropped(tracestate):
    ctx = tc.from_headers([_VALID], tracestate)
    assert ctx.trace_id == _TRACE
    assert ctx.tracestate is None


def test_tracestate_is_ignored_without_a_valid_traceparent():
    assert tc.from_headers(["bogus"], ["v=1"]) is None


@pytest.mark.parametrize(
    "tracestates, kept",
    [
        (["", "v=1"], "v=1"),
        (["v=1", " ", "w=2"], "v=1,w=2"),
        (["", " \t"], None),
    ],
)
def test_empty_tracestate_values_are_left_out(tracestates, kept):
    assert tc.from_headers([_VALID], tracestates).tracestate == kept


# ── Through the gateway ──────────────────────────────────────────────────────


@pytest.fixture
def usage_db(tmp_path, monkeypatch):
    import plugboard.billing as b
    import plugboard.usage as u

    monkeypatch.setattr(u, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(u, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(b, "_PRICING_DIR", tmp_path / "pricing")
    monkeypatch.setattr(b, "current_pricing_month", lambda: "2026_03")


def _messages():
    """A prompt of its own: a cached answer would not be billed."""
    return [{"role": "user", "content": f"Hi {uuid.uuid4().hex}"}]


async def _chat(client, headers):
    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        return await client.post(
            "/v1/chat/completions",
            headers={**AUTH, **headers},
            json={"model": _MODEL, "messages": _messages()},
        )


@pytest.mark.asyncio
async def test_a_valid_traceparent_is_recorded_on_the_usage_entry(client, usage_db):
    from plugboard import usage

    resp = await _chat(client, {"traceparent": _VALID, "tracestate": "v=1"})

    assert resp.status_code == 200
    [row] = usage.get_usage("2026_03")
    assert (row["request_id"], row["trace_id"]) == (
        resp.headers["X-Request-ID"],
        _TRACE,
    )


@pytest.mark.asyncio
async def test_no_traceparent_records_no_trace_id(client, usage_db):
    from plugboard import usage

    resp = await _chat(client, {})

    assert resp.status_code == 200
    [row] = usage.get_usage("2026_03")
    assert row["trace_id"] is None


_MARK = "INJECTED0MARK"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        f"00-{_TRACE}-{_SPAN}",  # length
        f"00-{_TRACE[: -len(_MARK)]}{_MARK}-{_SPAN}-01",  # not hex
        f"00-{'0' * 32}-{_SPAN}-01",  # zeros
        f"ff-{_TRACE}-{_SPAN}-01",  # version ff
        f'00-{_TRACE}-{_SPAN}-01 {_MARK}","level":"CRITICAL',  # log injection
    ],
)
async def test_an_invalid_traceparent_is_ignored_and_never_reflected(
    client, usage_db, caplog, value
):
    from plugboard import usage

    with caplog.at_level(logging.DEBUG):
        resp = await _chat(client, {"traceparent": value})

    assert resp.status_code == 200
    [row] = usage.get_usage("2026_03")
    assert row["trace_id"] is None
    assert _MARK not in caplog.text
    assert _MARK not in resp.text
    assert all(_MARK not in v for v in resp.headers.values())


@pytest.mark.asyncio
async def test_concurrent_requests_keep_their_own_trace(client, usage_db, monkeypatch):
    """Two streamed calls in flight at once: each usage entry has the trace
    id its own request sent."""
    from plugboard import usage

    both_in_flight = asyncio.Barrier(2)

    async def fake_stream(body):
        await asyncio.wait_for(both_in_flight.wait(), timeout=5)
        for chunk in (FAKE_STREAM_CHUNK_1, FAKE_STREAM_CHUNK_2, FAKE_STREAM_DONE):
            yield chunk

    monkeypatch.setattr("plugboard.app.forward_stream", fake_stream)
    traces = ["a" * 32, "b" * 32]

    async def call(trace_id):
        resp = await client.post(
            "/v1/chat/completions",
            headers={**AUTH, "traceparent": f"00-{trace_id}-{_SPAN}-01"},
            json={
                "model": _MODEL,
                "messages": _messages(),
                "stream": True,
            },
        )
        assert resp.status_code == 200
        _ = resp.content
        return resp.headers["X-Request-ID"], trace_id

    sent = dict(await asyncio.gather(*(call(t) for t in traces)))

    recorded = {r["request_id"]: r["trace_id"] for r in usage.get_usage("2026_03")}
    assert recorded == sent
