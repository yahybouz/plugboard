"""PLU-16: per provider and model, /metrics keeps the time to the first token
of a stream, its tokens per second, the admission wait and the
``providers_busy`` refusals; /metrics/prometheus exposes them as histograms
and a counter. Labels carry a provider id and a model name, nothing else."""

import asyncio
import json
import math
import re

import pytest
from aioresponses import aioresponses

from plugboard import circuit_breaker, metrics, provider_admission, router
from tests.factories import FAKE_COMPLETION, FAKE_STREAM_BODY, PROVIDER_A, PROVIDER_B

AUTH = {"Authorization": "Bearer testkey"}
URL_A = "http://fake-llm.test/v1/chat/completions"
ONLY_A = "test-provider:fake:model-v1"
BASE_KEYS = {"requests", "errors", "latency_sum", "latency_avg"}


@pytest.fixture(autouse=True)
def fresh_metrics(monkeypatch):
    """Every test starts with empty metrics and the real clock."""
    monkeypatch.setattr(metrics, "_stats", {})
    monkeypatch.setattr(metrics, "_models", {})


class FakeClock:
    """``metrics._clock``: returns the queued times, then repeats the last."""

    def __init__(self, *times):
        self.times = list(times)
        self.last = 0.0

    def __call__(self):
        if self.times:
            self.last = self.times.pop(0)
        return self.last


def _sse(delta=None, usage=None):
    evt = {"object": "chat.completion.chunk", "choices": []}
    if delta is not None:
        evt["choices"] = [{"index": 0, "delta": delta}]
    if usage is not None:
        evt["usage"] = usage
    return f"data: {json.dumps(evt)}\n\n".encode()


def _model(provider="p", model="m"):
    return metrics.get_stats(per_model=True)[provider]["models"][model]


# ── The store ────────────────────────────────────────────────────────────────


def test_wait_and_busy_refusals_are_kept_per_provider_and_model():
    metrics.record_admission_wait("p", "m", 0.0)
    metrics.record_admission_wait("p", "m", 2.0)
    metrics.record_busy_refusal("p", "m")
    metrics.record_busy_refusal("p", "other")

    m = _model()
    assert m["admission_wait_count"] == 2
    assert m["admission_wait_sum"] == 2.0
    assert m["admission_wait_avg"] == 1.0
    assert (m["admission_wait_min"], m["admission_wait_max"]) == (0.0, 2.0)
    assert m["busy_refusals"] == 1
    assert m["ttft_count"] == 0 and m["ttft_avg"] == 0.0
    assert m["ttft_min"] is None and m["ttft_max"] is None
    assert _model(model="other")["busy_refusals"] == 1
    # A provider seen only through its models still has the base counters.
    assert BASE_KEYS <= set(metrics.get_stats(per_model=True)["p"])


def test_default_stats_keep_their_shape():
    """The router's load-balancing read (and the old /metrics shape) is unchanged."""
    metrics.record("p", 0.5)
    metrics.record_admission_wait("p", "m", 1.0)

    assert metrics.get_stats() == {
        "p": {"requests": 1, "errors": 0, "latency_sum": 0.5, "latency_avg": 0.5}
    }
    full = metrics.get_stats(per_model=True)["p"]
    assert {k: full[k] for k in BASE_KEYS} == metrics.get_stats()["p"]
    assert set(full["models"]) == {"m"}


def test_provider_without_model_metrics_has_no_models_key():
    metrics.record("p", 0.5)
    assert "models" not in metrics.get_stats(per_model=True)["p"]


@pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf, "3", None, True])
def test_invalid_observations_are_ignored(bad):
    metrics.record_ttft("p", "m", bad)
    metrics.record_tokens_per_second("p", "m", bad)
    metrics.record_admission_wait("p", "m", bad)
    stats = metrics.get_stats(per_model=True)
    m = stats.get("p", {}).get("models", {}).get("m")
    assert m is None or (
        m["ttft_count"] == m["tokens_per_second_count"] == 0
        and m["admission_wait_count"] == 0
    )


