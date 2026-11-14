"""A streamed chat served by a CLI sends its text as the CLI writes it (INC-2).

No CLI runs: ``cli_subprocess.complete`` is replaced by a script of events.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aioresponses import aioresponses

from plugboard import circuit_breaker, cli_subprocess, router
from plugboard.cli_subprocess import CliSubprocessError
from tests.factories import FAKE_STREAM_BODY, PROVIDER_B
from tests.test_router_admission import CLI, URL_B, _body

MODEL = "pbtest-cli:model-x"


def _result(text: str) -> dict:
    return {
        "id": "chatcmpl-final",
        "object": "chat.completion",
        "created": 1,
        "model": "claude-cli:model-x",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@pytest.fixture
def only_cli(_providers_file, monkeypatch):
    _providers_file.write_text(json.dumps({"providers": [CLI]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(router, "_DELAYS", [0, 0, 0])
    monkeypatch.setattr(circuit_breaker, "_circuits", {})


@pytest.fixture
def cli_then_http(_providers_file, monkeypatch):
    _providers_file.write_text(json.dumps({"providers": [CLI, PROVIDER_B]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "pbtest-cli": {"latency_avg": 0.1, "requests": 10, "errors": 0},
            "other-provider": {"latency_avg": 5.0, "requests": 10, "errors": 0},
        },
    )


def _events(chunks: list[bytes]) -> list[dict]:
    out = []
    for raw in b"".join(chunks).split(b"\n\n"):
        raw = raw.strip()
        if raw.startswith(b"data:") and b"[DONE]" not in raw:
            out.append(json.loads(raw[5:]))
    return out


async def test_first_word_leaves_before_the_cli_ends(only_cli, monkeypatch):
    cli_done = asyncio.Event()

    async def fake(kind, body, *, timeout_s=None, on_delta=None):
        on_delta("Bon")
        await asyncio.sleep(0.2)
        on_delta("jour")
        await asyncio.sleep(0.2)
        cli_done.set()
        return _result("Bonjour")

    monkeypatch.setattr(cli_subprocess, "complete", fake)
    stream = router.forward_stream(_body(MODEL, stream=True))
    first = await stream.__anext__()
    assert not cli_done.is_set()  # sent while the CLI still runs
    assert json.loads(first[5:])["choices"][0]["delta"] == {"content": "Bon"}
    rest = [c async for c in stream]
    events = _events([first, *rest])
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
    assert text == "Bonjour"
    assert rest[-1] == b"data: [DONE]\n\n"
    last = events[-1]
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["usage"]["total_tokens"] > 0  # billed on the whole answer
    assert len({e["id"] for e in events}) == 1  # one completion id


async def test_text_the_deltas_missed_is_sent_at_the_end(only_cli, monkeypatch):
    async def fake(kind, body, *, timeout_s=None, on_delta=None):
        on_delta("Bon")
        return _result("Bonjour")

    monkeypatch.setattr(cli_subprocess, "complete", fake)
    chunks = [c async for c in router.forward_stream(_body(MODEL, stream=True))]
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in _events(chunks))
    assert text == "Bonjour"


async def test_failure_before_the_first_word_goes_to_the_next_provider(
    cli_then_http, monkeypatch
):
    async def fake(kind, body, *, timeout_s=None, on_delta=None):
        raise CliSubprocessError("exited 1", provider_name="claude", exit_code=1)

    monkeypatch.setattr(cli_subprocess, "complete", fake)
    with aioresponses() as mock:
        mock.post(URL_B, body=FAKE_STREAM_BODY, status=200)
        chunks = [
            c async for c in router.forward_stream(_body("fake:model-v1", stream=True))
        ]
    assert chunks and b"[DONE]" in chunks[-1]


async def test_failure_after_the_first_word_is_raised_not_replayed(
    cli_then_http, monkeypatch
):
    async def fake(kind, body, *, timeout_s=None, on_delta=None):
        on_delta("Bon")
        await asyncio.sleep(0.05)
        raise CliSubprocessError("exited 1: boom", provider_name="claude", exit_code=1)

    monkeypatch.setattr(cli_subprocess, "complete", fake)
    stream = router.forward_stream(_body("fake:model-v1", stream=True))
    got = [await stream.__anext__()]
    with pytest.raises(CliSubprocessError):
        async for chunk in stream:
            got.append(chunk)
    assert b"Bon" in got[0]


async def test_a_client_leaving_stops_the_cli(only_cli, monkeypatch):
    cancelled = asyncio.Event()

    async def fake(kind, body, *, timeout_s=None, on_delta=None):
        on_delta("Bon")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(cli_subprocess, "complete", fake)
    stream = router.forward_stream(_body(MODEL, stream=True))
    await stream.__anext__()
    await stream.aclose()
    assert cancelled.is_set()


async def test_a_cli_that_cannot_stream_is_called_without_a_callback(
    only_cli, monkeypatch
):
    seen = {}

    async def fake(kind, body, *, timeout_s=None, on_delta=None):
        seen["on_delta"] = on_delta
        return _result("Bonjour")

    monkeypatch.setattr(cli_subprocess, "complete", fake)
    monkeypatch.setattr(cli_subprocess, "can_stream", lambda kind: False)
    chunks = [c async for c in router.forward_stream(_body(MODEL, stream=True))]
    assert seen["on_delta"] is None
    text = "".join(e["choices"][0]["delta"].get("content", "") for e in _events(chunks))
    assert text == "Bonjour"
