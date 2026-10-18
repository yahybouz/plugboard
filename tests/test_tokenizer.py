"""Tests for tokenizer.py -- token estimation via tiktoken."""

from __future__ import annotations

from plugboard.tokenizer import (
    count_tokens,
    estimate_prompt_tokens,
    estimate_completion_tokens,
)
import plugboard.tokenizer as tokenizer


# ── count_tokens ─────────────────────────────────────────────────────────────


def test_count_tokens_simple():
    """Basic string should return a positive token count."""
    result = count_tokens("Hello, world!")
    assert result > 0


def test_count_tokens_empty():
    """Empty string should return 0 tokens."""
    assert count_tokens("") == 0


def test_count_tokens_long_text():
    """Longer text should have more tokens."""
    short = count_tokens("Hi")
    long = count_tokens(
        "This is a much longer sentence that should have many more tokens than just hi."
    )
    assert long > short


def test_count_tokens_unicode():
    """Unicode text should not crash."""
    result = count_tokens(
        "Bonjour le monde! Ceci est un texte en francais avec des accents: e, a, u."
    )
    assert result > 0


def test_count_tokens_code():
    """Code snippet should be tokenizable."""
    code = "def hello():\n    return 'world'\n"
    result = count_tokens(code)
    assert result > 0


def test_count_tokens_falls_back_when_encoding_asset_is_unavailable(monkeypatch):
    """A missing tiktoken data file must not make token estimation fail."""

    class UnavailableEncoding:
        calls = 0

        @classmethod
        def get_encoding(cls, _name: str):
            cls.calls += 1
            raise OSError("encoding asset unavailable")

    tokenizer._get_encoding.cache_clear()
    monkeypatch.setattr(tokenizer, "_tiktoken", UnavailableEncoding)

    try:
        assert tokenizer.count_tokens("a" * 100) == 25
        assert tokenizer.count_tokens("hi") == 1
        assert UnavailableEncoding.calls == 1
    finally:
        tokenizer._get_encoding.cache_clear()


# ── estimate_prompt_tokens ───────────────────────────────────────────────────


def test_estimate_prompt_tokens_simple():
    """Single user message."""
    messages = [{"role": "user", "content": "Hello!"}]
    tokens = estimate_prompt_tokens(messages)
    # At minimum: 3 (base) + 4 (msg overhead) + role tokens + content tokens
    assert tokens >= 8


def test_estimate_prompt_tokens_empty():
    """Empty messages list returns base overhead only."""
    tokens = estimate_prompt_tokens([])
    assert tokens == 3  # base overhead


def test_estimate_prompt_tokens_multiple_messages():
    """Multiple messages should sum up."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "Thanks!"},
    ]
    tokens = estimate_prompt_tokens(messages)
    assert tokens > 20


def test_estimate_prompt_tokens_multimodal():
    """Multimodal content (list of parts) should count text parts."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is in this image?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,..."},
                },
            ],
        }
    ]
    tokens = estimate_prompt_tokens(messages)
    assert tokens > 8  # base + overhead + text tokens


def test_estimate_prompt_tokens_empty_content():
    """Message with empty content should still count overhead."""
    messages = [{"role": "user", "content": ""}]
    tokens = estimate_prompt_tokens(messages)
    assert tokens >= 3 + 4  # base + msg overhead


def test_estimate_prompt_tokens_missing_content():
    """Message without content key should not crash."""
    messages = [{"role": "user"}]
    tokens = estimate_prompt_tokens(messages)
    assert tokens >= 3 + 4


def test_estimate_prompt_tokens_multimodal_non_text_only():
    """Multimodal with only non-text parts should still work."""
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        }
    ]
    tokens = estimate_prompt_tokens(messages)
    assert tokens >= 3 + 4


# ── estimate_completion_tokens ───────────────────────────────────────────────


def test_estimate_completion_tokens_simple():
    """Standard completion response."""
    response = {"choices": [{"message": {"content": "Hello! How can I help you?"}}]}
    tokens = estimate_completion_tokens(response)
    assert tokens > 0


def test_estimate_completion_tokens_empty_choices():
    """No choices should return 0."""
    assert estimate_completion_tokens({"choices": []}) == 0


def test_estimate_completion_tokens_no_choices_key():
    """Missing choices key should return 0."""
    assert estimate_completion_tokens({}) == 0


def test_estimate_completion_tokens_empty_content():
    """Empty content should return 0."""
    response = {"choices": [{"message": {"content": ""}}]}
    assert estimate_completion_tokens(response) == 0


def test_estimate_completion_tokens_multiple_choices():
    """Multiple choices should sum up."""
    response = {
        "choices": [
            {"message": {"content": "First answer"}},
            {"message": {"content": "Second answer that is longer"}},
        ]
    }
    tokens = estimate_completion_tokens(response)
    single = estimate_completion_tokens(
        {"choices": [{"message": {"content": "First answer"}}]}
    )
    assert tokens > single


def test_estimate_completion_tokens_no_message():
    """Choice without message key should not crash."""
    response = {"choices": [{"index": 0}]}
    assert estimate_completion_tokens(response) == 0


def test_estimate_completion_tokens_none_content():
    """Choice with None content should return 0."""
    response = {"choices": [{"message": {"content": None}}]}
    assert estimate_completion_tokens(response) == 0
