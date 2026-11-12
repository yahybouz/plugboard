"""One turn at a time per chat session (D-24, PLU-8).

Two turns of the same session sent together used to run side by side: the
second was answered without seeing the first, and on a full history one of
the two exchanges was lost when both were recorded. A turn now takes the
session's lock before its history is merged and gives it back once its
answer is sent and recorded; the next turn waits for it, in arrival order
(``asyncio.Lock`` is fair), for at most what is left of its request's wait
budget (PLU-15, ``request_wait``).

The lock is per ``(client_id, session_id)``: two clients that pick the same
session id never wait for each other. State lives in this process only, like
the rest of the load rules; the entry is dropped with its last user.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Dict, List, Optional, Tuple

# (client_id, session_id) -> [lock, users]: users counts the turn running
# plus the turns waiting behind it.
_turns: Dict[Tuple[str, str], List] = {}


def _leave(key: Tuple[str, str], entry: List) -> None:
    entry[1] -= 1
    if entry[1] == 0 and _turns.get(key) is entry:
        del _turns[key]


def busy(client_id: str, session_id: str) -> bool:
    """A turn of this session runs or waits: the next one would wait."""
    return (client_id, session_id) in _turns


async def acquire(
    client_id: str, session_id: str, timeout: Optional[float] = None
) -> Callable[[], None]:
    """Wait for the session's turn; return the function that gives it back.

    The returned function is idempotent. A wait that is cancelled (the caller
    hung up) or lasts ``timeout`` seconds (``TimeoutError``, PLU-15) leaves
    the queue without taking the turn; None waits without limit. A free
    turn is taken at once, whatever the timeout (0 included).
    """
    key = (client_id, session_id)
    entry = _turns.get(key)
    if entry is None:
        entry = _turns[key] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with asyncio.timeout(timeout):
            await entry[0].acquire()
    except BaseException:
        _leave(key, entry)
        raise
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        entry[0].release()
        _leave(key, entry)

    return release


def waiting(client_id: str, session_id: str) -> int:
    """Turns of this session waiting behind the one that runs."""
    entry = _turns.get((client_id, session_id))
    if entry is None:
        return 0
    return entry[1] - (1 if entry[0].locked() else 0)