def test_model_series_are_capped(monkeypatch):
    monkeypatch.setattr(metrics, "MAX_SERIES", 2)
    for name in ("a", "b", "c", "d"):
        metrics.record_busy_refusal("p", name)
    models = metrics.get_stats(per_model=True)["p"]["models"]
    assert set(models) == {"a", "b", metrics.OTHER_MODEL}
    assert models[metrics.OTHER_MODEL]["busy_refusals"] == 2


def test_long_model_names_are_cut():
    metrics.record_busy_refusal("p", "x" * 1000)
    (name,) = metrics.get_stats(per_model=True)["p"]["models"]
    assert len(name) == metrics.MAX_LABEL


# ── The stream meter ─────────────────────────────────────────────────────────


def test_stream_meter_times_the_first_token_and_the_decode_rate(monkeypatch):
    # start 10.0; role-only chunk at 10.5; first word at 12.0; more at 13.0
    # and 14.0; usage chunk at 14.1.
    monkeypatch.setattr(
        metrics, "_clock", FakeClock(10.0, 10.5, 12.0, 13.0, 14.0, 14.1)
    )
    meter = metrics.StreamMeter("p", "m")
    meter.feed(_sse({"role": "assistant", "content": ""}))
    assert _model_or_none() is None  # no word yet
    meter.feed(_sse({"content": "Hel"}))
    meter.feed(_sse({"content": "lo"}))
    meter.feed(_sse({"content": " world"}))
    meter.feed(
        _sse(usage={"prompt_tokens": 3, "completion_tokens": 9}) + b"data: [DONE]\n\n"
    )
    meter.finish()

    m = _model()
    assert m["ttft_count"] == 1 and m["ttft_sum"] == 2.0
    assert m["tokens_per_second_count"] == 1
    # 8 tokens after the first one, from 12.0 to the last word at 14.0.
    assert m["tokens_per_second_avg"] == 4.0


def _model_or_none():
    return metrics.get_stats(per_model=True).get("p", {}).get("models", {}).get("m")


def test_stream_meter_estimates_tokens_without_usage(monkeypatch):
    monkeypatch.setattr(metrics, "_clock", FakeClock(0.0, 1.0, 2.0, 3.0))
    monkeypatch.setattr(metrics, "count_tokens", lambda text: len(text.split()))
    meter = metrics.StreamMeter("p", "m")
    meter.feed(_sse({"reasoning": "think hard "}))  # a thinking model's first token
    meter.feed(_sse({"content": "one two "}))
    meter.feed(_sse({"content": "three"}))
    meter.finish()

    m = _model()
    assert m["ttft_sum"] == 1.0
    # 5 words estimated, 4 after the first, over 2 seconds.
    assert m["tokens_per_second_avg"] == 2.0


def test_stream_meter_reads_lines_split_across_chunks(monkeypatch):
    monkeypatch.setattr(metrics, "_clock", FakeClock(0.0, 1.0, 2.0, 3.0))
    monkeypatch.setattr(metrics, "count_tokens", len)
    raw = _sse({"content": "a"}) + _sse({"content": "b"})
    meter = metrics.StreamMeter("p", "m")
    meter.feed(raw[:20])  # half a line: no word yet
    meter.feed(raw[20:-5])  # first line complete at 2.0
    meter.feed(raw[-5:])  # second line complete at 3.0
    meter.finish()
    m = _model()
    assert m["ttft_sum"] == 2.0
    assert m["tokens_per_second_count"] == 1


def test_stream_meter_counts_tool_calls_as_generated(monkeypatch):
    monkeypatch.setattr(metrics, "_clock", FakeClock(0.0, 0.5))
    meter = metrics.StreamMeter("p", "m")
    call = {"index": 0, "function": {"name": "f", "arguments": "{}"}}
    meter.feed(_sse({"tool_calls": [call]}))
    assert _model()["ttft_sum"] == 0.5


