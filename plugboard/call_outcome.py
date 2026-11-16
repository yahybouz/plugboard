"""The last real call of a route, as the client catalogue reads it (PLU-55).

The client catalogue leaves out a model that cannot be called now. Probes are
off by default (BUG-45), so a CLI model's failure was never seen. The circuit
breaker already records each real call's outcome per route
(``provider-id:native-model``): a success, or a failure the provider caused
(``is_provider_fault``: not a prompt too long, not a model the subscription
does not serve, not a 4xx answering the request). A key the gateway refuses
never reaches it. This module only reads that record, without touching the
circuit's order or state: nothing is probed, nothing is written.

The record lives in memory: a restart forgets it, and the model is listed
again until its next call.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple


def last_call(provider_id: str, native_model: str) -> Optional[Tuple[float, bool]]:
    """``(seconds since the route's last real call, whether it succeeded)``.

    None when the route has no recorded call since the gateway started.
    """
    from plugboard import circuit_breaker

    # Read, never _get_circuit(): that would create the entry and move it in
    # the breaker's least-recently-used order.
    circuit = circuit_breaker._circuits.get(f"{provider_id}:{native_model}")
    if circuit is None:
        return None
    failed_at, succeeded_at = circuit.last_failure, circuit.last_success
    if not (failed_at or succeeded_at):
        return None
    now = time.monotonic()
    if failed_at > succeeded_at:
        return now - failed_at, False
    return now - succeeded_at, True
