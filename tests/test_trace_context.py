"""The incoming W3C ``traceparent`` (PLU-107).

The gateway reads and validates ``traceparent`` (and bounds ``tracestate``)
in the request-id middleware, keeps the trace id and the caller's span id in
a ContextVar, and records the trace id on the call's usage entry. An invalid
or missing header is ignored: no 4xx, no trace id invented, and the raw value
is never written to a log or an answer.
"""

from __future__ import annotations

import asyncio
import json
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


# ── Background tasks started by an admin request ─────────────────────────────


def _request_context():
    from plugboard.logging_config import _request_id_var

    return _request_id_var.get(), tc.current.get()


@pytest.mark.asyncio
async def test_the_auto_update_run_holds_no_request_or_trace_id(client, monkeypatch):
    """The run outlives the admin request: its logs must not carry that
    request's id nor its trace."""
    from plugboard import app as app_mod

    seen = []

    class Scheduler:
        running_since = None

        async def run_now(self, reason):
            await asyncio.sleep(0)  # runs on after the request has answered
            seen.append(_request_context())

    state = app_mod.app.state
    monkeypatch.setattr(state, "pricing_scheduler", Scheduler(), raising=False)
    monkeypatch.setattr(state, "model_availability_scheduler", None, raising=False)
    before = set(app_mod._background_tasks)

    resp = await client.post(
        "/admin/auto-update/run", headers={**AUTH, "traceparent": _VALID}
    )

    assert resp.status_code == 202
    await asyncio.gather(*(set(app_mod._background_tasks) - before))
    assert seen == [("-", None)]


@pytest.mark.asyncio
async def test_the_initial_discovery_holds_no_request_or_trace_id(monkeypatch):
    """Started while an admin request adds a provider."""
    from plugboard import app as app_mod
    from plugboard.logging_config import _request_id_var

    seen = []

    def get_provider(provider_id):
        seen.append(_request_context())
        return None

    monkeypatch.setattr(app_mod, "get_provider", get_provider)
    rid_token = _request_id_var.set("admin-request")
    trace_token = tc.current.set(tc.parse_traceparent(_VALID))
    try:
        app_mod._schedule_initial_discovery("probe")
    finally:
        tc.current.reset(trace_token)
        _request_id_var.reset(rid_token)
    for _ in range(5):
        await asyncio.sleep(0)

    assert seen == [("-", None)]


# ── JSON log lines of a real request ─────────────────────────────────────────


class _JsonLines(logging.Handler):
    """Formats each record with the gateway's JSON formatter as it is
    emitted, in the context of the code that logs it."""

    def __init__(self):
        from plugboard.logging_config import _JsonFormatter

        super().__init__(logging.INFO)
        self.setFormatter(_JsonFormatter())
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@pytest.fixture
def json_lines():
    handler = _JsonLines()
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield handler.lines
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


def _lines_of(lines, resp):
    """The JSON lines the request wrote, by its X-Request-ID."""
    rid = resp.headers["X-Request-ID"]
    return [e for e in (json.loads(line) for line in lines) if e["request_id"] == rid]


@pytest.mark.asyncio
async def test_the_json_lines_of_a_traced_request_carry_its_trace(
    client, usage_db, json_lines
):
    """PLU-108 end to end: a real request with a valid traceparent goes
    through the middleware, and the gateway's own log lines (routing, answer)
    carry the trace id and the caller's span id."""
    resp = await _chat(client, {"traceparent": _VALID})

    assert resp.status_code == 200
    mine = _lines_of(json_lines, resp)
    assert {e["logger"] for e in mine} >= {"plugboard.router", "plugboard.app"}
    assert all((e["trace_id"], e["span_id"]) == (_TRACE, _SPAN) for e in mine)


@pytest.mark.asyncio
async def test_the_json_lines_of_an_invalid_traceparent_carry_no_trace_and_no_value(
    client, usage_db, json_lines
):
    bad = f'00-{_TRACE}-{_SPAN}-01 {_MARK}","trace_id":"{_MARK}'
    resp = await _chat(client, {"traceparent": bad})

    assert resp.status_code == 200
    mine = _lines_of(json_lines, resp)
    assert mine
    assert all("trace_id" not in e and "span_id" not in e for e in mine)
    assert not any(_MARK in line for line in json_lines)
