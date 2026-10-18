"""
Token estimation — counts tokens when the backend doesn't.

Uses tiktoken (cl100k_base) as a universal approximation.
Accurate for GPT-4/4o, ~10-15% margin for Claude/Gemini (acceptable for billing).
Falls back to character-based estimation if tiktoken unavailable.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, List

log = logging.getLogger(__name__)

try:
    import tiktoken as _tiktoken
except ImportError:
    _tiktoken = None
    log.warning(
        "tiktoken not installed — using character-based token estimation (~4 chars/token)"
    )


@lru_cache(maxsize=1)
def _get_encoding() -> Any | None:
    """Load the encoding on first use and permanently fall back if unavailable."""
    if _tiktoken is None:
        return None

    try:
        return _tiktoken.get_encoding("cl100k_base")
    except Exception as exc:
        # tiktoken downloads the encoding data when its local cache is empty.
        # Token estimation must remain available when that asset cannot be fetched.
        log.warning(
            "cl100k_base encoding unavailable (%s) — using character-based "
            "token estimation (~4 chars/token)",
            type(exc).__name__,
        )
        return None


def count_tokens(text: str) -> int:
    if not text:
        return 0

    encoding = _get_encoding()
    if encoding is None:
        return max(1, len(text) // 4)
    return len(encoding.encode(text))


def estimate_prompt_tokens(messages: List[Dict[str, Any]]) -> int:
    """Estimate token count for a list of chat messages."""
    total = 0
    for msg in messages:
        # Each message has overhead (~4 tokens for role, separators)
        total += 4
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            # Multimodal: list of content parts
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += count_tokens(part.get("text", ""))
        role = msg.get("role", "")
        total += count_tokens(role)
    # Base overhead for the conversation
    total += 3
    return total


def estimate_completion_tokens(response: Dict[str, Any]) -> int:
    """Estimate token count from a chat completion response."""
    total = 0
    for choice in response.get("choices", []):
        msg = choice.get("message", {})
        content = msg.get("content", "")
        if content:
            total += count_tokens(content)
    return total
