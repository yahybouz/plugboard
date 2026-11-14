"""Streamed chats: the next provider before the first chunk, a readable end
after it (PLU-26).

A streamed request went to one route only: when that provider failed before
sending anything (an error status, a timeout, a dropped connection), the
client got the error although another provider could serve the model. Once
the stream has begun, a cut ended it with no word of explanation: the client
saw a truncated stream. Providers are faked with aioresponses.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from plugboard import circuit_breaker, router
from tests.factories import (
    FAKE_STREAM_BODY,
    FAKE_STREAM_CHUNK_1,
    PROVIDER_A,
    PROVIDER_B,
)

AUTH = {"Authorization": "Bearer testkey"}
URL_A = "http://fake-llm.test/v1/chat/completions"
URL_B = "http://other-llm.test/v1/chat/completions"
BARE = "fake:model-v1"  # both providers are candidates, A first


@pytest.fixture
def two_routes(_providers_file, monkeypatch):
    """Both providers serve the bare model; A comes first."""
    capped = {**PROVIDER_A, "max_concurrency": 1}
    _providers_file.write_text(json.dumps({"providers": [capped, PROVIDER_B]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 10, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 10, "errors": 0},
        },
    )
    monkeypatch.setattr(circuit_breaker, "_circuits", {})


def _calls(mock, url):
    return len(mock.requests.get(("POST", URL(url)), []))


def _stream(model=BARE):
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "cache": False,
    }


@pytest.mark.parametrize(
    "failure",
    [
        {"status": 500, "body": "upstream down"},
        {"status": 429, "body": "rate limited"},
        {"exception": asyncio.TimeoutError()},
        {"exception": aiohttp.ClientConnectionError("refused")},
    ],
    ids=["500", "429", "timeout", "network"],
)
async def test_a_stream_failing_before_its_first_chunk_goes_to_the_next_provider(
    client, two_routes, failure
):
    with aioresponses() as mock:
        mock.post(URL_A, **failure)
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"content":"Hel"' in resp.text and "data: [DONE]" in resp.text
    assert calls == (1, 1)


async def test_a_stream_every_provider_fails_answers_the_last_error(client, two_routes):
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post(URL_B, status=502, body="down too")
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 502, resp.text
    assert calls == (1, 1)


async def test_a_stream_puts_an_open_circuit_last(client, two_routes):
    """BUG-51 with PLU-26: A's circuit is open, B is tried first."""
    for _ in range(3):
        circuit_breaker.record_failure(f"{PROVIDER_A['id']}:{BARE}")
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 200
    assert calls == (0, 1)


async def test_a_stream_with_a_provider_prefix_does_not_change_provider(
    client, two_routes
):
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=_stream(f"{PROVIDER_A['id']}:{BARE}"),
        )
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 502
    assert calls == (1, 0)


