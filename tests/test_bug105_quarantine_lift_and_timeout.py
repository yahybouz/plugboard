"""BUG-105: a successful call lifts a quarantine; a timeout never sets one.

A quarantine set at the right provider was lifted only by a successful
probe, and the probes are off in production: the model stayed hidden from
``/v1/models`` and the catalogue pass removed it, even once it answered
again. Now a real call that succeeds on the provider+model pair lifts that
pair's entry (and only it). No paid probe is added.

The internet check quarantined a model for good on a mere timeout. A
timeout says nothing of the model: it now answers ``"status": "timeout"``
and records nothing.
"""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses

import plugboard.app as app_module
from plugboard import model_quarantine as mq
from plugboard import router
from plugboard.cli_subprocess import CliSubprocessError
from plugboard.router import ProviderTimeoutError
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY
from tests.test_circuit_breaker_provider_faults import cli  # noqa: F401 (fixture)
from tests.test_cli_stream import FINAL
from tests.test_route_pauses import two_providers  # noqa: F401 (fixture)
from tests.test_router_admission import BARE, ONLY_A, URL_A, _body

MODEL = "fake:model-v1"
# The real writer: the ``cli`` fixture replaces the module's own by a mock.
_quarantine = mq.record_terminal_unavailable


@pytest.fixture
def qfile(monkeypatch):
    """The quarantine store every writer uses by default, empty at the start
    and removed at the end."""
    path = mq._QUARANTINE_FILE
    path.unlink(missing_ok=True)
    yield path
    path.unlink(missing_ok=True)


def _quarantined(provider_id: str) -> set:
    return mq.get_quarantined_suffixes(mq._QUARANTINE_FILE, provider_id=provider_id)


# ── a real call that succeeds lifts its pair's quarantine ───────────────────


async def test_a_successful_call_lifts_the_quarantine(qfile, two_providers):  # noqa: F811
    mq.record_terminal_unavailable(
        MODEL, reason="was down", provider_id="test-provider"
    )
    mq.record_terminal_unavailable(
        MODEL, reason="refused", provider_id="other-provider"
    )
    assert MODEL in _quarantined("test-provider")

    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        await router.forward(_body(ONLY_A))

    assert MODEL not in _quarantined("test-provider")
    # The same name at another provider is that provider's business.
    assert MODEL in _quarantined("other-provider")


async def test_a_successful_stream_lifts_the_quarantine(qfile, two_providers):  # noqa: F811
    mq.record_terminal_unavailable(
        MODEL, reason="was down", provider_id="test-provider"
    )

    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, content_type="text/event-stream")
        async for _chunk in router.forward_stream(_body(ONLY_A, stream=True)):
            pass

    assert MODEL not in _quarantined("test-provider")


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_a_successful_cli_call_lifts_the_quarantine(qfile, cli, stream):  # noqa: F811
    _quarantine("pbtest-model", reason="was down", provider_id="pbtest-cli")
    assert "pbtest-model" in _quarantined("pbtest-cli")
    cli("claude", stdout=("\n".join(FINAL) + "\n").encode(), code=0)
    body = _body("pbtest-cli:pbtest-model", stream=stream)
    if stream:
        async for _chunk in router.forward_stream(body):
            pass
    else:
        await router.forward(body)

    assert "pbtest-model" not in _quarantined("pbtest-cli")


async def test_counted_failures_are_reset_by_a_success(qfile, two_providers):  # noqa: F811
    mq.record_failure(MODEL, reason="slow", provider_id="test-provider")
    mq.record_failure(MODEL, reason="slow", provider_id="test-provider")

    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        await router.forward(_body(ONLY_A))

    state = json.loads(qfile.read_text(encoding="utf-8"))
    assert MODEL not in (state.get("providers") or {}).get("test-provider", {})


async def test_the_model_is_listed_again_after_the_lift(qfile, two_providers):  # noqa: F811
    mq.record_terminal_unavailable(
        MODEL, reason="was down", provider_id="test-provider"
    )
    router.invalidate_models_cache()
    before = {m["id"]: m for m in await router.list_models()}
    assert before[ONLY_A].get("reason") == "quarantined"

    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        await router.forward(_body(ONLY_A))

    after = {m["id"]: m for m in await router.list_models()}
    assert after[ONLY_A].get("reason") != "quarantined"


async def test_a_success_without_quarantine_writes_nothing(qfile, two_providers):  # noqa: F811
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION, repeat=True)
        mock.post("http://other-llm.test/v1/chat/completions", payload=FAKE_COMPLETION)
        await router.forward(_body(BARE))
    assert not qfile.exists()


async def test_a_failed_call_lifts_nothing(qfile, two_providers):  # noqa: F811
    mq.record_terminal_unavailable(
        MODEL, reason="was down", provider_id="test-provider"
    )
    with aioresponses() as mock:
        mock.post(URL_A, status=500, payload={"error": "boom"}, repeat=True)
        with pytest.raises(Exception):
            await router.forward(_body(ONLY_A))
    assert MODEL in _quarantined("test-provider")


# ── the internet check: a timeout never quarantines ─────────────────────────


def _check_internet(monkeypatch, error: Exception):
    async def fake_forward(body, timeout=20.0):
        raise error

    monkeypatch.setattr(app_module, "forward", fake_forward)
    monkeypatch.setattr(
        app_module,
        "resolve_provider_and_model",
        lambda mid: ({"id": "slow-provider", "models": []}, "slow-model"),
    )
    return app_module.check_model_internet_access(
        {"model_id": "slow-provider:slow-model", "persist": True}, _=None
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            ProviderTimeoutError(
                "[slow-provider] timeout (>30.0s) — model=slow-model",
                provider_id="slow-provider",
            ),
            id="http-timeout",
        ),
        pytest.param(
            CliSubprocessError(
                "CLI 'claude' timed out after 30s",
                provider_name="claude",
                model_id="slow-model",
                timed_out=True,
            ),
            id="cli-timeout",
        ),
    ],
)
async def test_a_timeout_never_quarantines(qfile, monkeypatch, error):
    out = await _check_internet(monkeypatch, error)

    assert out["status"] == "timeout"
    assert out["internet_access"] is False
    assert "slow-model" not in _quarantined("slow-provider")
    state = mq.get_state(qfile)
    assert "slow-model" not in (state.get("providers") or {}).get("slow-provider", {})


async def test_an_unknown_model_is_still_quarantined(qfile, monkeypatch):
    """Only the timeout changes: a model the provider says does not exist is
    still quarantined at that provider."""
    out = await _check_internet(
        monkeypatch,
        RuntimeError("[slow-provider] HTTP 404: model_not_found"),
    )
    assert out["status"] == "quarantined"
    assert "slow-model" in _quarantined("slow-provider")