def test_one_read_or_one_token_gives_no_rate(monkeypatch):
    monkeypatch.setattr(metrics, "_clock", FakeClock(0.0, 1.0, 2.0))
    meter = metrics.StreamMeter("p", "m")
    meter.feed(FAKE_STREAM_BODY)  # every word in the same read
    meter.finish()
    assert _model()["ttft_count"] == 1
    assert _model()["tokens_per_second_count"] == 0

    monkeypatch.setattr(metrics, "_models", {})
    meter = metrics.StreamMeter("p", "m")
    meter.feed(_sse({"content": "a"}))
    meter.feed(_sse(usage={"completion_tokens": 1}))
    meter.finish()
    assert _model()["tokens_per_second_count"] == 0


@pytest.mark.parametrize(
    "garbage",
    [
        b"data: {not json\n\n",
        b"\xff\xfe\n",
        b"data: [1, 2]\n\n",
        b'data: {"choices": "x", "usage": [1]}\n\n',
        b'data: {"choices": [{"delta": ["x"]}]}\n\n',
        b'data: {"choices": [{"delta": {"content": 5}}]}\n\n',
        b": keep-alive\n\nevent: ping\n\n",
    ],
)
def test_stream_meter_never_raises_and_records_nothing_on_garbage(garbage):
    meter = metrics.StreamMeter("p", "m")
    meter.feed(garbage)
    meter.finish()
    assert _model_or_none() is None


def test_stream_meter_records_nothing_when_the_parser_fails(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(metrics.json, "loads", boom)
    meter = metrics.StreamMeter("p", "m")
    meter.feed(_sse({"content": "a"}))  # must not raise into the stream
    meter.finish()
    assert _model_or_none() is None


def test_stream_meter_drops_an_endless_line(monkeypatch):
    monkeypatch.setattr(metrics, "MAX_LINE_BYTES", 64)
    meter = metrics.StreamMeter("p", "m")
    meter.feed(b"data: " + b"x" * 100)  # no newline, over the cap
    assert len(meter._buf) == 0
    meter.feed(_sse({"content": "a"}))
    assert _model()["ttft_count"] == 1


# ── Prometheus ───────────────────────────────────────────────────────────────


def test_prometheus_exposes_histograms_and_the_busy_counter():
    metrics.record("p", 0.5)
    for v in (0.3, 4.0):
        metrics.record_ttft("p", "m", v)
    metrics.record_tokens_per_second("p", "m", 12.5)
    metrics.record_admission_wait("p", "m", 0.0)
    metrics.record_busy_refusal("p", "m")

    text = metrics.format_prometheus()
    # The existing families are untouched.
    assert 'plugboard_requests_total{provider="p"} 1' in text
    assert "plugboard_latency_avg_seconds" in text and "plugboard_up 1" in text

    lab = 'provider="p",model="m"'
    assert "# TYPE plugboard_ttft_seconds histogram" in text
    assert f'plugboard_ttft_seconds_bucket{{{lab},le="0.25"}} 0' in text
    assert f'plugboard_ttft_seconds_bucket{{{lab},le="0.5"}} 1' in text
    assert f'plugboard_ttft_seconds_bucket{{{lab},le="5.0"}} 2' in text
    assert f'plugboard_ttft_seconds_bucket{{{lab},le="+Inf"}} 2' in text
    assert f"plugboard_ttft_seconds_sum{{{lab}}} 4.3" in text
    assert f"plugboard_ttft_seconds_count{{{lab}}} 2" in text
    assert "# TYPE plugboard_tokens_per_second histogram" in text
    assert f'plugboard_tokens_per_second_bucket{{{lab},le="20.0"}} 1' in text
    assert "# TYPE plugboard_admission_wait_seconds histogram" in text
    assert f"plugboard_admission_wait_seconds_count{{{lab}}} 1" in text
    assert "# TYPE plugboard_providers_busy_total counter" in text
    assert f"plugboard_providers_busy_total{{{lab}}} 1" in text


def test_prometheus_buckets_are_cumulative_and_end_with_the_count():
    for v in (0.1, 0.6, 1.5, 100.0):
        metrics.record_ttft("p", "m", v)
    counts = [
        int(line.rsplit(" ", 1)[1])
        for line in metrics.format_prometheus().splitlines()
        if line.startswith("plugboard_ttft_seconds_bucket")
    ]
    assert counts == sorted(counts) and counts[-1] == 4


def test_prometheus_escapes_label_values():
    metrics.record_busy_refusal('pro"v', 'a"b\\c\nd')
    text = metrics.format_prometheus()
    assert (
        'plugboard_providers_busy_total{provider="pro\\"v",model="a\\"b\\\\c\\nd"} 1'
        in text
    )


def test_prometheus_labels_are_only_provider_model_and_le():
    metrics.record_ttft("p", "m", 1.0)
    metrics.record_tokens_per_second("p", "m", 1.0)
    metrics.record_admission_wait("p", "m", 1.0)
    metrics.record_busy_refusal("p", "m")
    for line in metrics.format_prometheus().splitlines():
        labels = re.search(r"\{(.*)\}", line)
        if line.startswith("#") or not labels:
            continue
        names = set(re.findall(r'(\w+)="', labels.group(1)))
        assert names <= {"provider", "model", "le"}, line


# ── The router records them ──────────────────────────────────────────────────


@pytest.fixture
def capped_a(_providers_file, monkeypatch):
    """test-provider takes one request at a time; no retries, no wait."""
    a = {**PROVIDER_A, "max_concurrency": 1}
    _providers_file.write_text(json.dumps({"providers": [a, PROVIDER_B]}))
    router.invalidate_models_cache()
    monkeypatch.setattr(router, "_DELAYS", [])
    monkeypatch.setattr(provider_admission, "WAIT_S", 0.0)
    monkeypatch.setattr(circuit_breaker, "_circuits", {})
    return a


def _body(model, **extra):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}


