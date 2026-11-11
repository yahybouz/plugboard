import json

import pytest
from aioresponses import aioresponses

from plugboard import circuit_breaker, cli_subprocess, route_pauses, router
from plugboard.cli_subprocess import CliSubprocessError
from tests.factories import FAKE_COMPLETION, PROVIDER_A, PROVIDER_B
from tests.test_router_admission import BARE, CLI, ONLY_A, URL_A, URL_B, _body, _calls


@pytest.fixture
def two_providers(_providers_file, monkeypatch):
    """test-provider first, other-provider next, the CLI last; no caps, no
    retry delay. A test that reaches the CLI fakes it."""
    _providers_file.write_text(json.dumps({"providers": [PROVIDER_A, PROVIDER_B, CLI]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 10, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 10, "errors": 0},
            "pbtest-cli": {"latency_avg": 9.0, "requests": 10, "errors": 0},
        },
    )
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    unfaked_cli_runs = []

    async def no_real_cli(kind, body, *, timeout_s=None):
        unfaked_cli_runs.append(kind)
        raise RuntimeError("tests never run a real CLI")

    monkeypatch.setattr(cli_subprocess, "complete", no_real_cli)
    yield
    assert unfaked_cli_runs == [], "a test reached a CLI it did not fake"


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(route_pauses, "_now", lambda: now[0])
    return now


def test_pause_follows_retry_after(clock):
    assert route_pauses.pause("p:m", 120) == 120
    clock[0] += 100
    assert route_pauses.paused_for("p:m") == pytest.approx(20)
    clock[0] += 21
    assert route_pauses.paused_for("p:m") == 0.0


def test_pause_without_retry_after_grows_30_60_300(clock):
    assert [route_pauses.pause("p:m", None) for _ in range(4)] == [30, 60, 300, 300]


def test_success_resets_the_steps(clock):
    route_pauses.pause("p:m", None)
    route_pauses.pause("p:m", None)
    route_pauses.clear("p:m")
    assert route_pauses.paused_for("p:m") == 0.0
    assert route_pauses.pause("p:m", None) == 30


def test_retry_quota_here_rule(monkeypatch):
    monkeypatch.setattr(router, "_DELAYS", [2, 5, 15])
    assert router._retry_quota_here(None, 0) is True
    assert router._retry_quota_here(2, 0) is True
    assert router._retry_quota_here(3, 0) is False
    assert router._retry_quota_here(5, 1) is True
    assert router._retry_quota_here(1, 3) is False  # no delay left


def test_paused_or_open_routes_go_last(clock):
    a, b, c, d = ({"id": x} for x in "abcd")
    route_pauses.pause("a:m", 100)
    route_pauses.pause("b:m", 10)
    ordered = router._order_routes([(a, "m"), (b, "m"), (c, "m"), (d, "m")], {"c:m"})
    assert [p["id"] for p, _ in ordered] == ["d", "c", "b", "a"]


async def test_long_retry_after_is_not_retried_and_pauses_the_route(two_providers):
    with aioresponses() as mock:
        mock.post(
            URL_A,
            status=429,
            body="slow down",
            headers={"Retry-After": "120"},
            repeat=True,
        )
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 1  # no retry on the spot
        assert route_pauses.paused_for(ONLY_A) == pytest.approx(120, abs=1)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 1 and _calls(mock, URL_B) == 2  # A went last


async def test_short_retry_after_is_retried_on_the_spot(two_providers):
    with aioresponses() as mock:
        mock.post(URL_A, status=429, body="slow down", headers={"Retry-After": "0"})
        mock.post(URL_A, payload=FAKE_COMPLETION)
        await router.forward(_body(ONLY_A))
        assert _calls(mock, URL_A) == 2
    assert route_pauses.paused_for(ONLY_A) == 0.0


async def test_429_without_retry_after_is_retried_as_before_then_paused(
    two_providers,
):
    with aioresponses() as mock:
        mock.post(URL_A, status=429, body="slow down", repeat=True)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 4  # 1 + PLUGBOARD_RETRY_DELAYS
    assert route_pauses.paused_for(ONLY_A) == pytest.approx(30, abs=1)


async def test_route_with_open_circuit_goes_last(two_providers):
    for _ in range(3):
        circuit_breaker.record_failure(ONLY_A)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        mock.post(URL_B, payload=FAKE_COMPLETION, repeat=True)
        await router.forward(_body(BARE))
        assert _calls(mock, URL_A) == 0 and _calls(mock, URL_B) == 1


async def test_paused_single_route_is_still_called(two_providers):
    route_pauses.pause(ONLY_A, 300)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        await router.forward(_body(ONLY_A))
        assert _calls(mock, URL_A) == 1
    assert route_pauses.paused_for(ONLY_A) == 0.0


async def test_cli_quota_pauses_the_route(two_providers, monkeypatch):
    async def quota(kind, body, *, timeout_s=None):
        raise CliSubprocessError(
            "usage limit",
            quota=True,
            retry_after=600,
            provider_name="claude",
            model_id="model-x",
        )

    monkeypatch.setattr(cli_subprocess, "complete", quota)
    with pytest.raises(CliSubprocessError):
        await router.forward(_body("pbtest-cli:model-x"))
    assert route_pauses.paused_for("pbtest-cli:model-x") == pytest.approx(600, abs=1)


async def test_streamed_429_pauses_the_route(two_providers):
    with aioresponses() as mock:
        mock.post(URL_A, status=429, body="slow down", headers={"Retry-After": "90"})
        with pytest.raises(router.ProviderQuotaError):
            await router.forward_stream(_body(ONLY_A, stream=True)).__anext__()
    assert route_pauses.paused_for(ONLY_A) == pytest.approx(90, abs=1)
