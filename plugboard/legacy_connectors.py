"""Rejection tombstones for a removed connector, never a routing codec.

Only explicit IDs/metadata identify old records. Arbitrary custom names and
OpenAI-compatible URLs cannot identify their implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from plugboard.model_identity import catalogue_native_model, registered_model_owner

REMOVED_CONNECTOR_ERROR = "Unsupported/removed legacy connector: OpenClaw; configure a direct provider manually."
_REMOVED_IDS = frozenset({"openclaw", "openclaw-llm-proxy"})
_ROUTING_ALIASES = frozenset(
    {
        "ollama",
        "anthropic",
        "claude",
        "claude-via-claude-cli",
        "claude-code",
        "openai",
        "codex",
        "chatgpt",
        "chatgpt-via-codex-cli",
        "google",
        "gemini",
        "google-gemini-cli",
        "google-gemini-via-gemini-cli",
        "gemini-cli",
        "gemini-cli-subprocess",
        "codex-cli",
        "codex-cli-subprocess",
        "claude-cli",
        "claude-cli-subprocess",
    }
)


def is_removed_provider(provider: Mapping[str, Any]) -> bool:
    """Match explicit legacy connector metadata, without substring guessing."""
    return any(
        str(provider.get(field, "") or "").strip().lower().partition(":")[0]
        in _REMOVED_IDS
        for field in ("id", "type", "provider_type", "auth_source", "connector")
    )


def require_supported_provider(provider: Mapping[str, Any]) -> None:
    if is_removed_provider(provider):
        raise ValueError(REMOVED_CONNECTOR_ERROR)


def is_removed_model(
    model: str, *, provider_ids: Iterable[str] = (), effective_name: bool = False
) -> bool:
    """Reject explicit wrappers; native model tags/revisions remain opaque."""
    if not isinstance(model, str):
        return False
    name = model.strip().lower()
    end = name.find(":")
    if name[: end if end >= 0 else len(name)] in _REMOVED_IDS:
        return True
    start = 0
    owner = registered_model_owner(name, (pid.lower() for pid in provider_ids))
    if owner is not None:
        start = len(owner) + 1
        effective_name = True
    # Only known routing aliases consume colons. Unknown model:tag is opaque.
    # Advance indices over the original normalized string: copying each remaining
    # suffix makes deeply nested caller aliases quadratic.
    while True:
        end = name.find(":", start)
        prefix = name[start : end if end >= 0 else len(name)]
        if prefix in _REMOVED_IDS or name.startswith("openclaw/", start):
            return True
        if (
            end >= 0
            and prefix in _ROUTING_ALIASES
            and name.startswith("openclaw/", end + 1)
        ):
            return True
        if effective_name or end < 0 or prefix not in _ROUTING_ALIASES:
            return False
        start = end + 1


def is_removed_catalogue_model(row: Mapping[str, Any]) -> bool:
    """Classify the catalogue's owner and native ID, not its display spelling."""
    owner = row.get("provider") or row.get("owned_by") or ""
    return is_removed_provider({"id": owner}) or is_removed_model(
        catalogue_native_model(row), effective_name=True
    )


def require_supported_model(
    model: str, *, provider_ids: Iterable[str] = (), effective_name: bool = False
) -> None:
    if is_removed_model(
        model, provider_ids=provider_ids, effective_name=effective_name
    ):
        raise ValueError(REMOVED_CONNECTOR_ERROR)