def _only_model(provider="test-provider"):
    (m,) = metrics.get_stats(per_model=True)[provider]["models"].values()
    return m


async def _until(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never met")


async def test_a_free_slot_records_a_zero_wait(capped_a):
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        await router.forward(_body(ONLY_A))
    m = _only_model()
    assert m["admission_wait_count"] == 1 and m["admission_wait_max"] == 0.0
    assert m["ttft_count"] == 0  # a non-streamed answer has no first token


async def test_a_queued_request_records_its_wait(capped_a, monkeypatch):
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, payload=FAKE_COMPLETION)
        task = asyncio.create_task(router.forward(_body(ONLY_A)))
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        await asyncio.sleep(0.15)
        held.release()
        await asyncio.wait_for(task, 2)
    m = _only_model()
    assert m["admission_wait_count"] == 1
    assert 0.15 <= m["admission_wait_max"] < 2.0


async def test_a_queued_stream_records_its_wait(capped_a, monkeypatch):
    monkeypatch.setattr(provider_admission, "WAIT_S", 2.0)
    held = provider_admission.try_acquire(capped_a)
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY)
        gen = router.forward_stream(_body(ONLY_A, stream=True))
        first = asyncio.ensure_future(gen.__anext__())
        await _until(lambda: provider_admission.waiting("test-provider") == 1)
        await asyncio.sleep(0.15)
        held.release()
        await asyncio.wait_for(first, 2)
        await gen.aclose()
    assert 0.15 <= _only_model()["admission_wait_max"] < 2.0


async def test_busy_refusals_are_counted_for_plain_and_streamed_chats(capped_a):
    held = provider_admission.try_acquire(capped_a)
    with pytest.raises(router.ProvidersBusyError):
        await router.forward(_body(ONLY_A))
    with pytest.raises(router.ProvidersBusyError):
        await router.forward_stream(_body(ONLY_A, stream=True)).__anext__()
    held.release()
    m = _only_model()
    assert m["busy_refusals"] == 2
    assert m["admission_wait_count"] == 0  # refused, never admitted
    # Full is not an error: the provider's error counter stays untouched.
    assert metrics.get_stats().get("test-provider", {}).get("errors", 0) == 0


