"""One wait per chat request (PLU-15).

A chat request may wait in three places before a provider works on it: for a
slot under the global cap (``PLUGBOARD_MAX_INFLIGHT_CHAT``), for its
session's turn (D-24) and for a slot on a provider (D-23). Each wait had its
own clock, the session's none at all, so a request could wait 10 s, then
without end, then 10 s more. They now share one deadline per request.

The first wait sets the deadline, ``length`` seconds later, and every later
wait ends at it at the latest; a wait may also have a shorter ceiling of its
own (``PLUGBOARD_INFLIGHT_WAIT_S``, ``PLUGBOARD_PROVIDER_WAIT_S``). Time spent
before the first wait (the checks, a provider that failed before the request
moved to a full one) does not count. Outside a chat request (an admin probe,
a direct call) there is no deadline: a wait lasts its own ceiling, as before.

The deadline lives in a context variable, set by the chat admission once the
key is checked; the router and the session turn read it in the same request.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Optional


class RequestWait:
    """The wait budget of one request; its clock starts at the first wait."""

    __slots__ = ("length", "deadline")

    def __init__(self, length: float) -> None:
        self.length = max(0.0, length)
        self.deadline: Optional[float] = None

    def remaining(self, ceiling: Optional[float] = None) -> float:
        """Seconds the wait about to begin may last (sets the deadline once)."""
        now = time.monotonic()
        if self.deadline is None:
            self.deadline = now + self.length
        left = max(0.0, self.deadline - now)
        return left if ceiling is None else min(left, max(0.0, ceiling))


_current: ContextVar[Optional[RequestWait]] = ContextVar(
    "plugboard_request_wait", default=None
)


def start(length: float) -> RequestWait:
    """Give the current request a fresh budget of ``length`` seconds."""
    wait = RequestWait(length)
    _current.set(wait)
    return wait


def remaining(ceiling: Optional[float] = None) -> Optional[float]:
    """Seconds the wait about to begin may last, at most ``ceiling``.

    The first call of a request sets its deadline: call it only when the
    request is about to wait. Outside a request, the ceiling alone (None:
    no limit).
    """
    wait = _current.get()
    if wait is None:
        return None if ceiling is None else max(0.0, ceiling)
    return wait.remaining(ceiling)
