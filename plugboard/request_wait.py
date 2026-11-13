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
session's turn (``release_slot``), so requests waiting for a full provider
never keep the requests for the other providers out. It takes the slot back
right before a provider is called (``slot_back``, PLU-51), within the
budget, and ahead of the requests that hold no provider's slot (``ChatCap``,
PLU-48): a provider's slot is not kept idle behind them, and a provider
turned off meanwhile is dropped before any wait for the slot (PLU-49).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from contextvars import ContextVar
from typing import Deque, Iterator, List, Optional, Protocol


class ChatCap:
    """The global cap of running chat requests (``PLUGBOARD_MAX_INFLIGHT_CHAT``).

    A semaphore, first come first served, with a front of the line: a request
    that already holds a provider's slot (``acquire_first``) goes before the
    requests that hold none (``acquire``), so that slot is not kept idle
    (PLU-48). A wait that ends (timeout, cancellation) as the slot is handed
    to it passes the slot on.
    """

    def __init__(self, value: int) -> None:
        self._value = value
        self._front: Deque["asyncio.Future[None]"] = deque()
        self._back: Deque["asyncio.Future[None]"] = deque()

    @property
    def _waiters(self) -> List["asyncio.Future[None]"]:
        """Requests still waiting, the front of the line first."""
        return [f for f in (*self._front, *self._back) if not f.done()]

    def locked(self) -> bool:
        """Whether a new request would wait."""
        return self.would_wait(first=False)

    def would_wait(self, first: bool) -> bool:
        """Whether an ``acquire`` (``first``: ``acquire_first``) would wait.

        ``release`` hands a free permit to a waiter at once, so a free permit
        and a live waiter never coexist; the waiter test is a safety net."""
        if first:
            return self._value <= 0
        return self._value <= 0 or any(
            not f.done() for line in (self._front, self._back) for f in line
        )

    async def acquire(self) -> bool:
        return await self._acquire(self._back)

    async def acquire_first(self) -> bool:
        return await self._acquire(self._front)

    async def _acquire(self, line: "Deque[asyncio.Future[None]]") -> bool:
        if not self.would_wait(first=line is self._front):
            self._value -= 1
            return True
        fut: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()
        line.append(fut)
        try:
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                self.release()  # handed over as the wait ended: pass it on
            else:
                fut.cancel()
                try:
                    line.remove(fut)
                except ValueError:
                    pass
            raise
        return True

    def release(self) -> None:
        self._value += 1
        while self._value > 0:
            fut = self._next()
            if fut is None:
                return
            self._value -= 1
            fut.set_result(None)

    def _next(self) -> "Optional[asyncio.Future[None]]":
        for line in (self._front, self._back):
            while line:
                fut = line.popleft()
                if not fut.done():
                    return fut
        return None


class ChatSlot(Protocol):
    """A request's slot under the global chat cap (owned by the chat admission)."""

    held: bool
    wait_s: float  # ceiling of a wait for it

    async def take(self, wait_s: float, first: bool = False) -> None:
        """Take the slot, waiting at most ``wait_s`` (``first``: ahead of the
        requests that hold no provider's slot); raise when it cannot."""

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


def _held_slot() -> Optional[ChatSlot]:
    budget = _current.get()
    return budget.slot if budget is not None else None


def release_slot() -> None:
    """Give the request's global slot back before a wait for something else
    than the slot itself: a provider's slot or the session's turn (BUG-77).

    Nothing takes it back here: ``slot_back`` does, right before a provider
    is called. Without a slot (no global cap, outside a chat request),
    nothing happens.
    """
    slot = _held_slot()
    if slot is not None:
        slot.release()


async def slot_back(first: bool = False) -> bool:
    """Make sure the request holds its global slot before a provider is
    called (PLU-51): take it back if a wait gave it away, within the wait
    budget (its own ceiling ``PLUGBOARD_INFLIGHT_WAIT_S`` included). With
    ``first``, the request holds a scarce provider's slot and goes ahead of
    those that hold none (PLU-48). Raises the slot's refusal when the cap
    stays full. Without a slot, nothing happens.

    True when the slot had to be taken back: time may have passed, and the
    caller checks again what may have changed meanwhile (a provider turned
    off, PLU-12)."""
    slot = _held_slot()
    if slot is None or slot.held:
        return False
    with waiting(slot.wait_s) as wait_s:
        await slot.take(wait_s, first=first)
    return True
