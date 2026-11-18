"""Tests for structured logging format."""

from __future__ import annotations

import json
import logging

from plugboard.logging_config import _JsonFormatter, _request_id_var


def test_json_formatter_includes_application_and_source_service():
    token = _request_id_var.set("rid-123")
    try:
        record = logging.LogRecord(
            name="plugboard.jobs",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="Job %s started",
            args=("daily_pricing",),
            exc_info=None,
        )
        payload = json.loads(_JsonFormatter().format(record))
    finally:
        _request_id_var.reset(token)

    assert payload["application"] == "plugboard"
    assert payload["service"] == "plugboard"
    assert payload["source_service"] == "jobs"
    assert payload["logger"] == "plugboard.jobs"
    assert payload["request_id"] == "rid-123"
    assert payload["msg"] == "Job daily_pricing started"


def test_json_formatter_defaults_request_id_and_handles_external_logger():
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.WARNING,
        pathname=__file__,
        lineno=64,
        msg="upstream is slow",
        args=(),
        exc_info=None,
    )
    payload = json.loads(_JsonFormatter().format(record))

    assert payload["source_service"] == "uvicorn.error"
    assert payload["request_id"] == "-"


def _format_plugboard_record() -> dict:
    record = logging.LogRecord(
        name="plugboard.router",
        level=logging.INFO,
        pathname=__file__,
        lineno=7,
        msg="routed",
        args=(),
        exc_info=None,
    )
    return json.loads(_JsonFormatter().format(record))


def test_json_formatter_adds_the_callers_trace_and_span_ids():
    """PLU-108: with a valid traceparent, each entry of the request carries
    its trace id and the caller's span id (the W3C parent-id)."""
    from plugboard import trace_context

    trace = trace_context.parse_traceparent(
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    )
    token = trace_context.current.set(trace)
    try:
        payload = _format_plugboard_record()
    finally:
        trace_context.current.reset(token)

    assert payload["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert payload["span_id"] == "00f067aa0ba902b7"


def test_json_formatter_leaves_trace_fields_out_without_a_trace():
    payload = _format_plugboard_record()

    assert "trace_id" not in payload
    assert "span_id" not in payload
    assert payload["request_id"] == "-"
