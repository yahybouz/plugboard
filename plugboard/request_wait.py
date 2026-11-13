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

The budget also carries the request's slot under the global chat cap
(BUG-77): a request gives it back while it waits for a provider or for its
session's turn (``slot_released``), so requests waiting for a full provider
never keep the requests for the other providers out, and takes it back,
within the budget, before it goes on.
"""

from __future__ import annotations

import contextlib
import time
from contextvars import ContextVar
from typing import AsyncIterator, Iterator, Optional, Protocol


class ChatSlot(Protocol):
    """A request's slot under the global chat cap (owned by the chat admission)."""

    held: bool
    wait_s: float  # ceiling of a wait for it

    async def take(self, wait_s: float) -> None:
        """Take the slot, waiting at most ``wait_s``; raise when it cannot."""

    def release(self) -> None:
        """Give the slot back (no-op when not held)."""


class RequestWait:
    """The wait budget of one request: ``length`` seconds of waiting in all."""

    __slots__ = ("length", "spent", "slot")

    def __init__(self, length: float, slot: Optional[ChatSlot] = None) -> None:
        self.length = max(0.0, length)
        self.spent = 0.0
        self.slot = slot

    def left(self) -> float:
        return max(0.0, self.length - self.spent)


_current: ContextVar[Optional[RequestWait]] = ContextVar(
    "plugboard_request_wait", default=None
)


def start(length: float, slot: Optional[ChatSlot] = None) -> RequestWait:
    """Give the current request a fresh budget of ``length`` seconds, and the
    slot it holds under the global chat cap, if any."""
    budget = RequestWait(length, slot)
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


class StepOut:
    """Yielded by ``slot_released``: set ``come_back`` to False when the
    request will not call a provider after this wait (it ends with a refusal)."""

    __slots__ = ("come_back",)

    def __init__(self) -> None:
        self.come_back = True


def _held_slot() -> Optional[ChatSlot]:
    budget = _current.get()
    return budget.slot if budget is not None else None


@contextlib.asynccontextmanager
async def slot_released() -> AsyncIterator[StepOut]:
    """Give the request's global slot back while it waits, then take it back.

    For a wait for something else than the slot itself: a provider's slot or
    the session's turn. The slot is taken back, within the wait budget (its
    own ceiling ``PLUGBOARD_INFLIGHT_WAIT_S`` included), only when the wait
    ends normally and ``come_back`` is still True; a wait that raises (timed
    out, client gone) leaves the request without it. Without a slot (no
    global cap, outside a chat request), nothing happens.
    """
    step = StepOut()
    slot = _held_slot()
    if slot is None or not slot.held:
        yield step
        return
    slot.release()
    yield step
    if step.come_back:
        with waiting(slot.wait_s) as wait_s:
            await slot.take(wait_s)


async def slot_back() -> None:
    """Take the request's global slot back when a wait left without it (a
    preflight goes on with its next ping after a refusal)."""
    slot = _held_slot()
    if slot is not None and not slot.held:
        with waiting(slot.wait_s) as wait_s:
            await slot.take(wait_s)
