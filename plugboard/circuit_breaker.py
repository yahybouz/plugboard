"""
Circuit breaker — prevent cascading failures on slow/down models.

When a model fails N times consecutively within a window, it's marked
as "open" (broken). Requests to that model are immediately rejected
with a fallback suggestion. After a cooldown period, the circuit
moves to "half-open" and allows one test request through.

Also provides:
- Per-tier timeout recommendations
- Fallback model mapping
- Model availability status
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

# Circuit states
CLOSED = "closed"  # Normal — requests flow through
OPEN = "open"  # Broken — requests rejected, fallback used
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

# Fallback mapping: if model A is down, try model B
# Key: model suffix, Value: fallback model suffix
FALLBACK_MAP: Dict[str, str] = {
    # Claude fallbacks
    "claude-opus-4-7": "claude-opus-4-6",
    "claude-opus-4-6": "claude-opus-4-5",
    "claude-opus-4-5": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-5",
    "claude-sonnet-4-5": "claude-haiku-4-5",
    "claude-3-7-sonnet": "claude-sonnet-4-6",
    # GPT fallbacks
    "gpt-5.5-pro": "gpt-5.5",
    "gpt-5.5": "gpt-5-4",
    "gpt-5.5-mini": "gpt-5-mini",
    "gpt-5.5-nano": "gpt-5-nano",
    "gpt-5-4-pro": "gpt-5-4",
    "gpt-5-4": "gpt-5-2-pro",
    "gpt-5-2-pro": "gpt-5-2",
    "gpt-5-2": "gpt-5-1",
    "gpt-5-mini": "gpt-5-nano",
    # Reasoning fallbacks
    "o3-pro": "o3",
    "o3": "o3-mini",
    "o3-mini": "o4-mini",
    "o1-pro": "o1",
    # Gemini fallbacks
    "gemini-3-1-pro-preview": "gemini-3-pro-preview",
    "gemini-3-pro-preview": "gemini-2-5-pro",
    "gemini-2-5-pro": "gemini-2-5-flash",
    "gemini-2-5-flash": "gemini-2-0-flash",
    # Kimi fallbacks
    "kimi-k2.6-thinking": "kimi-k2.6",
    "kimi-k2.6": "kimi-k2.5",
    # DeepSeek fallbacks
    "deepseek-r1": "deepseek-v3",
    "deepseek-v3": "deepseek-v3.2",
    # Qwen fallbacks
    "qwen3.5": "qwen3",
    "qwen3": "qwen3-next",
    # MiniMax fallbacks
    "minimax-m2.7": "minimax-m2.5",
    "minimax-m2.5": "minimax-m2.1",
    "minimax-m2.1": "minimax-m2",
}


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
    """Extract model suffix from full ID for fallback matching."""
    if "--" in model_id:
        return model_id.split("--")[-1]
    if "/" in model_id:
        return model_id.split("/")[-1]
    return model_id


def check_circuit(model: str) -> Dict[str, Any]:
    """
    Check if a request should be allowed through.
    Returns: {"allowed": bool, "state": str, "fallback": str|None}
    """
    circuit = _get_circuit(model)
    suffix = _extract_suffix(model)
    now = time.monotonic()

    if circuit.state == CLOSED:
        return {"allowed": True, "state": CLOSED, "fallback": None}

    if circuit.state == OPEN:
        # Check cooldown
        if now - circuit.opened_at > _COOLDOWN_SECONDS:
            circuit.state = HALF_OPEN
            log.info("Circuit half-open for %s — allowing test request", suffix)
            return {"allowed": True, "state": HALF_OPEN, "fallback": None}

        # Still open — suggest fallback
        fallback = FALLBACK_MAP.get(suffix)
        log.warning("Circuit OPEN for %s — rejecting (fallback: %s)", suffix, fallback)
        return {"allowed": False, "state": OPEN, "fallback": fallback}

    if circuit.state == HALF_OPEN:
        # Allow one test request
        return {"allowed": True, "state": HALF_OPEN, "fallback": None}

    return {"allowed": True, "state": CLOSED, "fallback": None}


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
            "fallback": FALLBACK_MAP.get(suffix),
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
