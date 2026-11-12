"""One wait budget per chat request (PLU-15).

A chat request may wait in three places before a provider works on it: for a
slot under the global cap (``PLUGBOARD_MAX_INFLIGHT_CHAT``), for its
session's turn (D-24) and for a slot on a provider (D-23). Each wait had its
own clock, the session's none at all, so a request could wait 10 s, then
without end, then 10 s more. They now share one budget per request: the time
really spent waiting is added up, and each wait gets at most what is left,
within its own ceiling (``PLUGBOARD_INFLIGHT_WAIT_S``,
``PLUGBOARD_PROVIDER_WAIT_S``). Time spent working (the checks, a provider
answering or failing) does not count.

Outside a chat request (an admin probe, a direct call) there is no budget:
a wait lasts its own ceiling, as before. The budget lives in a context
variable, set by the chat admission once the key is checked; the router and
the session turn use it in the same request.
"""

from __future__ import annotations

import contextlib
import time
from contextvars import ContextVar
from typing import Iterator, Optional


class RequestWait:
    """The wait budget of one request: ``length`` seconds of waiting in all."""

    __slots__ = ("length", "spent")

    def __init__(self, length: float) -> None:
        self.length = max(0.0, length)
        self.spent = 0.0

    def left(self) -> float:
        return max(0.0, self.length - self.spent)


_current: ContextVar[Optional[RequestWait]] = ContextVar(
    "plugboard_request_wait", default=None
)


def start(length: float) -> RequestWait:
    """Give the current request a fresh budget of ``length`` seconds."""
    budget = RequestWait(length)
    _current.set(budget)
    return budget


@contextlib.contextmanager
def waiting(ceiling: Optional[float] = None) -> Iterator[Optional[float]]:
    """Time one wait: give the seconds it may last, then count what it took.

    At most ``ceiling`` and what is left of the request's budget; outside a
    request, ``ceiling`` alone (None: no limit). The time is counted however
    the wait ends: served, timed out, or cancelled.
    """
    if ceiling is not None:
        ceiling = max(0.0, ceiling)
    budget = _current.get()
    if budget is None:
        yield ceiling
        return
    left = budget.left()
    started = time.monotonic()
    try:
        yield left if ceiling is None else min(left, ceiling)
    finally:
        budget.spent += time.monotonic() - started
