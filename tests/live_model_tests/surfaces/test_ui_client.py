"""Unit tests for UiSurfaceClient — no real browser, no network.

Council C9 (2026-05-31): developer + codex-coder. A scriptable FakeUiDriver
replaces Playwright entirely, so these run in the framework venv without
Chromium. The DOM-fidelity decisions (empty-content mapping, error
discrimination, fresh-context isolation) are exercised here because that is
exactly where the UI surface could manufacture a false PASS.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from tests.live_model_tests.protocols import (
    SurfaceClient,
)
from tests.live_model_tests.surfaces.ui_client import (
    BubbleRead,
    TurnRead,
    UiModelUnavailable,
    UiSurfaceClient,
    UiSurfaceError,
    _classify_error,
    _extract_user_message,
    _match_option,
    _resolve_content,
)


# ── catalog-id ↔ provider-prefixed dropdown option resolution ─────────────────

# Real /v1/models option values observed live (provider-prefixed).
_DROPDOWN = [
    "Codex:gpt-5.5",
    "Codex:gpt-5.4",
    "Codex:gpt-5.4-mini",
    "ollama:gpt-oss:120b-cloud",
    "ollama:qwen3.5:397b-cloud",
    "claude-code:claude-opus-4-7",
    "Gemini:gemini-3-flash-preview",
]


@pytest.mark.parametrize(
    "catalog_id,expected",
    [
        ("gpt-5.4", "Codex:gpt-5.4"),  # bare → prefixed
        ("gpt-5.4-mini", "Codex:gpt-5.4-mini"),  # not confused with gpt-5.4
        (
            "gpt-oss:120b-cloud",
            "ollama:gpt-oss:120b-cloud",
        ),  # colon-in-id survives (split first only)
        ("qwen3.5:397b-cloud", "ollama:qwen3.5:397b-cloud"),
        ("claude-opus-4-7", "claude-code:claude-opus-4-7"),
        ("gemini-3-flash-preview", "Gemini:gemini-3-flash-preview"),
        ("Codex:gpt-5.4", "Codex:gpt-5.4"),  # already-exact value wins
    ],
)
def test_match_option_resolves_prefixed_dropdown_ids(catalog_id, expected):
    assert _match_option(catalog_id, _DROPDOWN) == expected


def test_match_option_absent_returns_none():
    assert _match_option("claude-haiku-4-5", _DROPDOWN) is None
    assert _match_option("gpt-5", _DROPDOWN) is None  # partial, not a suffix match


# ── FakeUiDriver ──────────────────────────────────────────────────────────────


class FakeUiDriver:
    """Scriptable UiDriver. run_turn() returns a pre-canned TurnRead or raises a
    pre-canned exception, and records every call for assertions."""

    def __init__(
        self, result: Any = None, *, raises: Optional[Exception] = None
    ) -> None:
        self._result = result
        self._raises = raises
        self.calls: List[Dict[str, Any]] = []
        self.closed = False

    def run_turn(self, **kwargs: Any) -> TurnRead:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._result

    def close(self) -> None:
        self.closed = True


def _done(
    text: str,
    *,
    content_empty: bool = False,
    finish_reason: Optional[str] = None,
    status_text: str = "1.2s | 10 tokens | $0.0001",
) -> TurnRead:
    return TurnRead(
        status_state="done",
        status_text=status_text,
        bubble=BubbleRead(
            text=text, content_empty=content_empty, finish_reason=finish_reason
        ),
    )


def _client(result: Any = None, *, raises: Optional[Exception] = None):
    drv = FakeUiDriver(result=result, raises=raises)
    return UiSurfaceClient(driver=drv), drv


MSGS = [{"role": "user", "content": "ping ALIVE:NONCE"}]


def _chat(client, msgs=MSGS, *, stream=True):
    return client.chat(
        "claude-haiku-4-5",
        msgs,
        stream=stream,
        timeout=10.0,
        max_tokens=256,
        temperature=0.0,
    )


# ── helpers (pure) ────────────────────────────────────────────────────────────


def test_extract_user_message_last_user_wins():
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "r"},
        {"role": "user", "content": "second"},
    ]
    assert _extract_user_message(msgs) == "second"


def test_extract_user_message_no_user_raises():
    with pytest.raises(ValueError):
        _extract_user_message([{"role": "system", "content": "sys only"}])


@pytest.mark.parametrize(
    "status,expected",
    [
        ("Error 401", "http_error:401"),
        ("Error 500", "http_error:500"),
        ("Error", "http_error:unknown"),
        ("Connection error", "ui_connection_error"),
        ("weird", "ui_error"),
    ],
)
def test_classify_error(status, expected):
    assert _classify_error(status) == expected


# ── empty-content fidelity (the BLOCKER-class reason this surface exists) ──────


def test_empty_response_sentinel_maps_to_empty_via_attribute():
    """data-content-empty=1 → content '' even though the bubble shows a sentence."""
    content, fr = _resolve_content(
        BubbleRead("(empty response)", content_empty=True, finish_reason=None)
    )
    assert content == "" and fr is None


def test_truncation_maps_to_empty_length_via_attribute():
    content, fr = _resolve_content(
        BubbleRead(
            "⚠️ Réponse tronquée avant le contenu …",
            content_empty=True,
            finish_reason="length",
        )
    )
    assert content == "" and fr == "length"


def test_empty_with_stop_finish_reason_normalizes_to_none():
    """Council C9 MAJOR-2: empty content + finish_reason 'stop' → None, for parity
    with the MCP surface (_make_unified_dict: 'stop' if content else None). Only
    'length' survives on empty content."""
    content, fr = _resolve_content(
        BubbleRead("(empty response)", content_empty=True, finish_reason="stop")
    )
    assert content == "" and fr is None


def test_empty_sentinel_string_fallback_without_attribute():
    """Stale app.js (no hook): the known sentinel STRING still maps to empty."""
    content, fr = _resolve_content(
        BubbleRead("(empty response)", content_empty=False, finish_reason=None)
    )
    assert content == "" and fr is None


def test_truncation_string_fallback_without_attribute():
    content, fr = _resolve_content(
        BubbleRead(
            "⚠️ Réponse tronquée avant le contenu — …",
            content_empty=False,
            finish_reason=None,
        )
    )
    assert content == "" and fr == "length"


def test_real_content_passes_through_with_synthesized_stop():
    content, fr = _resolve_content(
        BubbleRead('{"echo":"NONCE"}', content_empty=False, finish_reason=None)
    )
    assert content == '{"echo":"NONCE"}' and fr == "stop"


def test_real_content_keeps_observed_finish_reason():
    content, fr = _resolve_content(
        BubbleRead("hi", content_empty=False, finish_reason="stop")
    )
    assert content == "hi" and fr == "stop"


# ── chat() integration through the fake driver ────────────────────────────────


def test_chat_plain_content():
    expected = '{"echo": "ALIVE:NONCE", "payload": "éàçü"}'
    client, drv = _client(_done(expected))
    r = _chat(client)
    assert r["content"] == expected
    assert r["reasoning"] == ""  # decision 3 — UI never scrapes reasoning
    assert r["finish_reason"] == "stop"
    assert r["done_received"] is True
    assert r["raw_sse_lines"] is None  # D5 skip
    assert r["billing"] is None  # D6 skip
    assert r["first_token_latency_ms"] is None  # UI can't measure TTFT (contract)
    assert r["model_echoed"] is None and r["request_id"] is None
    # the driver received the human-typed text (last user message)
    assert drv.calls[0]["message"] == "ping ALIVE:NONCE"
    assert drv.calls[0]["model"] == "claude-haiku-4-5"


def test_chat_empty_response_not_a_false_alive():
    """The bubble shows '(empty response)' but content must be '' so D1 fails it."""
    client, _ = _client(_done("(empty response)", content_empty=True))
    r = _chat(client)
    assert r["content"] == "" and r["finish_reason"] is None


def test_chat_error_status_raises_with_classified_type():
    err_turn = TurnRead(
        status_state="error",
        status_text="Error 429",
        bubble=BubbleRead("Error: rate limited", False, None),
    )
    client, _ = _client(err_turn)
    with pytest.raises(UiSurfaceError) as ei:
        _chat(client)
    assert ei.value.error_type == "http_error:429"


def test_chat_error_word_as_content_with_success_status_is_content():
    """C8 M-2 parity: a model literally answering 'Error: ...' on a DONE turn is
    content, not a transport error — discrimination is on the status state."""
    client, _ = _client(_done("Error: this is the model's own answer"))
    r = _chat(client)
    assert r["content"] == "Error: this is the model's own answer"


def test_chat_model_unavailable_propagates():
    client, _ = _client(raises=UiModelUnavailable("ghost-model"))
    with pytest.raises(UiModelUnavailable) as ei:
        _chat(client)
    assert ei.value.error_type == "ui_model_unavailable"


def test_chat_timeout_propagates():
    client, _ = _client(raises=TimeoutError("deadline"))
    with pytest.raises(TimeoutError):
        _chat(client)


def test_chat_passes_stream_and_max_tokens_to_driver():
    client, drv = _client(_done("ok"))
    client.chat("m", MSGS, stream=False, timeout=5.0, max_tokens=4096, temperature=0.5)
    call = drv.calls[0]
    assert (
        call["stream"] is False
        and call["max_tokens"] == 4096
        and call["temperature"] == 0.5
    )


# ── protocol conformance + skip discriminants ─────────────────────────────────


def test_satisfies_surface_client_protocol():
    client, _ = _client(_done("x"))
    assert isinstance(client, SurfaceClient)


def test_supports_billing_false_value():
    """D6 discriminates on the VALUE (getattr), not isinstance — must be False."""
    client, _ = _client(_done("x"))
    assert client.supports_billing is False


def test_not_streaming_capable_dict_has_none_sse():
    """Even though structural typing may match, the dict carries raw_sse_lines
    None so D5 short-circuits to skipped:surface_non_streaming."""
    client, _ = _client(_done("x"))
    assert _chat(client)["raw_sse_lines"] is None


def test_injected_driver_not_closed_by_client():
    """Ownership parity with MCP _owned_transport: an injected driver belongs to
    the caller, so client.close() must NOT close it (only a driver the client
    built itself is closed)."""
    client, drv = _client(_done("x"))
    client.close()
    assert drv.closed is False


def test_context_manager_runs_without_closing_injected():
    client, drv = _client(_done("x"))
    with client:
        r = _chat(client)
    assert r["content"] == "x"
    assert drv.closed is False
