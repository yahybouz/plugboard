"""The last real call of a route, as the client catalogue reads it (PLU-55).

The client catalogue leaves out a model that cannot be called now. Probes are
off by default (BUG-45), so a CLI model's failure was never seen. The circuit
breaker already records each real call's outcome per route
(``provider-id:native-model``): a success, or a failure the provider caused
(``is_provider_fault``: not a prompt too long, not a model the subscription
does not serve, not a 4xx answering the request). A key the gateway refuses
never reaches it. This module only reads that record, without touching the
circuit's order or state: nothing is probed, nothing is written.

One client's call must not hide a model from every other client (review M1
of 4.14.4). A failed call hides the model only when the breaker counts its
threshold of failures in a row on the route (the threshold that opens the
circuit) and the last of them is neither a timeout nor a quota: those are
left to the route pauses and the breaker. A success resets the count, so a
more recent success always brings the model back.

The record lives in memory: a restart forgets it, and the model is listed
again until its next call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional, Tuple


def is_transient(exc: BaseException) -> bool:
    """Whether a failed call was a timeout or a quota (an HTTP 429, a CLI's
    quota): it counts for the breaker, never for the client catalogue."""
    from plugboard.cli_subprocess import CliSubprocessError
    from plugboard.router import ProviderQuotaError, ProviderTimeoutError

    if isinstance(
        exc, (ProviderQuotaError, ProviderTimeoutError, asyncio.TimeoutError)
    ):
        return True
    return isinstance(exc, CliSubprocessError) and bool(exc.timed_out or exc.quota)


def last_call(provider_id: str, native_model: str) -> Optional[Tuple[float, bool]]:
    """``(seconds since the route's last telling call, whether it succeeded)``.

    A failure tells only past the breaker's threshold, the last failure not
    transient; otherwise the last success, if any, is the observation. None
    when the route has nothing telling since the gateway started.
    """
    from plugboard import circuit_breaker

    # Read, never _get_circuit(): that would create the entry and move it in
    # the breaker's least-recently-used order.
    circuit = circuit_breaker._circuits.get(f"{provider_id}:{native_model}")
    if circuit is None:
        return None
    failed_at, succeeded_at = circuit.last_failure, circuit.last_success
    now = time.monotonic()
    if (
        failed_at > succeeded_at
        and circuit.failures >= circuit_breaker._FAILURE_THRESHOLD
        and not circuit.last_failure_transient
    ):
        return now - failed_at, False
    if succeeded_at:
        return now - succeeded_at, True
    return None
