# plugboard/metrics.py
from __future__ import annotations

from typing import Any, Dict

_stats: Dict[str, Any] = {}


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


def get_stats() -> Dict[str, Any]:
    return {pid: {**v} for pid, v in _stats.items()}


def format_prometheus() -> str:
    """Return metrics in Prometheus exposition format."""
    lines: list[str] = []
    lines.append("# HELP plugboard_requests_total Total requests per provider")
    lines.append("# TYPE plugboard_requests_total counter")
    for pid, s in _stats.items():
        lines.append(f'plugboard_requests_total{{provider="{pid}"}} {s["requests"]}')

    lines.append("# HELP plugboard_errors_total Total errors per provider")
    lines.append("# TYPE plugboard_errors_total counter")
    for pid, s in _stats.items():
        lines.append(f'plugboard_errors_total{{provider="{pid}"}} {s["errors"]}')

    lines.append("# HELP plugboard_latency_avg_seconds Average latency per provider")
    lines.append("# TYPE plugboard_latency_avg_seconds gauge")
    for pid, s in _stats.items():
        lines.append(
            f'plugboard_latency_avg_seconds{{provider="{pid}"}} {s.get("latency_avg", 0)}'
        )

    lines.append("# HELP plugboard_up Service health")
    lines.append("# TYPE plugboard_up gauge")
    lines.append("plugboard_up 1")

    return "\n".join(lines) + "\n"
