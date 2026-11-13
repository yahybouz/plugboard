"""The global cap of running chats (request_wait.ChatCap, PLU-48)."""

import asyncio

import pytest

from plugboard.request_wait import ChatCap


async def test_free_permits_are_taken_at_once():
    cap = ChatCap(2)
    assert not cap.locked()
    await cap.acquire()
    await cap.acquire_first()
    assert cap.locked() and cap._value == 0
    cap.release()
    cap.release()
    assert cap._value == 2 and cap._waiters == []


async def test_front_of_the_line_goes_before_earlier_requests():
    cap = ChatCap(1)
    await cap.acquire()
    order = []

    async def take(name, first):
        await (cap.acquire_first() if first else cap.acquire())
        order.append(name)

    back_1 = asyncio.create_task(take("back-1", False))
    await asyncio.sleep(0)
    back_2 = asyncio.create_task(take("back-2", False))
    await asyncio.sleep(0)
    front = asyncio.create_task(take("front", True))
    await asyncio.sleep(0)
    for _ in range(3):
        cap.release()
        await asyncio.sleep(0)
    await asyncio.gather(back_1, back_2, front)
    assert order == ["front", "back-1", "back-2"]


async def test_a_new_request_queues_behind_waiters_but_the_front_does_not():
    cap = ChatCap(1)
    await cap.acquire()
    waiter = asyncio.create_task(cap.acquire())
    await asyncio.sleep(0)
    cap._value += 1  # an artificial state: a free permit beside a waiter
    assert cap.would_wait(first=False)
    assert not cap.would_wait(first=True)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


async def test_a_wait_that_times_out_leaves_no_permit_behind():
    cap = ChatCap(1)
    await cap.acquire()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(cap.acquire_first(), 0.01)
    assert cap._waiters == []
    cap.release()
    assert cap._value == 1


async def test_a_permit_handed_to_a_cancelled_wait_is_passed_on():
    cap = ChatCap(1)
    await cap.acquire()
    first = asyncio.create_task(cap.acquire())
    second = asyncio.create_task(cap.acquire())
    await asyncio.sleep(0)
    cap.release()  # handed to ``first``, which is cancelled before it runs
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, 1.0)
    assert cap._value == 0
    cap.release()
    assert cap._value == 1 and cap._waiters == []
