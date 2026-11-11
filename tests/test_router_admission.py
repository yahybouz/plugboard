import asyncio
import json

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from plugboard import circuit_breaker, cli_subprocess, provider_admission, router
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY, PROVIDER_A, PROVIDER_B

URL_A = "http://fake-llm.test/v1/chat/completions"
URL_B = "http://other-llm.test/v1/chat/completions"
BARE = "fake:model-v1"  # both providers are candidates
ONLY_A = "test-provider:fake:model-v1"
CLI = {
    "id": "pbtest-cli",
    "name": "CLI test",
    "base_url": "subprocess://claude",
    "bearer_token": "(via CLI)",
    "default_model": "model-x",
    "auth_source": "subprocess:claude",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture
def capped_a(_providers_file, monkeypatch):
    """test-provider takes one request at a time and comes first, then
    other-provider. No CLI among the candidates of a model without prefix."""
    a = {**PROVIDER_A, "max_concurrency": 1}
    _providers_file.write_text(json.dumps({"providers": [a, PROVIDER_B]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 10, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 10, "errors": 0},
        },
    )
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(provider_admission, "WAIT_S", 0.0)
    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    unfaked_cli_runs = []

    async def no_real_cli(kind, body, *, timeout_s=None):
        unfaked_cli_runs.append(kind)
        raise RuntimeError("tests never run a real CLI")

    monkeypatch.setattr(cli_subprocess, "complete", no_real_cli)
    yield a
    assert unfaked_cli_runs == [], "a test reached a CLI it did not fake"


def _calls(mock, url):
    return len(mock.requests.get(("POST", URL(url)), []))


def _body(model, **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


async def _until(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never met")


async def test_full_provider_hands_the_request_to_the_next(capped_a):
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 0 and _calls(mock, URL_B) == 1
    assert router.served_by()[0]["id"] == "other-provider"
    assert (
        circuit_breaker._circuits[ONLY_A].failures == 0
    )  # full is not a failure (D-7)
    held.release()
    assert provider_admission._active == {}


async def test_every_candidate_full_raises_providers_busy(capped_a):
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        with pytest.raises(router.ProvidersBusyError) as err:
            await router.forward(_body(ONLY_A))
        assert _calls(mock, URL_A) == 0
    assert err.value.provider_ids == ["test-provider"]
    assert err.value.retry_after == 5
    held.release()


@pytest.mark.parametrize(
    "answer",
    [
        {"payload": FAKE_COMPLETION},
        {"status": 500, "body": "down"},
        {"exception": asyncio.TimeoutError()},
        {"exception": aiohttp.ClientConnectionError("refused")},
    ],
    ids=["ok", "http-500", "timeout", "network"],
)
async def test_slot_is_released_whatever_the_outcome(capped_a, answer):
    with aioresponses() as mock:
        mock.post(URL_A, repeat=True, **answer)
        try:
            await router.forward(_body(ONLY_A))
        except Exception:
            pass
    assert provider_admission._active == {}


async def test_cli_call_counts_in_flight_and_frees_its_slot(
    capped_a, _providers_file, monkeypatch
):
    _providers_file.write_text(json.dumps({"providers": [capped_a, PROVIDER_B, CLI]}))
    router.invalidate_models_cache()
    gate = asyncio.Event()

    async def fake_complete(kind, body, *, timeout_s=None):
        await gate.wait()
        return {**FAKE_COMPLETION}

    monkeypatch.setattr(cli_subprocess, "complete", fake_complete)
    task = asyncio.create_task(router.forward(_body("pbtest-cli:model-x")))
    await _until(lambda: provider_admission.in_flight("pbtest-cli") == 1)
    gate.set()
    await task
    assert provider_admission._active == {}


async def test_stream_holds_its_slot_until_the_last_chunk(capped_a):
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        gen = router.forward_stream(_body(ONLY_A, stream=True))
        assert await gen.__anext__()
        assert provider_admission.in_flight("test-provider") == 1
        async for _ in gen:
            pass
    assert provider_admission._active == {}


async def test_stream_closed_early_frees_its_slot(capped_a):
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        gen = router.forward_stream(_body(ONLY_A, stream=True))
        await gen.__anext__()
        await gen.aclose()
    assert provider_admission._active == {}


async def test_full_stream_route_raises_providers_busy(capped_a):
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        with pytest.raises(router.ProvidersBusyError):
            await router.forward_stream(_body(ONLY_A, stream=True)).__anext__()
        assert _calls(mock, URL_A) == 0
    held.release()


async def test_refused_route_is_checked_before_its_slot(capped_a):
    held = provider_admission.try_acquire(capped_a)  # the only slot is taken

    def guard(provider, model):
        raise PermissionError("not allowed")

    with pytest.raises(PermissionError):  # not ProvidersBusyError
        await router.forward(_body(ONLY_A), route_guard=guard)
    with pytest.raises(PermissionError):
        await router.forward_stream(
            _body(ONLY_A, stream=True), route_guard=guard
        ).__anext__()
    held.release()
    assert provider_admission._active == {}
