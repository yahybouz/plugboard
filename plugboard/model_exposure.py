"""Which models a provider exposes: every model, or the latest versions only.

``PLUGBOARD_MODEL_EXPOSE`` picks the policy:

- ``latest`` (default): per model family, only the ``PLUGBOARD_MODEL_EXPOSE_VERSIONS``
  newest versions (default 2) are listed, e.g. ``claude-opus-5-5`` and
  ``claude-opus-4-7`` but not ``claude-opus-4-5``. A provider's default model
  and models already used for billing are always kept.
- ``all``: every model the provider reports.

Dated snapshots of one version (``claude-sonnet-4-5-20250929``) count as that
version. Models without a version number are always listed.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Set, Tuple

from plugboard import envcompat

log = logging.getLogger(__name__)

MODES = ("latest", "all")
DEFAULT_VERSIONS = 2
_warned: Set[str] = set()


def mode() -> str:
    raw = (envcompat.get("PLUGBOARD_MODEL_EXPOSE") or "latest").strip().lower()
    if raw not in MODES:
        if "mode" not in _warned:
            _warned.add("mode")
            log.warning(
                "PLUGBOARD_MODEL_EXPOSE=%r is not one of %s; using latest", raw, MODES
            )
        return "latest"
    return raw


def versions() -> int:
    raw = envcompat.get("PLUGBOARD_MODEL_EXPOSE_VERSIONS")
    if not raw:
        return DEFAULT_VERSIONS
    try:
        value = int(raw)
    except ValueError:
        if "versions" not in _warned:
            _warned.add("versions")
            log.warning(
                "PLUGBOARD_MODEL_EXPOSE_VERSIONS=%r is not a number; using %d",
                raw,
                DEFAULT_VERSIONS,
            )
        return DEFAULT_VERSIONS
    return max(1, min(10, value))


def _family_version(model_id: str) -> Tuple[str, Tuple[int, ...]]:
    from plugboard.model_availability import parse_family_and_version

    family, version = parse_family_and_version(model_id)
    # A dated snapshot (`-20250929`) is the same version as its base name.
    return family, tuple(v for v in version if v < 10_000_000)


def latest_ids(model_ids: Iterable[str], keep: int) -> Set[str]:
    """The ids whose version is among the ``keep`` newest of their family."""
    ids = list(dict.fromkeys(model_ids))
    by_family: Dict[str, Set[Tuple[int, ...]]] = {}
    parsed: Dict[str, Tuple[str, Tuple[int, ...]]] = {}
    for mid in ids:
        family, version = _family_version(mid)
        parsed[mid] = (family, version)
        if version:
            by_family.setdefault(family, set()).add(version)
    newest = {
        family: set(sorted(vs, reverse=True)[:keep]) for family, vs in by_family.items()
    }
    return {
        mid
        for mid, (family, version) in parsed.items()
        if not version or version in newest.get(family, set())
    }


def hidden_ids(model_ids: List[str], protected: Set[str]) -> List[str]:
    """Ids the current policy hides (never one of ``protected``)."""
    if mode() == "all":
        return []
    shown = latest_ids(model_ids, versions())
    return [mid for mid in model_ids if mid not in shown and mid not in protected]
