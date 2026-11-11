"""
Route pauses — a route that answered 429 is tried last for a while (PLU-9).

A route (``provider-id:model``, ``router._circuit_key``) whose provider said
its quota or rate limit is exhausted, by an HTTP 429 or a CLI quota error,
is paused for the ``Retry-After`` the provider gave; without one, for 30 s,
1 min, then 5 min as its 429s follow one another. A success on the route
clears its pause and its count.

A pause only reorders the candidates (D-23, rule 3): the router tries a
paused route after the others, and still calls it when nothing else is
left. It never refuses a request.

The state lives in this process's memory: each worker keeps its own.
"""

from __future__ import annotations

import time
from typing import Dict, Optional, Tuple

# Pause, in seconds, after the 1st, 2nd, then 3rd and later 429 in a row
# when the provider gives no Retry-After.
STEPS_S = (30, 60, 300)

# Clock of the pauses; a test replaces it.
_now = time.monotonic

# Paused routes: (end of the pause on the ``_now`` clock, 429s in a row).
_pauses: Dict[str, Tuple[float, int]] = {}


def pause(route: str, retry_after: Optional[int]) -> float:
    """Count one more 429 on ``route`` and pause it; return the seconds set.

    The pause lasts ``retry_after`` when the provider gave one, else the
    step of ``STEPS_S`` matching the 429s in a row.
    """
    _, count = _pauses.get(route, (0.0, 0))
    count += 1
    if retry_after is not None:
        seconds = float(max(0, retry_after))
    else:
        seconds = float(STEPS_S[min(count, len(STEPS_S)) - 1])
    _pauses[route] = (_now() + seconds, count)
    return seconds


def clear(route: str) -> None:
    """Forget the pause and the 429 count of ``route`` (it answered)."""
    _pauses.pop(route, None)


def paused_for(route: str) -> float:
    """Seconds left on the pause of ``route``; 0.0 when it has none."""
    entry = _pauses.get(route)
    if entry is None:
        return 0.0
    return max(0.0, entry[0] - _now())


def reset() -> None:
    """Forget every pause (tests)."""
    _pauses.clear()
