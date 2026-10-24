"""Owner boundaries shared by routing, publication and health observations.

Display aliases are only defined for complete provider IDs. In particular a
registered owner containing ':' is never shortened by rewriting its first token.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_DISPLAY_OWNERS = {
    "claude-via-claude-cli": "claude-code",
    "chatgpt-via-codex-cli": "Codex",
    "google-gemini-via-gemini-cli": "Gemini",
}


def registered_model_owner(model: str, provider_ids: Iterable[str]) -> str | None:
    """Return the longest explicitly registered prefix, never a display label."""
    return max(
        (pid for pid in provider_ids if pid and model.startswith(pid + ":")),
        key=len,
        default=None,
    )


def published_model_id(
    owner: str, native: str, provider_ids: Iterable[str]
) -> str | None:
    """Use an existing safe spelling, or omit an inexpressible identity.

    Inactive and quarantined owners still reserve their namespace. Materialize
    iterators once so the alias and canonical fallback see the same records.
    """
    provider_ids = tuple(provider_ids)
    display_owner = _DISPLAY_OWNERS.get(owner, owner)
    for prefix in dict.fromkeys((display_owner, owner)):
        proposed = f"{prefix}:{native}"
        registered = registered_model_owner(proposed, provider_ids)
        if registered is None or registered == owner:
            return proposed
    return None


def catalogue_native_model(
    row: Mapping[str, Any], *, provider_ids: Iterable[str] = ()
) -> str:
    """Recover the opaque native ID using the row's actual owner boundary."""
    model = row.get("id", "") or ""
    owner = (
        row.get("provider")
        or row.get("owned_by")
        or registered_model_owner(model, provider_ids)
        or ""
    )
    for prefix in (owner, _DISPLAY_OWNERS.get(owner, owner)):
        if prefix and model.startswith(prefix + ":"):
            return model[len(prefix) + 1 :]
    return model


def observation_owner(
    model: str, observation: Mapping[str, Any], provider_ids: Iterable[str]
) -> str | None:
    """An explicit observation owner precedes any historical key spelling."""
    return observation.get("provider") or registered_model_owner(model, provider_ids)