async def test_a_stream_cut_after_its_first_chunk_ends_with_a_readable_error(
    client, two_routes, monkeypatch
):
    """The first chunk went to the client: no other provider can take over.
    The stream ends with an error event and [DONE], not a bare cut."""
    import plugboard.app as app_mod

    async def cut_stream(body, **options):
        yield FAKE_STREAM_CHUNK_1
        raise router.ProviderNetworkError(
            "[test-provider] network error: reset by peer at 10.0.0.7:443",
            provider_id="test-provider",
        )

    monkeypatch.setattr(app_mod, "forward_stream", cut_stream)
    resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
    assert resp.status_code == 200
    events = [
        line.removeprefix("data: ")
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert json.loads(events[0])["choices"][0]["delta"]["content"] == "Hel"
    error = json.loads(events[-2])["error"]
    assert error["code"] == "stream_interrupted"
    assert error["message"]
    assert "10.0.0.7" not in resp.text  # never the provider's own text
    assert events[-1] == "[DONE]"


async def test_a_stream_passes_a_full_provider_to_the_next_at_once(client, two_routes):
    """D-23 for streams too, now that they have several routes: a full
    provider is skipped, not waited for, when the next one is free."""
    from plugboard import provider_admission

    capped = {**PROVIDER_A, "max_concurrency": 1}
    held = provider_admission.try_acquire(capped)
    try:
        with aioresponses() as mock:
            mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
            mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
            resp = await asyncio.wait_for(
                client.post("/v1/chat/completions", headers=AUTH, json=_stream()),
                2.0,
            )
            calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    finally:
        held.release()
    assert resp.status_code == 200
    assert calls == (0, 1)


# ── review of PLU-26 ────────────────────────────────────────────────────────


def test_complete_events_keep_a_cut_event_and_done_back():
    from plugboard.app import _complete_events

    buf = bytearray(b'data: {"a":1}\n\ndata: {"b"')
    assert _complete_events(buf) == b'data: {"a":1}\n\n'
    assert bytes(buf) == b'data: {"b"'
    buf = bytearray(b'data: {"a":1}\n\ndata: [DONE]\n\n')
    assert _complete_events(buf) == b'data: {"a":1}\n\n'
    assert bytes(buf) == b"data: [DONE]\n\n"
    buf = bytearray(b'data: {"a":1}\r\n\r\ndata: x')
    assert _complete_events(buf) == b'data: {"a":1}\r\n\r\n'
    buf = bytearray(b'data: {"a"')
    assert _complete_events(buf) == b"" and bytes(buf) == b'data: {"a"'


def _events(text):
    return [
        line.removeprefix("data: ")
        for line in text.splitlines()
        if line.startswith("data: ")
    ]


async def test_a_cut_inside_an_event_still_ends_with_a_parsable_error(
    client, two_routes, monkeypatch
):
    import plugboard.app as app_mod

    async def cut_mid_event(body, **options):
        yield FAKE_STREAM_CHUNK_1
        yield b'data: {"id":"chatcmpl-s1","object":"chat.compl'
        raise router.ProviderTimeoutError(
            "[test-provider] timeout (>900s)", provider_id="test-provider"
        )

    monkeypatch.setattr(app_mod, "forward_stream", cut_mid_event)
    resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
    events = _events(resp.text)
    assert [json.loads(e) for e in events[:-1]]  # every event parses
    assert json.loads(events[-2])["error"]["code"] == "stream_interrupted"
    assert "chat.compl" not in resp.text.replace("chat.completion.chunk", "")


async def test_a_late_error_after_done_is_a_complete_stream(
    client, two_routes, monkeypatch
):
    import plugboard.app as app_mod

    async def done_then_error(body, **options):
        yield FAKE_STREAM_BODY
        raise router.ProviderNetworkError("late reset", provider_id="test-provider")

    monkeypatch.setattr(app_mod, "forward_stream", done_then_error)
    resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
    assert resp.status_code == 200
    assert "stream_interrupted" not in resp.text
    assert _events(resp.text)[-1] == "[DONE]"


async def test_a_cut_stream_records_no_session_turn(
    client, two_routes, tmp_path, monkeypatch
):
    import plugboard.app as app_mod
    from plugboard import session_store

    monkeypatch.setattr(session_store, "_DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_INITIALISED", False)

    async def cut_stream(body, **options):
        yield FAKE_STREAM_CHUNK_1
        raise router.ProviderNetworkError("reset", provider_id="test-provider")

    monkeypatch.setattr(app_mod, "forward_stream", cut_stream)
    resp = await client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-LLM-Session-ID": "cut"},
        json=_stream(),
    )
    assert "stream_interrupted" in resp.text
    assert session_store.load_session("admin", "cut") == []


async def test_a_handed_over_stream_is_billed_to_the_provider_that_streamed(
    client, two_routes, monkeypatch
):
    import plugboard.app as app_mod
    from plugboard import provider_admission

    billed = []

    def bill(client_id, body, raw, key_id=None, hold=None):
        billed.append(router.served_by()[0]["id"])
        return None

    monkeypatch.setattr(app_mod, "_bill_streaming", bill)
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
    assert resp.status_code == 200
    assert billed == ["other-provider"]
    assert provider_admission._active == {}


