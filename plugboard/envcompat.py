"""Read environment variables, old names included.

Every ``LLM_ENABLER_<NAME>`` variable is copied to ``PLUGBOARD_<NAME>`` when
the new name is not set, so existing ``.env`` files, cron entries and
deployments keep working. The code itself only reads ``PLUGBOARD_*`` names.

``get_int`` and ``get_float`` are the one reader of numeric settings, and
``get_floats`` of comma-separated lists of numbers: a value that is not a
finite number (``nan`` and ``inf`` included), or is outside the bounds the
caller gives, falls back to the default with one warning.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Callable, List, MutableMapping, Optional, TypeVar, Union

OLD_PREFIX = "LLM_" + "ENABLER_"  # split so the rename sweep leaves it alone
NEW_PREFIX = "PLUGBOARD_"

log = logging.getLogger(__name__)
_warned: set = set()
_conflicts: set = set()
_bad_numbers: set = set()  # (name, raw) already reported

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


N = TypeVar("N", int, float)


def _number(
    name: str,
    legacy: tuple,
    default: N,
    cast: Callable[[str], N],
    minimum: Optional[Union[int, float]],
    above: Optional[Union[int, float]],
    maximum: Optional[Union[int, float]] = None,
) -> N:
    raw = get(name, *legacy)
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        problem = "is not a whole number" if cast is int else "is not a number"
    else:
        problem = _why_unusable(value, minimum, above, maximum)
        if problem is None:
            return value
    _report(name, raw, problem, default)
    return default


def _why_unusable(
    value: Union[int, float],
    minimum: Optional[Union[int, float]],
    above: Optional[Union[int, float]],
    maximum: Optional[Union[int, float]] = None,
) -> Optional[str]:
    """Why ``value`` cannot be used, or None when it can."""
    # A whole number is always finite (and may be too large for a float).
    if isinstance(value, float) and math.isnan(value):
        return "is not a number"
    if isinstance(value, float) and math.isinf(value):
        return "is not a finite number"
    if minimum is not None and value < minimum:
        return f"is below {minimum}"
    if above is not None and value <= above:
        return f"must be above {above}"
    if maximum is not None and value > maximum:
        return f"is above {maximum}"
    return None


def _report(name: str, raw: str, problem: str, default: object) -> None:
    if (name, raw) not in _bad_numbers:
        _bad_numbers.add((name, raw))
        log.warning("%s=%r %s; using %s", name, raw, problem, default)


def get_int(
    name: str,
    *legacy: str,
    default: int,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Whole number from ``name`` (or a legacy name), else ``default``.

    A value that is not a whole number, is below ``minimum`` or above
    ``maximum`` gives the default, with one warning per value.
    """
    return _number(name, legacy, default, int, minimum, None, maximum)


def get_float(
    name: str,
    *legacy: str,
    default: float,
    minimum: Optional[float] = None,
    above: Optional[float] = None,
) -> float:
    """Number from ``name`` (or a legacy name), else ``default``.

    A value that is not a finite number (NaN or infinity), is below
    ``minimum``, or is not greater than ``above`` gives the default, with one
    warning per value.
    """
    return _number(name, legacy, default, float, minimum, above)


def get_floats(
    name: str,
    *legacy: str,
    default: List[float],
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> List[float]:
    """Comma-separated numbers from ``name`` (or a legacy name), else ``default``.

    Empty items are skipped, and a value with no item gives the default. One
    item that is not a finite number (NaN or infinity), is below ``minimum``
    or is above ``maximum`` gives the whole default, with one warning per
    value.
    """
    raw = get(name, *legacy)
    values: List[float] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        try:
            value = float(item)
        except ValueError:
            problem: Optional[str] = "is not a number"
        else:
            problem = _why_unusable(value, minimum, None, maximum)
        if problem is not None:
            _report(name, raw, f"item {item!r} {problem}", default)
            return list(default)
        values.append(value)
    return values or list(default)
