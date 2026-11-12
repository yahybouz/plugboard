"""What a CLI subscription route cannot serve.

A CLI subscription route (a provider whose ``auth_source`` starts with
``subprocess:``) hands the conversation to a CLI as plain text. It cannot pass
tools, a tool choice, legacy functions, a structured response format or a
non-text content part: these used to be dropped silently.

``unsupported_on_cli()`` names each such field of an OpenAI-style chat body so
that a caller can refuse the request with the reason ``UNSUPPORTED_ON_CLI``.
It is a pure function: it imports neither the router nor the app, and it never
raises, whatever the shape of the body.
"""

from __future__ import annotations

from typing import Any, List, Mapping

UNSUPPORTED_ON_CLI = "unsupported_on_cli"

# Values that ask for no tool or function call: a CLI route can honour them.
_NO_CALL_CHOICES = (None, "none", "auto")


def _content_types(messages: Any) -> List[str]:
    """Sorted ``content:<type>`` entries for every non-text content part."""
    if not isinstance(messages, list):
        return []
    types = set()
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            part_type = part.get("type") if isinstance(part, Mapping) else None
            if not isinstance(part_type, str) or not part_type:
                types.add("unknown")
            elif part_type != "text":
                types.add(part_type)
    return [f"content:{part_type}" for part_type in sorted(types)]


def _structured_response_format(response_format: Any) -> bool:
    """True when ``response_format`` asks for anything but plain text."""
    if response_format is None:
        return False
    if not isinstance(response_format, Mapping):
        return True
    return response_format.get("type", "text") != "text"


def unsupported_on_cli(body: Mapping[str, Any]) -> List[str]:
    """Fields of ``body`` a CLI subscription route cannot serve.

    The list has no duplicates and keeps this order: ``tools``,
    ``tool_choice``, ``functions``, ``function_call``, ``response_format``,
    then the ``content:<type>`` entries sorted (``content:unknown`` for a part
    that is not an object or has no type). An empty list means the body is
    text only.
    """
    if not isinstance(body, Mapping):
        return []
    refused: List[str] = []
    if body.get("tools"):
        refused.append("tools")
    if body.get("tool_choice") not in _NO_CALL_CHOICES:
        refused.append("tool_choice")
    if body.get("functions"):
        refused.append("functions")
    if body.get("function_call") not in _NO_CALL_CHOICES:
        refused.append("function_call")
    if _structured_response_format(body.get("response_format")):
        refused.append("response_format")
    refused.extend(_content_types(body.get("messages")))
    return refused