async def test_a_failing_provider_turned_off_route_keeps_the_real_error(
    client, two_routes, _providers_file
):
    """A fails (500), B is turned off: the answer is A's error, not
    provider_disabled ("nothing was called" would be false)."""
    off_b = {**PROVIDER_B, "is_active": False}
    capped = {**PROVIDER_A, "max_concurrency": 1}
    with aioresponses() as mock:
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)

        def turn_b_off(url, **kwargs):
            _providers_file.write_text(json.dumps({"providers": [capped, off_b]}))
            router.invalidate_models_cache()

        mock.post(URL_A, status=500, body="down", callback=turn_b_off)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
    assert resp.status_code == 502, resp.text
    assert "provider_disabled" not in resp.text


async def test_an_empty_stream_hands_over(client, two_routes):
    with aioresponses() as mock:
        mock.post(URL_A, body=b"", status=200)
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 200 and '"content":"Hel"' in resp.text
    assert calls == (1, 1)


async def test_a_provider_without_credentials_hands_over(
    client, _providers_file, monkeypatch
):
    no_token = {**PROVIDER_A, "auth_source": "cli:pbtest-unknown-loader"}
    _providers_file.write_text(json.dumps({"providers": [no_token, PROVIDER_B]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 10, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 10, "errors": 0},
        },
    )
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 200, resp.text
    assert calls == (0, 1)


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
def http_cli_http(_providers_file, monkeypatch):
    """A, then a CLI route, then B, for the bare model; the CLI fails."""
    from plugboard import cli_subprocess
    from plugboard.cli_subprocess import CliSubprocessError

    _providers_file.write_text(json.dumps({"providers": [PROVIDER_A, CLI, PROVIDER_B]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "test-provider": {"latency_avg": 0.1, "requests": 10, "errors": 0},
            "pbtest-cli": {"latency_avg": 1.0, "requests": 10, "errors": 0},
            "other-provider": {"latency_avg": 2.0, "requests": 10, "errors": 0},
        },
    )
    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    failure = {"too_long": False}

    async def failing_cli(kind, body, *, timeout_s=None, on_delta=None):
        raise CliSubprocessError(
            "CLI 'claude' exited 1: SECRET_STDERR token=abc",
            provider_name="claude",
            model_id=body["model"],
            exit_code=1,
            stderr="SECRET_STDERR token=abc",
            request_too_long=failure["too_long"],
        )

    monkeypatch.setattr(cli_subprocess, "complete", failing_cli)
    return failure


async def test_a_failing_cli_route_hands_over_without_showing_its_stderr(
    client, http_cli_http, tmp_path, monkeypatch
):
    import plugboard.apikeys as ak

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    key = ak.create_key("stream-client", "stream")["key"]
    import plugboard.app as app_mod

    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=_stream(),
        )
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 200, resp.text
    assert calls == (1, 1)
    assert "SECRET_STDERR" not in resp.text


async def test_a_cli_route_finding_the_prompt_too_long_answers_400(
    client, http_cli_http
):
    http_cli_http["too_long"] = True
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        calls = (_calls(mock, URL_A), _calls(mock, URL_B))
    assert resp.status_code == 400, resp.text
    assert calls == (1, 0)


def test_done_quoted_in_content_is_not_the_end():
    from plugboard.app import _complete_events, _done_event_at

    quoted = b'data: {"choices":[{"delta":{"content":"say data: [DONE] now"}}]}\n\n'
    buf = bytearray(quoted + b'data: {"x"')
    assert _done_event_at(buf) == -1
    assert _complete_events(buf) == quoted
    buf = bytearray(quoted + b"data: [DONE]\n\n")
    assert _done_event_at(buf) == len(quoted)


def test_a_stream_without_event_ends_is_not_held_whole():
    from plugboard.app import _MAX_HELD, _complete_events

    buf = bytearray(b"x" * (_MAX_HELD + 100))
    out = _complete_events(buf)
    assert len(out) == _MAX_HELD + 36 and len(buf) == 64


