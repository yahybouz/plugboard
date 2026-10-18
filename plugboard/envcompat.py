"""Accept the environment variable names used before the Plugboard rename.

Every ``LLM_ENABLER_<NAME>`` variable is copied to ``PLUGBOARD_<NAME>`` when
the new name is not set, so existing ``.env`` files, cron entries and
deployments keep working. The code itself only reads ``PLUGBOARD_*`` names.
"""

from __future__ import annotations

import logging
import os
from typing import List, MutableMapping, Optional

OLD_PREFIX = "LLM_" + "ENABLER_"  # split so the rename sweep leaves it alone
NEW_PREFIX = "PLUGBOARD_"

log = logging.getLogger(__name__)
_warned: set = set()
_conflicts: set = set()

# Opt-outs where the stricter value wins when both names are set: an
# operator who disabled a feature under the old name keeps it disabled.
_STRICT_OFF = {"LOCAL_AUTO_LOGIN"}

# Not carried over: the old DEV flag only quietened a warning, while
# PLUGBOARD_DEV=1 lets the server start without ADMIN_KEY. Opt in explicitly.
_NOT_COPIED = {"DEV"}


def apply(environ: Optional[MutableMapping[str, str]] = None) -> List[str]:
    """Copy legacy names to the new prefix; return the legacy names copied."""
    env = os.environ if environ is None else environ
    copied = []
    for key in list(env):
        if not key.startswith(OLD_PREFIX):
            continue
        new_key = NEW_PREFIX + key[len(OLD_PREFIX) :]
        if key[len(OLD_PREFIX) :] in _NOT_COPIED:
            if key not in _conflicts:
                _conflicts.add(key)
                log.warning(
                    "%s is ignored: set %s=1 explicitly for local development.",
                    key,
                    new_key,
                )
            continue
        if new_key not in env:
            env[new_key] = env[key]
            copied.append(key)
        elif env[new_key] != env[key]:
            strict = key[len(OLD_PREFIX) :] in _STRICT_OFF and env[key].strip() == "0"
            if strict:
                env[new_key] = "0"
            if key not in _conflicts:
                _conflicts.add(key)
                log.warning(
                    "%s and %s are both set with different values; %s. "
                    "Remove the old name.",
                    key,
                    new_key,
                    "the opt-out (0) is kept" if strict else "the new name is used",
                )
    fresh = [k for k in copied if k not in _warned]
    if fresh:
        _warned.update(fresh)
        log.warning(
            "Deprecated environment variable names %s: rename them to %s*",
            ", ".join(sorted(fresh)),
            NEW_PREFIX,
        )
    return copied


def get(name: str, *legacy: str, default: str = "") -> str:
    """Value of ``name``, else of the first legacy name that is set.

    For settings renamed without the old prefix convention (for example
    ``PRICING_REFRESH_INTERVAL_H`` -> ``PLUGBOARD_PRICING_REFRESH_INTERVAL_H``).
    The new name always wins; reading an old name logs a warning once.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for old in legacy:
        old_value = os.environ.get(old, "").strip()
        if old_value:
            if old not in _warned:
                _warned.add(old)
                log.warning("%s is deprecated: rename it to %s", old, name)
            return old_value
    return default
