"""Ollama switch in the routing core (PLU-12): a disabled provider is never called.

forward() and forward_stream() refuse a model only a disabled provider serves,
whoever calls them; a request that waited for a slot on a provider turned off
meanwhile is refused when its turn comes, and gives its slot back. A call
already running when the switch flips goes to its end.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aioresponses import aioresponses

from plugboard import provider_admission, router
from tests import test_router_admission as admission
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY, PROVIDER_B

capped_a = admission.capped_a  # fixture: test-provider takes one call at a time
AUTH = admission.AUTH
URL_A = admission.URL_A
URL_B = admission.URL_B
ONLY_A = admission.ONLY_A
BARE = admission.BARE  # both providers are candidates
_body = admission._body
_calls = admission._calls
_until = admission._until


def _turn_off(providers_file, provider, other=PROVIDER_B) -> None:
    """Write the registry with ``provider`` inactive, as the switch does."""
    off = {**provider, "is_active": False}
    providers_file.write_text(json.dumps({"providers": [off, other]}))
    router.invalidate_models_cache()


async def test_forward_refuses_a_disabled_model(capped_a, _providers_file):
    _turn_off(_providers_file, capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        with pytest.raises(router.ProviderDisabledError) as exc:
            await router.forward(_body(ONLY_A))
        assert _calls(mock, URL_A) == 0
    assert exc.value.provider_id == "test-provider"
    assert provider_admission._active == {}


async def test_forward_stream_refuses_a_disabled_model(capped_a, _providers_file):
    _turn_off(_providers_file, capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        with pytest.raises(router.ProviderDisabledError):
            await router.forward_stream(_body(ONLY_A, stream=True)).__anext__()
        assert _calls(mock, URL_A) == 0
    assert provider_admission._active == {}


async def test_waiter_is_refused_after_the_switch(
    capped_a, _providers_file, monkeypatch
):
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        task = asyncio.create_task(router.forward(_body(ONLY_A)))
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        _turn_off(_providers_file, capped_a)
        held.release()
        with pytest.raises(router.ProviderDisabledError):
            await asyncio.wait_for(task, 2)
        assert _calls(mock, URL_A) == 0
    assert provider_admission._active == {}, "the granted slot is given back"


async def test_stream_waiter_is_refused_after_the_switch(
    capped_a, _providers_file, monkeypatch
):
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        gen = router.forward_stream(_body(ONLY_A, stream=True))
        first = asyncio.ensure_future(gen.__anext__())
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        _turn_off(_providers_file, capped_a)
        held.release()
        with pytest.raises(router.ProviderDisabledError):
            await asyncio.wait_for(first, 2)
        assert _calls(mock, URL_A) == 0
    assert provider_admission._active == {}


async def test_running_stream_finishes_after_the_switch(capped_a, _providers_file):
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        gen = router.forward_stream(_body(ONLY_A, stream=True))
        chunks = [await gen.__anext__()]
        _turn_off(_providers_file, capped_a)
        chunks += [chunk async for chunk in gen]
    assert b"[DONE]" in b"".join(chunks)
    assert provider_admission._active == {}


async def test_waiter_still_served_when_its_provider_stays_on(capped_a, monkeypatch):
    """The re-check after a wait refuses nothing while the provider is on."""
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        task = asyncio.create_task(router.forward(_body(ONLY_A)))
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        held.release()
        await asyncio.wait_for(task, 2)
        assert _calls(mock, URL_A) == 1


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_chat_maps_a_late_refusal_to_503(client, monkeypatch, stream):
    """A refusal raised by the routing core gets the chat's 503 body."""
    import plugboard.app as app_module

    async def _forward(body, **_kwargs):
        raise router.ProviderDisabledError("test-provider")

    async def _forward_stream(body, **_kwargs):
        raise router.ProviderDisabledError("test-provider")
        yield b""  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(app_module, "forward", _forward)
    monkeypatch.setattr(app_module, "forward_stream", _forward_stream)
    resp = await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=_body(ONLY_A, stream=stream, cache=False),
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "provider_disabled"
    assert "Provider 'test-provider' is turned off" in resp.json()["detail"]


async def test_turned_off_candidate_hands_over_to_the_next(
    capped_a, _providers_file, monkeypatch
):
    """A waited for, turned off meanwhile: skipped, its slot given back at once,
    and the request is served by the other candidate."""
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    capped_b = {**PROVIDER_B, "max_concurrency": 1}
    _providers_file.write_text(json.dumps({"providers": [capped_a, capped_b]}))
    router.invalidate_models_cache()
    held_a = provider_admission.try_acquire(capped_a)
    held_b = provider_admission.try_acquire(capped_b)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        task = asyncio.create_task(router.forward(_body(BARE)))
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        _turn_off(_providers_file, capped_a, capped_b)
        held_a.release()
        await _until(lambda: provider_admission.in_flight("test-provider") == 0)
        held_b.release()
        assert (await asyncio.wait_for(task, 2)) == FAKE_COMPLETION
        assert (_calls(mock, URL_A), _calls(mock, URL_B)) == (0, 1)
    assert provider_admission._active == {}


async def test_every_waiter_is_refused_after_the_switch(
    capped_a, _providers_file, monkeypatch
):
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        tasks = [asyncio.create_task(router.forward(_body(ONLY_A))) for _ in range(2)]
        await _until(lambda: provider_admission.waiting("test-provider") == 2)
        _turn_off(_providers_file, capped_a)
        held.release()
        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), 2
        )
        assert [type(r) for r in results] == [router.ProviderDisabledError] * 2
        assert _calls(mock, URL_A) == 0
    assert provider_admission._active == {}


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_wait_ending_on_a_turned_off_provider_is_disabled_not_busy(
    capped_a, _providers_file, monkeypatch, stream
):
    """No slot came back before the wait ended: off, not full."""
    monkeypatch.setattr(provider_admission, "WAIT_S", 0.3)
    held = provider_admission.try_acquire(capped_a)
    try:
        with aioresponses() as mock:
            mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
            if stream:
                call = router.forward_stream(_body(ONLY_A, stream=True)).__anext__()
            else:
                call = router.forward(_body(ONLY_A))
            task = asyncio.ensure_future(call)
            await _until(lambda: provider_admission.waiting("test-provider") == 1)
            _turn_off(_providers_file, capped_a)
            with pytest.raises(router.ProviderDisabledError):
                await asyncio.wait_for(task, 2)
            assert _calls(mock, URL_A) == 0
    finally:
        held.release()
    assert provider_admission._active == {}


async def test_stream_slot_goes_back_when_the_registry_read_fails(
    capped_a, monkeypatch
):
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)

    def _unreadable(_provider_id):
        raise OSError("registry unreadable")

    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        gen = router.forward_stream(_body(ONLY_A, stream=True))
        first = asyncio.ensure_future(gen.__anext__())
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        monkeypatch.setattr(router, "_turned_off", _unreadable)
        held.release()
        with pytest.raises(OSError):
            await asyncio.wait_for(first, 2)
    assert provider_admission._active == {}, "the slot is never lost"