class _Content:
    def __init__(self, chunks, gate=None):
        self.chunks, self.gate = chunks, gate

    async def iter_any(self):
        for i, chunk in enumerate(self.chunks):
            if i and self.gate is not None:
                await self.gate.wait()
            yield chunk


class _Resp:
    status = 200
    headers = {}

    def __init__(self, content):
        self.content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, content):
        self.content = content

    def post(self, url, **kwargs):
        return _Resp(self.content)


def _fake_provider_stream(monkeypatch, chunks, gate=None):
    from plugboard import http_pool

    session = _Session(_Content(chunks, gate))

    async def session_for(provider):
        return session

    monkeypatch.setattr(http_pool, "session_for", session_for)


async def test_a_streamed_answer_records_ttft_and_tokens_per_second(
    capped_a, monkeypatch
):
    chunks = [
        _sse({"role": "assistant", "content": ""}),
        _sse({"content": "Hel"}),
        _sse({"content": "lo"}),
        _sse(usage={"prompt_tokens": 2, "completion_tokens": 5}),
        b"data: [DONE]\n\n",
    ]
    _fake_provider_stream(monkeypatch, chunks)
    # start 100.0; role 100.2; first word 101.0; last word 102.0; then 102.1...
    monkeypatch.setattr(
        metrics, "_clock", FakeClock(100.0, 100.2, 101.0, 102.0, 102.1, 102.2)
    )
    got = b"".join([c async for c in router.forward_stream(_body(ONLY_A, stream=True))])
    assert got == b"".join(chunks)  # the meter never changes the stream
    m = _only_model()
    assert m["ttft_count"] == 1 and m["ttft_sum"] == 1.0
    assert m["tokens_per_second_avg"] == 4.0  # 4 tokens after the first, in 1 s
    assert m["admission_wait_count"] == 1


async def test_a_stream_closed_early_keeps_its_ttft_but_no_rate(capped_a, monkeypatch):
    gate = asyncio.Event()  # the provider never sends the rest
    chunks = [_sse({"content": "Hel"}), _sse({"content": "lo"})]
    _fake_provider_stream(monkeypatch, chunks, gate)
    gen = router.forward_stream(_body(ONLY_A, stream=True))
    assert await gen.__anext__() == chunks[0]
    await gen.aclose()
    m = _only_model()
    assert m["ttft_count"] == 1 and m["tokens_per_second_count"] == 0
    assert provider_admission._active == {}


# ── The endpoints ────────────────────────────────────────────────────────────


async def test_metrics_lists_models_under_their_provider(client, capped_a):
    with aioresponses() as mock:
        mock.post(URL_A, body=FAKE_STREAM_BODY)
        resp = await client.post(
            "/v1/chat/completions",
            headers=AUTH,
            json=_body(ONLY_A, stream=True, cache=False),
        )
        assert resp.status_code == 200
    body = (await client.get("/metrics", headers=AUTH)).json()
    entry = body["test-provider"]
    assert BASE_KEYS <= set(entry)
    (m,) = entry["models"].values()
    assert m["ttft_count"] == 1 and m["admission_wait_count"] == 1

    prom = (await client.get("/metrics/prometheus", headers=AUTH)).text
    assert 'plugboard_ttft_seconds_count{provider="test-provider",model=' in prom


async def test_busy_chat_answer_is_counted(client, capped_a):
    held = provider_admission.try_acquire(capped_a)
    resp = await client.post(
        "/v1/chat/completions", headers=AUTH, json=_body(ONLY_A, cache=False)
    )
    held.release()
    assert resp.status_code == 503
    body = (await client.get("/metrics", headers=AUTH)).json()
    (m,) = body["test-provider"]["models"].values()
    assert m["busy_refusals"] == 1