async def test_a_cut_after_a_quoted_done_is_still_a_cut(
    client, two_routes, monkeypatch
):
    import plugboard.app as app_mod

    quoted = b'data: {"choices":[{"index":0,"delta":{"content":"data: [DONE]"}}]}\n\n'

    async def cut(body, **options):
        yield quoted
        raise router.ProviderNetworkError("reset", provider_id="test-provider")

    monkeypatch.setattr(app_mod, "forward_stream", cut)
    resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
    assert '"code": "stream_interrupted"' in resp.text
    assert resp.text.rstrip().endswith("data: [DONE]")


async def test_stream_successes_reset_the_provider_failures(client, two_routes):
    from plugboard import alerts

    alerts._provider_failures.clear()
    router.metrics._stats.pop("test-provider", None)
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        assert alerts._provider_failures.get("test-provider") == 1
        for _ in range(3):
            mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
            ok = await client.post(
                "/v1/chat/completions",
                headers=AUTH,
                json=_stream(f"{PROVIDER_A['id']}:{BARE}"),
            )
            assert ok.status_code == 200
    assert alerts._provider_failures.get("test-provider") == 0


async def test_a_failing_last_cli_route_shows_no_stderr_to_a_client_key(
    client, http_cli_http, _providers_file, tmp_path, monkeypatch
):
    import plugboard.apikeys as ak
    import plugboard.app as app_mod

    # A, then the CLI route, last: its error is the answer.
    _providers_file.write_text(json.dumps({"providers": [PROVIDER_A, CLI]}))
    router.invalidate_models_cache()

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    key = ak.create_key("stream-client", "stream")["key"]
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=_stream(),
        )
    assert resp.status_code == 502, resp.text
    assert "error_ref" in resp.text
    assert "SECRET_STDERR" not in resp.text and "pid" not in resp.text


async def test_an_empty_stream_counts_as_a_failure_not_a_success(client, two_routes):
    """An answer of 200 with no event at all hands over (above) and counts
    against the route, not for it: no circuit success, no reset of the
    provider_down count."""
    from plugboard import alerts

    alerts._provider_failures["test-provider"] = 1
    with aioresponses() as mock:
        mock.post(URL_A, body=b"", status=200)
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        resp = await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
    assert resp.status_code == 200
    circuit = circuit_breaker._circuits.get(f"{PROVIDER_A['id']}:{BARE}")
    assert circuit is not None and circuit.failures == 1
    assert alerts._provider_failures.get("test-provider") == 2


async def test_stream_successes_count_in_the_routing_score(
    client, two_routes, monkeypatch
):
    """One failed stream then three good ones: 4 requests, 1 error, not a
    provider at 100 % errors."""
    monkeypatch.setattr(router.metrics, "_stats", {})
    with aioresponses() as mock:
        mock.post(URL_A, status=500, body="down")
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        await client.post("/v1/chat/completions", headers=AUTH, json=_stream())
        for _ in range(3):
            mock.post(URL_A, body=FAKE_STREAM_BODY, status=200)
            await client.post(
                "/v1/chat/completions",
                headers=AUTH,
                json=_stream(f"{PROVIDER_A['id']}:{BARE}"),
            )
    stats = router.metrics._stats["test-provider"]
    assert (stats["requests"], stats["errors"]) == (4, 1)


async def test_an_empty_stream_on_its_only_route_is_an_error_not_a_success(
    client, two_routes
):
    """BUG-82: a 200 with no event at all is no success for the circuit, and
    with no other route the client gets the error, not an empty 200."""
    with aioresponses() as mock:
        mock.post(URL_A, body=b"", status=200)
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=_stream(f"{PROVIDER_A['id']}:{BARE}"),
        )
    assert resp.status_code == 502, resp.text
    circuit = circuit_breaker._circuits.get(f"{PROVIDER_A['id']}:{BARE}")
    assert circuit is not None and circuit.failures == 1
