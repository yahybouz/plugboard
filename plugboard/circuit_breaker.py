"""
Circuit breaker — track the routes whose provider keeps failing.

When a route (``provider-id:model``) fails N times in a row on the
provider's side, it's marked "open": check_circuit() answers
``allowed: False``, the LLM pricing pass skips the route and
/admin/circuits shows it. forward() still sends requests to it. After a
cooldown period, the circuit moves to "half-open" and the next success
closes it.

Also provides per-tier timeout recommendations.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Circuit states
CLOSED = "closed"  # Normal — requests flow through
OPEN = "open"  # Broken — check_circuit() answers allowed=False
HALF_OPEN = "half_open"  # Testing — one request allowed through

# Config
_FAILURE_THRESHOLD = 3  # Consecutive failures to open circuit
_COOLDOWN_SECONDS = 60.0  # Time before half-open (try again)
_SUCCESS_THRESHOLD = 1  # Successes in half-open to close circuit

# Per-tier timeouts (seconds)
# All set to 900s (15 min) — OpenClaw agents spawn sub-agents that chain
# multiple LLM calls. Complex reasoning tasks can take 10+ minutes.
TIER_TIMEOUTS: Dict[str, float] = {
    "edge": 900.0,
    "fast": 900.0,
    "mid": 900.0,
    "flagship": 900.0,
    "specialized": 900.0,
    "reasoning": 900.0,
}

# What counts as a failure (PLU-3): only the provider's own. HTTP statuses
# that say the provider is failing besides its 5xx: credentials it refuses and
# its rate limit or quota. Any other status (400, 404, 413, 422...) is the
# answer to the request itself and does not count.
_PROVIDER_FAULT_STATUSES = frozenset({401, 403, 429})

# Lower-case fragments of an error that says the context or the message is too
# long, whatever its status or CLI: the request caused it. Anthropic and
# claude ("prompt is too long", "exceed context limit"), OpenAI
# ("context_length_exceeded", "maximum context length", "string too long"),
# codex ("exceeds the context window"), Bedrock ("input is too long"),
# llama.cpp ("exceeds the available context size"), Ollama ("prompt is too
# long", "exceeded max context length"), Gemini ("exceeds the maximum number
# of tokens"). The router's answer to the client follows the same rule
# (router._names_the_request): such an error gives 400, never 502.
_REQUEST_TOO_LONG = (
    "prompt is too long",
    "input is too long",
    "message is too long",
    "string too long",
    "context_length_exceeded",
    "context length",
    "context limit",
    "context window",
    "context size",
    "exceeds the maximum number of tokens",
)


def is_request_too_long(message: str) -> bool:
    """True when an error says the context or the message is too long."""
    text = (message or "").lower()
    return any(fragment in text for fragment in _REQUEST_TOO_LONG)


def is_provider_fault(message: str, status: Optional[int] = None) -> bool:
    """Whether a failed call counts against its route's circuit.

    ``status`` is the HTTP status of the provider's answer, or None when there
    was none: a timeout, a network error or a failed CLI run, which count
    unless the error says the request is too long.
    """
    if is_request_too_long(message):
        return False
    if status is None:
        return True
    return status in _PROVIDER_FAULT_STATUSES or 500 <= status <= 599


class _CircuitState:
    """State for a single model circuit."""

    __slots__ = ("state", "failures", "last_failure", "last_success", "opened_at")

    def __init__(self):
        self.state: str = CLOSED
        self.failures: int = 0
        self.last_failure: float = 0
        self.last_success: float = 0
        self.opened_at: float = 0


# Global circuit states per model
_circuits: Dict[str, _CircuitState] = {}


def _get_circuit(model: str) -> _CircuitState:
    if model not in _circuits:
        _circuits[model] = _CircuitState()
    return _circuits[model]


def _extract_suffix(model_id: str) -> str:
    """Extract model suffix from full ID (display and reset by name)."""
    if "--" in model_id:
        return model_id.split("--")[-1]
    if "/" in model_id:
        return model_id.split("/")[-1]
    return model_id


def check_circuit(model: str) -> Dict[str, Any]:
    """
    Check if a request should be allowed through.
    Returns: {"allowed": bool, "state": str}
    """
    circuit = _get_circuit(model)
    suffix = _extract_suffix(model)
    now = time.monotonic()

    if circuit.state == CLOSED:
        return {"allowed": True, "state": CLOSED}

    if circuit.state == OPEN:
        # Check cooldown
        if now - circuit.opened_at > _COOLDOWN_SECONDS:
            circuit.state = HALF_OPEN
            log.info("Circuit half-open for %s — allowing test request", suffix)
            return {"allowed": True, "state": HALF_OPEN}

        log.warning("Circuit OPEN for %s", suffix)
        return {"allowed": False, "state": OPEN}

    if circuit.state == HALF_OPEN:
        # Allow one test request
        return {"allowed": True, "state": HALF_OPEN}

    return {"allowed": True, "state": CLOSED}


def record_success(model: str) -> None:
    """Record a successful request — may close a half-open circuit."""
    circuit = _get_circuit(model)
    circuit.failures = 0
    circuit.last_success = time.monotonic()

    if circuit.state == HALF_OPEN:
        circuit.state = CLOSED
        suffix = _extract_suffix(model)
        log.info("Circuit CLOSED for %s — model recovered", suffix)


def record_failure(model: str) -> None:
    """Record a failed request — may open the circuit."""
    circuit = _get_circuit(model)
    circuit.failures += 1
    circuit.last_failure = time.monotonic()

    if circuit.state == HALF_OPEN:
        # Failed during test — reopen
        circuit.state = OPEN
        circuit.opened_at = time.monotonic()
        suffix = _extract_suffix(model)
        log.warning("Circuit re-OPENED for %s — still failing", suffix)
        return

    if circuit.failures >= _FAILURE_THRESHOLD and circuit.state == CLOSED:
        circuit.state = OPEN
        circuit.opened_at = time.monotonic()
        suffix = _extract_suffix(model)
        log.warning("Circuit OPENED for %s after %d failures", suffix, circuit.failures)


def get_model_timeout(model: str) -> Optional[float]:
    """Get recommended timeout based on model tier."""
    try:
        from plugboard.benchmarks import get_model_info

        info = get_model_info(model)
        if info:
            tier = info.get("tier", "mid")
            return TIER_TIMEOUTS.get(tier, 60.0)
    except Exception:
        pass
    return None


def get_all_circuits() -> Dict[str, Dict[str, Any]]:
    """Return status of all tracked circuits."""
    now = time.monotonic()
    result = {}
    for model, circuit in _circuits.items():
        suffix = _extract_suffix(model)
        result[suffix] = {
            "state": circuit.state,
            "failures": circuit.failures,
            "last_failure_ago": round(now - circuit.last_failure, 1)
            if circuit.last_failure
            else None,
            "last_success_ago": round(now - circuit.last_success, 1)
            if circuit.last_success
            else None,
        }
    return result


def reset_circuit(model: str) -> None:
    """Manually reset a circuit to closed."""
    if model in _circuits:
        _circuits[model] = _CircuitState()
    # Also try suffix
    for key in list(_circuits.keys()):
        if _extract_suffix(key) == model:
            _circuits[key] = _CircuitState()
