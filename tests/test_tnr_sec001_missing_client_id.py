"""TNR-SEC-001b — Global handler for SEC-001 ValueError must produce a
structured 401 response with X-Error-Detail header.

Closes BUG-2026-05-04-001. Manifesto §5.5 mandates a Test de Non-Regression
for every fixed bug. Originally observed during model benchmarks: callers
that hit /v1/chat/completions without propagating client_id triggered a
ValueError in the cache layer. Without the global handler the error
propagated as a bare Starlette 500 (empty body), which masqueraded as an
upstream provider failure on claude-opus / claude-sonnet (cf. Notion
Kanban Bugs entry from 2026-05-04).

The fix converts SEC-001 ValueErrors into HTTP 401 (authentication required)
rather than 500, and adds an upstream guard in chat_completions so the
exception is never reached for anonymous requests. This TNR guarantees
both the handler and the endpoint gate never regress.

Catalogue : tests/tnr/README.md
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request


def _make_request(path: str = "/v1/chat/completions") -> Request:
    """Minimal ASGI scope to satisfy the handler signature."""
    return Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


def _call_handler(exc: ValueError):
    from plugboard.app import _value_error_handler  # noqa: PLC0415

    return asyncio.run(_value_error_handler(_make_request(), exc))


def test_tnr_sec001_handler_returns_structured_401():
    """SEC-001 ValueErrors must yield 401 (not 500) with JSON body + X-Error-Detail."""
    resp = _call_handler(
        ValueError(
            "cache call with empty client_id — pre-v2.8.2 legacy row scope. "
            "All callers must propagate client_id (SEC-001)."
        )
    )
    assert resp.status_code == 401
    assert resp.headers.get("X-Error-Detail") == "sec001-missing-client-id"
    body = json.loads(resp.body)
    assert body["error"]["code"] == "SEC-001"
    assert body["error"]["type"] == "sec001_violation"
    assert "client_id" in body["error"]["message"]
    assert "remediation" in body["error"]


def test_tnr_sec001_handler_passes_through_other_value_errors():
    """Non-SEC-001 ValueErrors still get a JSON envelope but no SEC-001 marker."""
    resp = _call_handler(ValueError("some unrelated value error"))
    assert resp.status_code == 500
    assert "X-Error-Detail" not in resp.headers
    body = json.loads(resp.body)
    assert body["error"]["type"] == "value_error"
    assert body["error"]["message"] == "some unrelated value error"


@pytest.mark.anyio
async def test_tnr_sec001_chat_missing_client_id_returns_401(client):
    """Anonymous request to /v1/chat/completions must return 401, not 500."""
    from plugboard.app import _resolve_client, app  # noqa: PLC0415

    # Override the dependency so FastAPI injects None as client_id.
    # Use a no-arg lambda so FastAPI does not try to resolve parameters.
    app.dependency_overrides[_resolve_client] = lambda: None
    try:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 401
        data = resp.json()
        assert data["detail"] == "Authentication required: missing client_id."
    finally:
        app.dependency_overrides.pop(_resolve_client, None)
