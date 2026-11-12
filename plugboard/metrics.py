# plugboard/metrics.py
"""Request metrics, kept in this process's memory.

Per provider: requests, errors and the mean latency of the calls that
succeeded (``record``). Per provider and model (PLU-16, D-26):

- ``ttft``: seconds from sending a streamed chat to its provider to the
  first generated chunk (text, reasoning or a tool call). The admission
  wait is not in it; it has its own measure.
- ``tokens_per_second``: completion tokens after the first one, divided by
  the time from the first generated chunk to the last (the decode rate a
  reader sees). Streamed answers only, when the words came in at least two
  reads; the provider's ``usage`` is used, else a tiktoken estimate.
- ``admission_wait``: seconds a request waited for a slot on its provider
  before being sent (0 when a slot was free).
- ``busy_refusals``: requests refused with 503 ``providers_busy``, counted
  on each full candidate route.

Labels are a provider id and a model name, never a client, a key or any
request content. The number of (provider, model) series is capped: past
``MAX_SERIES`` a new model is counted under ``OTHER_MODEL``.
"""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from plugboard.tokenizer import count_tokens

log = logging.getLogger(__name__)

_stats: Dict[str, Any] = {}

# Read once per stream chunk; tests replace it.
_clock = time.perf_counter

TTFT_BUCKETS = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.5, 10.0, 15.0, 30.0, 60.0)
TPS_BUCKETS = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 50.0, 100.0, 200.0)
WAIT_BUCKETS = (0.01, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0)
MAX_SERIES = 256
MAX_LABEL = 128
OTHER_MODEL = "(other)"
# A stream line longer than this (no newline) is dropped, never buffered.
MAX_LINE_BYTES = 1 << 20

_BASE = {"requests": 0, "errors": 0, "latency_sum": 0.0, "latency_avg": 0.0}


def record(provider_id: str, latency: float, *, error: bool = False) -> None:
    s = _stats.setdefault(
        provider_id,
        {"requests": 0, "errors": 0, "latency_sum": 0.0, "latency_avg": 0.0},
    )
    s["requests"] += 1
    if error:
        s["errors"] += 1
    else:
        s["latency_sum"] += latency
        ok = s["requests"] - s["errors"]
        s["latency_avg"] = round(s["latency_sum"] / ok, 3) if ok else 0.0


class _Histogram:
    """Observations of one measure: bucket counts (not cumulative), sum,
    count, min and max."""

    __slots__ = ("bounds", "buckets", "count", "sum", "min", "max")

    def __init__(self, bounds: Tuple[float, ...]) -> None:
        self.bounds = bounds
        self.buckets = [0] * len(bounds)
        self.count = 0
        self.sum = 0.0
        self.min: Optional[float] = None
        self.max: Optional[float] = None

    def observe(self, value: float) -> None:
        self.count += 1
        self.sum += value
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)
        for i, bound in enumerate(self.bounds):
            if value <= bound:
                self.buckets[i] += 1
                break

    def as_dict(self, name: str) -> Dict[str, Any]:
        def r(v: Optional[float]) -> Optional[float]:
            return None if v is None else round(v, 3)

        return {
            f"{name}_count": self.count,
            f"{name}_sum": round(self.sum, 6),
            f"{name}_avg": round(self.sum / self.count, 3) if self.count else 0.0,
            f"{name}_min": r(self.min),
            f"{name}_max": r(self.max),
        }


class _ModelStats:
    __slots__ = ("ttft", "tokens_per_second", "admission_wait", "busy_refusals")

    def __init__(self) -> None:
        self.ttft = _Histogram(TTFT_BUCKETS)
        self.tokens_per_second = _Histogram(TPS_BUCKETS)
        self.admission_wait = _Histogram(WAIT_BUCKETS)
        self.busy_refusals = 0

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        out.update(self.ttft.as_dict("ttft"))
        out.update(self.tokens_per_second.as_dict("tokens_per_second"))
        out.update(self.admission_wait.as_dict("admission_wait"))
        out["busy_refusals"] = self.busy_refusals
        return out


# (provider id, model) -> its measures.
_models: Dict[Tuple[str, str], _ModelStats] = {}


def _series(provider_id: str, model: str) -> _ModelStats:
    key = (str(provider_id), str(model or "")[:MAX_LABEL])
    stats = _models.get(key)
    if stats is None:
        if len(_models) >= MAX_SERIES:
            key = (key[0], OTHER_MODEL)
            stats = _models.get(key)
        if stats is None:
            stats = _models[key] = _ModelStats()
    return stats


