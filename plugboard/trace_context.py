"""The caller's W3C Trace Context, read from the request (PLU-107).

``_RequestIdMiddleware`` reads ``traceparent`` and ``tracestate`` with
``from_headers()`` and keeps the result in ``current`` for the request, next
to its request id. The usage entry of the call records the trace id
(PLU-106) and the JSON logs carry it (PLU-108).

Only a valid header is kept (W3C Trace Context, level 1 parsing rules): an
invalid or missing one gives None, never a 4xx and never an invented trace
id. Nothing here logs or returns the raw header. The gateway does not send
``traceparent`` on to providers nor back to the caller, and starts no span
of its own.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterable, Optional

# Version 00 is exactly 55 characters. A later version may append fields,
# but a header longer than this is not read at all.
MAX_TRACEPARENT_LEN = 512
# W3C asks to propagate at least 512 characters of tracestate; more is dropped.
MAX_TRACESTATE_LEN = 512

_V00_LEN = 55
_FIELDS = re.compile(
    r"(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})"
)
_PRINTABLE = re.compile(r"[\x20-\x7e]*")


@dataclass(frozen=True)
class TraceContext:
    """A valid incoming trace context. ``span_id`` is the caller's span (the
    W3C parent-id); ``tracestate`` is None when absent or dropped."""

    trace_id: str
    span_id: str
    flags: str
    tracestate: Optional[str] = None


current: ContextVar[Optional[TraceContext]] = ContextVar("trace_context", default=None)


def parse_traceparent(value: str) -> Optional[TraceContext]:
    """The trace context of a ``traceparent`` value, or None when invalid.

    Version 00 must be exactly ``00-<32 hex>-<16 hex>-<2 hex>``, lower case,
    with neither id all zeros. A later version (not ff) is read by its
    version 00 fields, and anything after them must start with a dash
    (forward compatibility)."""
    value = value.strip(" \t")
    if len(value) < _V00_LEN or len(value) > MAX_TRACEPARENT_LEN:
        return None
    match = _FIELDS.fullmatch(value[:_V00_LEN])
    if match is None:
        return None
    version = match["version"]
    if version == "ff":
        return None
    if version == "00" and len(value) != _V00_LEN:
        return None
    if len(value) > _V00_LEN and value[_V00_LEN] != "-":
        return None
    trace_id, span_id = match["trace_id"], match["span_id"]
    if trace_id == "0" * 32 or span_id == "0" * 16:
        return None
    return TraceContext(trace_id=trace_id, span_id=span_id, flags=match["flags"])


def _bounded_tracestate(values: Iterable[str]) -> Optional[str]:
    """The tracestate headers joined, or None when absent, longer than
    MAX_TRACESTATE_LEN or not printable ASCII. Not parsed further."""
    joined = ",".join(v.strip(" \t") for v in values)
    if not joined or len(joined) > MAX_TRACESTATE_LEN:
        return None
    if _PRINTABLE.fullmatch(joined) is None:
        return None
    return joined


def from_headers(
    traceparents: Iterable[str], tracestates: Iterable[str]
) -> Optional[TraceContext]:
    """The trace context of a request's headers (all their values), or None.

    Several ``traceparent`` headers are ignored: which one is right cannot be
    told. ``tracestate`` is only read with a valid ``traceparent``."""
    traceparents = list(traceparents)
    if len(traceparents) != 1:
        return None
    parsed = parse_traceparent(traceparents[0])
    if parsed is None:
        return None
    tracestate = _bounded_tracestate(tracestates)
    if tracestate is None:
        return parsed
    return TraceContext(parsed.trace_id, parsed.span_id, parsed.flags, tracestate)
