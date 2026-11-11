"""
Provider admission — a cap on the requests a provider runs at once (PLU-9).

Every request in flight holds a lease on its provider, whatever the path
(HTTP, stream, CLI), given back in a ``finally``. A provider may cap how
many leases it hands out (D-22): a local one (``sovereign``, loopback or
private address, or an Ollama over plain ``http://``, whatever its host)
takes one request at a time by default
(``PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT``); the others have no cap unless
their record sets ``max_concurrency``. When every candidate is full, a
request waits at most ``PLUGBOARD_PROVIDER_WAIT_S`` for the first freed
slot, oldest waiter first (D-23).

The state lives in this process's memory: each worker keeps its own.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque, Dict, Optional, Sequence

from plugboard import envcompat
from plugboard.billing import _is_local_host, _is_ollama

# Cap of a local provider whose record sets no ``max_concurrency``; 0 = none.
LOCAL_DEFAULT = envcompat.get_int(
    "PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT", default=1, minimum=0
)
# Seconds a request waits when every candidate is full; 0 = no wait.
WAIT_S = envcompat.get_float("PLUGBOARD_PROVIDER_WAIT_S", default=10.0, minimum=0.0)

# Leases held, by provider id (no entry = none).
_active: Dict[str, int] = {}
# Last cap seen for each provider id (0 = no cap).
_caps: Dict[str, int] = {}
# Requests waiting for a slot, by provider id, oldest first. A request that
# waits on several providers has the same future in each of their queues.
_waiters: Dict[str, Deque["asyncio.Future[str]"]] = {}


def is_local(provider: Dict[str, Any]) -> bool:
    """A provider on this machine or its private network: ``sovereign``, a
    ``localhost`` / loopback / private IP literal (``billing._is_local_host``),
    or an Ollama reached over plain ``http://``. A CLI never is."""
    if str(provider.get("auth_source") or "").startswith("subprocess:"):
        return False
    if provider.get("provider_type") == "sovereign":
        return True
    base_url = str(provider.get("base_url") or "")
    if _is_local_host(base_url):
        return True
    return _is_ollama(provider) and base_url.lower().startswith("http://")


def max_inflight(provider: Dict[str, Any]) -> int:
    """Requests ``provider`` may run at once; 0 means no cap.

    Its ``max_concurrency`` wins when it is a whole number >= 0 (not a
    boolean); otherwise a local provider gets ``LOCAL_DEFAULT`` and the
    others no cap.
    """
    value = provider.get("max_concurrency")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return LOCAL_DEFAULT if is_local(provider) else 0


def in_flight(provider_id: str) -> int:
    """Leases held on ``provider_id``."""
    return _active.get(provider_id, 0)


def waiting(provider_id: str) -> int:
    """Requests still waiting for a slot on ``provider_id``."""
    return sum(1 for fut in _waiters.get(provider_id, ()) if not fut.done())


class Lease:
    """One request's slot on a provider. ``release()`` gives it back once;
    ``with lease:`` gives it back on exit."""

    __slots__ = ("provider_id", "_held")

    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self._held = True

    def release(self) -> None:
        if self._held:
            self._held = False
            _release(self.provider_id)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _live(pid: str) -> Optional[Deque["asyncio.Future[str]"]]:
    """The waiters of ``pid`` still waiting, oldest first (done ones dropped)."""
    queue = _waiters.get(pid)
    while queue and queue[0].done():
        queue.popleft()
    if not queue:
        _waiters.pop(pid, None)
        return None
    return queue


def _wake(pid: str) -> None:
    """Hand the free slots of ``pid`` to its oldest waiters."""
    cap = _caps.get(pid, 0)
    while (queue := _live(pid)) and (cap == 0 or _active.get(pid, 0) < cap):
        _active[pid] = _active.get(pid, 0) + 1
        queue.popleft().set_result(pid)


def _release(pid: str) -> None:
    left = _active.get(pid, 0) - 1
    if left > 0:
        _active[pid] = left
    else:
        _active.pop(pid, None)
    _wake(pid)


def try_acquire(provider: Dict[str, Any]) -> Optional[Lease]:
    """A lease on ``provider`` now, or None when it is at its cap or others
    already wait for it."""
    pid = provider["id"]
    cap = _caps[pid] = max_inflight(provider)
    if cap and (_active.get(pid, 0) >= cap or _live(pid)):
        return None  # full, or others queued first
    _active[pid] = _active.get(pid, 0) + 1
    return Lease(pid)


async def wait_acquire(
    providers: Sequence[Dict[str, Any]], timeout: float
) -> Optional[Lease]:
    """A lease on the first of ``providers`` to free a slot, waiting behind
    the requests queued before this one; None after ``timeout`` seconds.

    A slot handed over just as the wait ends (timeout or cancellation) goes
    to the next waiter.
    """
    if timeout <= 0 or not providers:
        return None
    fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    for provider in providers:
        _caps[provider["id"]] = max_inflight(provider)
        _waiters.setdefault(provider["id"], deque()).append(fut)
    for provider in providers:
        if not fut.done():
            _wake(provider["id"])
    try:
        async with asyncio.timeout(timeout):
            pid = await fut
    except BaseException as exc:
        if fut.done() and not fut.cancelled():
            _release(fut.result())  # granted as we gave up: pass it on
        else:
            fut.cancel()
        for provider in providers:
            _live(provider["id"])
        if isinstance(exc, TimeoutError):
            return None
        raise
    for provider in providers:
        _live(provider["id"])  # drop it from the queues it was not served by
    return Lease(pid)


def reset() -> None:
    """Forget every lease, cap and waiter (tests)."""
    _active.clear()
    _caps.clear()
    _waiters.clear()
