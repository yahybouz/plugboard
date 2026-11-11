"""Per-provider concurrency cap, leases and bounded FIFO wait (PLU-9)."""

import asyncio

import pytest

from plugboard import provider_admission as pa

LOCAL = {"id": "ollama", "base_url": "http://127.0.0.1:11434"}
CLOUD = {"id": "openai", "base_url": "https://api.openai.com"}


@pytest.mark.parametrize(
    "provider",
    [
        {"id": "x", "base_url": "https://llm.example", "provider_type": "sovereign"},
        {"id": "x", "base_url": "http://127.0.0.1:8080"},
        {"id": "x", "base_url": "http://localhost:1234"},
        {"id": "x", "base_url": "http://192.168.1.20:8000"},
        {"id": "x", "base_url": "http://[::1]:11434"},
        {"id": "ollama-box", "base_url": "http://ollama:11434"},
        {"id": "gpu", "base_url": "http://gpu.lan:11434"},
    ],
    ids=[
        "sovereign",
        "loopback",
        "localhost",
        "private",
        "ipv6-loopback",
        "ollama-dns",
        "ollama-port",
    ],
)
def test_local_providers_are_capped_at_one_by_default(provider):
    assert pa.is_local(provider)
    assert pa.max_inflight(provider) == 1


@pytest.mark.parametrize(
    "provider",
    [
        CLOUD,
        {"id": "ollama-cloud", "base_url": "https://ollama.com"},
        {
            "id": "cli",
            "base_url": "subprocess://claude",
            "auth_source": "subprocess:claude",
        },
        {"id": "x", "base_url": "http://127.0.0.1.nip.io:8080"},
    ],
    ids=["cloud", "ollama-cloud", "cli", "dns-lookalike"],
)
def test_other_providers_have_no_cap_by_default(provider):
    assert not pa.is_local(provider)
    assert pa.max_inflight(provider) == 0


def test_max_concurrency_field_wins_over_the_default():
    assert pa.max_inflight({**CLOUD, "max_concurrency": 3}) == 3
    assert pa.max_inflight({**LOCAL, "max_concurrency": 0}) == 0
    assert pa.max_inflight({**LOCAL, "max_concurrency": 4}) == 4


@pytest.mark.parametrize("bad", ["2", True, -1, 1.5, None])
def test_invalid_max_concurrency_keeps_the_default(bad):
    assert pa.max_inflight({**LOCAL, "max_concurrency": bad}) == 1
    assert pa.max_inflight({**CLOUD, "max_concurrency": bad}) == 0


def test_local_default_follows_the_setting(monkeypatch):
    monkeypatch.setattr(pa, "LOCAL_DEFAULT", 2)
    assert pa.max_inflight(LOCAL) == 2
    monkeypatch.setattr(pa, "LOCAL_DEFAULT", 0)
    assert pa.max_inflight(LOCAL) == 0


def test_capped_provider_refuses_a_lease_until_one_is_released():
    first = pa.try_acquire(LOCAL)
    assert first is not None and first.provider_id == "ollama"
    assert pa.try_acquire(LOCAL) is None
    assert pa.in_flight("ollama") == 1
    first.release()
    first.release()  # idempotent
    assert pa.in_flight("ollama") == 0 and pa._active == {}
    pa.try_acquire(LOCAL).release()


def test_uncapped_provider_counts_every_lease():
    leases = [pa.try_acquire(CLOUD) for _ in range(3)]
    assert all(leases) and pa.in_flight("openai") == 3
    with leases[0]:
        pass
    assert pa.in_flight("openai") == 2
    for lease in leases[1:]:
        lease.release()
    assert pa._active == {}


async def test_freed_slot_goes_to_the_waiter_not_to_a_newcomer():
    held = pa.try_acquire(LOCAL)
    waiter = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    assert pa.waiting("ollama") == 1
    assert pa.try_acquire(LOCAL) is None
    held.release()
    lease = await waiter
    assert lease.provider_id == "ollama" and pa.in_flight("ollama") == 1
    lease.release()
    assert pa._active == {}


async def test_waiters_are_served_in_arrival_order():
    held = pa.try_acquire(LOCAL)
    first = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    second = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    held.release()
    lease = await first
    assert not second.done()
    lease.release()
    (await second).release()
    assert pa._active == {}


async def test_wait_gives_up_after_its_timeout():
    held = pa.try_acquire(LOCAL)
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await pa.wait_acquire([LOCAL], 0.05) is None
    assert loop.time() - started >= 0.05
    assert pa.waiting("ollama") == 0
    held.release()
    assert pa.in_flight("ollama") == 0


async def test_zero_timeout_does_not_wait():
    held = pa.try_acquire(LOCAL)
    assert await pa.wait_acquire([LOCAL], 0) is None
    held.release()


async def test_cancelled_waiter_leaves_the_queue():
    held = pa.try_acquire(LOCAL)
    waiter = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    held.release()
    assert pa.in_flight("ollama") == 0


async def test_slot_granted_while_the_waiter_is_cancelled_is_given_back():
    held = pa.try_acquire(LOCAL)
    waiter = asyncio.create_task(pa.wait_acquire([LOCAL], 5))
    await asyncio.sleep(0)
    held.release()  # hands the slot to the waiter...
    waiter.cancel()  # ...which is cancelled before it runs
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert pa.in_flight("ollama") == 0


async def test_waiter_on_two_providers_takes_the_first_freed():
    other = {"id": "llama", "base_url": "http://10.0.0.5:8080"}
    a, b = pa.try_acquire(LOCAL), pa.try_acquire(other)
    waiter = asyncio.create_task(pa.wait_acquire([LOCAL, other], 5))
    await asyncio.sleep(0)
    b.release()
    lease = await waiter
    assert lease.provider_id == "llama"
    a.release()
    assert pa.in_flight("ollama") == 0  # not handed to the served waiter
    lease.release()


async def test_lowered_cap_is_respected_when_slots_come_back():
    roomy = {**LOCAL, "max_concurrency": 2}
    one, two = pa.try_acquire(roomy), pa.try_acquire(roomy)
    tight = {**LOCAL, "max_concurrency": 1}
    waiter = asyncio.create_task(pa.wait_acquire([tight], 5))
    await asyncio.sleep(0)
    one.release()  # 1 in flight = the new cap
    await asyncio.sleep(0)
    assert not waiter.done() and pa.in_flight("ollama") == 1
    two.release()
    lease = await waiter
    assert pa.in_flight("ollama") == 1
    lease.release()