def _valid(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def record_ttft(provider_id: str, model: str, seconds: float) -> None:
    if _valid(seconds):
        _series(provider_id, model).ttft.observe(float(seconds))


def record_tokens_per_second(provider_id: str, model: str, rate: float) -> None:
    if _valid(rate):
        _series(provider_id, model).tokens_per_second.observe(float(rate))


def record_admission_wait(provider_id: str, model: str, seconds: float) -> None:
    if _valid(seconds):
        _series(provider_id, model).admission_wait.observe(float(seconds))


def record_busy_refusal(provider_id: str, model: str) -> None:
    _series(provider_id, model).busy_refusals += 1


class StreamMeter:
    """Watches an SSE chat stream as it goes through, without changing it:
    records the time to the first generated chunk, and the decode rate once
    the stream ends (``finish``). Never raises: a metric never breaks a
    stream."""

    __slots__ = (
        "_pid",
        "_model",
        "_start",
        "_first",
        "_last",
        "_now",
        "_buf",
        "_parts",
        "_tokens",
    )

    def __init__(self, provider_id: str, model: str) -> None:
        self._pid = provider_id
        self._model = model
        self._start = _clock()
        self._first: Optional[float] = None
        self._last: Optional[float] = None
        self._now = self._start
        self._buf = b""
        self._parts: List[str] = []
        self._tokens: Optional[int] = None

    def feed(self, chunk: bytes) -> None:
        try:
            self._now = _clock()
            self._buf += chunk
            *lines, self._buf = self._buf.split(b"\n")
            if len(self._buf) > MAX_LINE_BYTES:
                self._buf = b""
            for line in lines:
                self._line(line)
        except Exception as exc:  # never break the stream
            log.debug("stream meter: %s", exc)

    def finish(self) -> None:
        """The stream ended normally: record its decode rate."""
        try:
            if self._buf:
                self._line(self._buf)
                self._buf = b""
            if self._first is None or self._last is None:
                return
            tokens = self._tokens
            if tokens is None:
                tokens = count_tokens("".join(self._parts))
            span = self._last - self._first
            if tokens >= 2 and span > 0:
                record_tokens_per_second(self._pid, self._model, (tokens - 1) / span)
        except Exception as exc:
            log.debug("stream meter: %s", exc)

    def _line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            event = json.loads(payload)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        usage = event.get("usage")
        if isinstance(usage, dict):
            tokens = usage.get("completion_tokens")
            if isinstance(tokens, int) and not isinstance(tokens, bool):
                self._tokens = tokens
        choices = event.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict) and self._generated(delta):
                if self._first is None:
                    self._first = self._now
                    record_ttft(self._pid, self._model, self._now - self._start)
                self._last = self._now

    def _generated(self, delta: Dict[str, Any]) -> bool:
        """Keep the generated text of ``delta``; True when it has any."""
        found = False
        for key in ("content", "reasoning", "reasoning_content"):
            text = delta.get(key)
            if isinstance(text, str) and text:
                self._parts.append(text)
                found = True
        calls = delta.get("tool_calls")
        if isinstance(calls, list) and calls:
            found = True
            for call in calls:
                fn = call.get("function") if isinstance(call, dict) else None
                if isinstance(fn, dict):
                    for key in ("name", "arguments"):
                        if isinstance(fn.get(key), str):
                            self._parts.append(fn[key])
        return found


def get_stats(*, per_model: bool = False) -> Dict[str, Any]:
    """Counters per provider id. ``per_model`` adds, under ``models``, the
    measures of each model of the provider (a provider seen only through
    them gets zeroed counters)."""
    out = {pid: {**v} for pid, v in _stats.items()}
    if per_model:
        for (pid, model), stats in _models.items():
            entry = out.setdefault(pid, dict(_BASE))
            entry.setdefault("models", {})[model] = stats.as_dict()
    return out


def reset() -> None:
    """Forget every metric (tests)."""
    _stats.clear()
    _models.clear()


def _label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _num(value: float) -> str:
    return repr(round(value, 6))


def _histogram_lines(lines: List[str], name: str, help_text: str, attr: str) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} histogram")
    for (pid, model), stats in _models.items():
        hist: _Histogram = getattr(stats, attr)
        if not hist.count:
            continue
        lab = f'provider="{_label(pid)}",model="{_label(model)}"'
        running = 0
        for bound, n in zip(hist.bounds, hist.buckets):
            running += n
            lines.append(f'{name}_bucket{{{lab},le="{bound!r}"}} {running}')
        lines.append(f'{name}_bucket{{{lab},le="+Inf"}} {hist.count}')
        lines.append(f"{name}_sum{{{lab}}} {_num(hist.sum)}")
        lines.append(f"{name}_count{{{lab}}} {hist.count}")


def format_prometheus() -> str:
    """Return metrics in Prometheus exposition format."""
    lines: list[str] = []
    lines.append("# HELP plugboard_requests_total Total requests per provider")
    lines.append("# TYPE plugboard_requests_total counter")
    for pid, s in _stats.items():
        lines.append(
            f'plugboard_requests_total{{provider="{_label(pid)}"}} {s["requests"]}'
        )

    lines.append("# HELP plugboard_errors_total Total errors per provider")
    lines.append("# TYPE plugboard_errors_total counter")
    for pid, s in _stats.items():
        lines.append(
            f'plugboard_errors_total{{provider="{_label(pid)}"}} {s["errors"]}'
        )

    lines.append("# HELP plugboard_latency_avg_seconds Average latency per provider")
    lines.append("# TYPE plugboard_latency_avg_seconds gauge")
    for pid, s in _stats.items():
        lines.append(
            f'plugboard_latency_avg_seconds{{provider="{_label(pid)}"}} '
            f"{s.get('latency_avg', 0)}"
        )

    lines.append("# HELP plugboard_up Service health")
    lines.append("# TYPE plugboard_up gauge")
    lines.append("plugboard_up 1")

    _histogram_lines(
        lines,
        "plugboard_ttft_seconds",
        "Seconds from sending a streamed chat to its provider to the first "
        "generated chunk (admission wait excluded)",
        "ttft",
    )
    _histogram_lines(
        lines,
        "plugboard_tokens_per_second",
        "Completion tokens per second of a streamed answer, after its first token",
        "tokens_per_second",
    )
    _histogram_lines(
        lines,
        "plugboard_admission_wait_seconds",
        "Seconds a request waited for a slot on its provider (0 when one was free)",
        "admission_wait",
    )
    lines.append(
        "# HELP plugboard_providers_busy_total Requests refused with 503 "
        "providers_busy, counted on each full candidate route"
    )
    lines.append("# TYPE plugboard_providers_busy_total counter")
    for (pid, model), stats in _models.items():
        lines.append(
            f'plugboard_providers_busy_total{{provider="{_label(pid)}",'
            f'model="{_label(model)}"}} {stats.busy_refusals}'
        )

    return "\n".join(lines) + "\n"
