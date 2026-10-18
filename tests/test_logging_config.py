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
