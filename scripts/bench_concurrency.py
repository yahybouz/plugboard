#!/usr/bin/env python3
"""Concurrency bench of a Plugboard gateway (PLU-17, D-26, D-27). Not run by the CI.

Sends streamed chats to ``<url>/v1/chat/completions`` from 1, 2, 4 then 8
clients at once (``--clients``), ``--rounds`` chats each, one level after
the other, and prints per level: status codes, time to the response status,
time to the first word, total time and tokens per second.

The key is read from an environment variable (``--key-env``, default
``PLUGBOARD_BENCH_KEY``) and is never printed nor written. Each chat asks
for no cached answer and carries a distinct prompt.

``--check`` exits 1 unless every answer is a 200 or a 503
``providers_busy``, and every 503 came within ``--max-refusal-s`` seconds
(10.5: the 10 s admission wait, plus margin). The D-26 thresholds are
optional checks: ``--first-word-p95-max 5`` and ``--tps-median-min 5``.
Exit codes: 0 done (and checks passed), 1 a check failed, 2 bad usage.

Example (D-26 at 4 clients, 3 chats each):
  PLUGBOARD_BENCH_KEY=... python scripts/bench_concurrency.py \\
      --url https://gateway.example --model ollama:qwen2.5:7b \\
      --clients 1,2,4,8 --rounds 3 --check \\
      --first-word-p95-max 5 --tps-median-min 5 --json bench.json
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import math
import os
import statistics
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import aiohttp

BUSY = "503 providers_busy"
DEFAULT_PROMPT = (
    "Write a short paragraph, about eighty words, on why the sea looks blue."
)


def percentile(values: List[float], q: float) -> Optional[float]:
    """Nearest-rank percentile; None for no value."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _stats(values: List[float]) -> Dict[str, Optional[float]]:
    return {
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def _generated(delta: Any) -> bool:
    if not isinstance(delta, dict):
        return False
    if any(
        isinstance(delta.get(k), str) and delta[k]
        for k in ("content", "reasoning", "reasoning_content")
    ):
        return True
    return bool(delta.get("tool_calls"))


async def one_chat(
    session: aiohttp.ClientSession,
    url: str,
    key: str,
    body: Dict[str, Any],
    timeout: float,
) -> Dict[str, Any]:
    """One streamed chat; times are seconds from the request sent."""
    out: Dict[str, Any] = {
        "status": 0,
        "code": None,
        "retry_after": None,
        "status_s": None,
        "first_word_s": None,
        "total_s": None,
        "completion_tokens": None,
        "tokens_per_second": None,
        "error": None,
    }
    headers = {"Authorization": f"Bearer {key}", "Accept": "text/event-stream"}
    t0 = time.perf_counter()
    first = last = None
    events = 0
    usage_tokens = None
    try:
        async with session.post(
            url,
            json=body,
            headers=headers,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            out["status"] = resp.status
            out["status_s"] = time.perf_counter() - t0
            out["retry_after"] = resp.headers.get("Retry-After")
            if resp.status != 200:
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                if isinstance(data, dict) and isinstance(data.get("code"), str):
                    out["code"] = data["code"]
                return out
            async for raw_line in resp.content:
                line = raw_line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    break
                try:
                    evt = json.loads(payload)
                except ValueError:
                    continue
                if not isinstance(evt, dict):
                    continue
                if evt.get("error"):
                    out["error"] = "error event in the stream"
                usage = evt.get("usage")
                if isinstance(usage, dict) and isinstance(
                    usage.get("completion_tokens"), int
                ):
                    usage_tokens = usage["completion_tokens"]
                choices = evt.get("choices")
                if not isinstance(choices, list):
                    continue
                if any(
                    _generated(c.get("delta")) for c in choices if isinstance(c, dict)
                ):
                    now = time.perf_counter() - t0
                    first = now if first is None else first
                    last = now
                    events += 1
    except asyncio.TimeoutError:
        out["error"] = f"timeout after {timeout} s"
    except aiohttp.ClientError as exc:
        out["error"] = f"connection failed: {type(exc).__name__}"
    out["total_s"] = time.perf_counter() - t0
    out["first_word_s"] = first
    tokens = usage_tokens if usage_tokens and usage_tokens > 0 else events
    out["completion_tokens"] = tokens if first is not None else None
    if first is not None and last is not None and tokens >= 2 and last > first:
        out["tokens_per_second"] = (tokens - 1) / (last - first)
    return out


def _label(result: Dict[str, Any]) -> str:
    if result["status"] == 503 and result["code"] == "providers_busy":
        return BUSY
    if result["status"] == 200 and result["error"]:
        return "200 stream error"
    if result["status"] == 0:
        return "no answer"
    return str(result["status"])


async def run_level(
    url: str, key: str, args: argparse.Namespace, clients: int
) -> Dict[str, Any]:
    start = asyncio.Event()
    stamp = time.strftime("%H%M%S")

    async def client(session: aiohttp.ClientSession, n: int) -> List[Dict[str, Any]]:
        await start.wait()
        results = []
        for r in range(args.rounds):
            body = {
                "model": args.model,
                "messages": [
                    {
                        "role": "user",
                        "content": f"{args.prompt} (bench {stamp}, "
                        f"{clients} clients, client {n}, round {r + 1})",
                    }
                ],
                "stream": True,
                "cache": False,
                "max_tokens": args.max_tokens,
            }
            results.append(await one_chat(session, url, key, body, args.timeout))
        return results

    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        tasks = [asyncio.create_task(client(session, n + 1)) for n in range(clients)]
        start.set()
        requests = [res for batch in await asyncio.gather(*tasks) for res in batch]

    codes: Dict[str, int] = {}
    for res in requests:
        codes[_label(res)] = codes.get(_label(res), 0) + 1
    ok = [r for r in requests if r["status"] == 200 and not r["error"]]
    tps = [r["tokens_per_second"] for r in ok if r["tokens_per_second"] is not None]
    return {
        "clients": clients,
        "codes": codes,
        "status_s": _stats(
            [r["status_s"] for r in requests if r["status_s"] is not None]
        ),
        "first_word_s": _stats(
            [r["first_word_s"] for r in ok if r["first_word_s"] is not None]
        ),
        "total_s": _stats([r["total_s"] for r in ok]),
        "tokens_per_second": {
            "median": statistics.median(tps) if tps else None,
            "min": min(tps) if tps else None,
        },
        "refusal_max_s": max(
            (r["status_s"] for r in requests if _label(r) == BUSY), default=None
        ),
        "requests": requests,
    }


def check(levels: List[Dict[str, Any]], args: argparse.Namespace) -> List[str]:
    failures = []
    for lvl in levels:
        n = lvl["clients"]
        if args.check:
            other = {c: k for c, k in lvl["codes"].items() if c not in ("200", BUSY)}
            if other:
                failures.append(
                    f"{n} clients: answers other than 200 and {BUSY}: {other}"
                )
            slow = lvl["refusal_max_s"]
            if slow is not None and slow > args.max_refusal_s:
                failures.append(
                    f"{n} clients: a {BUSY} came after {slow:.2f} s "
                    f"(bound {args.max_refusal_s} s)"
                )
        p95 = lvl["first_word_s"]["p95"]
        if args.first_word_p95_max is not None and p95 is not None:
            if p95 > args.first_word_p95_max:
                failures.append(
                    f"{n} clients: first word p95 {p95:.2f} s "
                    f"> {args.first_word_p95_max} s"
                )
        median = lvl["tokens_per_second"]["median"]
        if args.tps_median_min is not None and median is not None:
            if median < args.tps_median_min:
                failures.append(
                    f"{n} clients: median {median:.1f} tok/s < {args.tps_median_min} tok/s"
                )
    return failures


def _fmt(stats: Dict[str, Optional[float]]) -> str:
    return "/".join("-" if v is None else f"{v:.2f}" for v in stats.values())


def print_report(levels: List[Dict[str, Any]], failures: List[str], args) -> None:
    print(
        f"Plugboard bench: model {args.model}, {args.rounds} chat(s) per client, "
        "streamed, times in seconds (p50/p95/max)"
    )
    head = ("clients", "codes", "status", "first word", "total", "tok/s med/min")
    rows = []
    for lvl in levels:
        codes = ", ".join(f"{c} x{k}" for c, k in sorted(lvl["codes"].items()))
        tps = lvl["tokens_per_second"]
        rows.append(
            (
                str(lvl["clients"]),
                codes,
                _fmt(lvl["status_s"]),
                _fmt(lvl["first_word_s"]),
                _fmt(lvl["total_s"]),
                "/".join("-" if v is None else f"{v:.1f}" for v in tps.values()),
            )
        )
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    for row in [head, *rows]:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    errors = sorted(
        {
            r["error"]
            for lvl in levels
            for r in lvl["requests"]
            if r["error"] is not None
        }
    )
    for err in errors:
        print(f"error: {err}")
    if (
        args.check
        or args.first_word_p95_max is not None
        or args.tps_median_min is not None
    ):
        print("check: passed" if not failures else "check: FAILED")
        for line in failures:
            print(f"  - {line}")


def _endpoint(url: str) -> Optional[str]:
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    return url.rstrip("/") + "/v1/chat/completions"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _levels(text: str) -> List[int]:
    values = [int(v) for v in text.split(",") if v.strip()]
    if not values or any(v < 1 or v > 64 for v in values):
        raise argparse.ArgumentTypeError("1 to 64 clients per level")
    return values


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", required=True, help="gateway base URL, e.g. https://host")
    p.add_argument("--model", required=True, help="model id, as in GET /v1/models")
    p.add_argument(
        "--key-env",
        default="PLUGBOARD_BENCH_KEY",
        help="environment variable holding the client key (never printed)",
    )
    p.add_argument("--clients", type=_levels, default=[1, 2, 4, 8])
    p.add_argument("--rounds", type=int, default=1, help="chats per client")
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--timeout", type=float, default=180.0, help="per chat, seconds")
    p.add_argument("--check", action="store_true", help="PLU-17 criterion")
    p.add_argument("--max-refusal-s", type=float, default=10.5)
    p.add_argument("--first-word-p95-max", type=float, default=None)
    p.add_argument("--tps-median-min", type=float, default=None)
    p.add_argument("--json", default=None, help="write the full results there")
    args = p.parse_args(argv)
    if args.rounds < 1 or args.max_tokens < 1 or args.timeout <= 0:
        p.error("--rounds, --max-tokens and --timeout must be positive")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    url = _endpoint(args.url)
    if url is None:
        print("error: --url must be http(s)://host[:port][/path]", file=sys.stderr)
        return 2
    key = os.environ.get(args.key_env, "").strip()
    if not key:
        print(f"error: set the client key in ${args.key_env}", file=sys.stderr)
        return 2
    host = urlsplit(url).hostname or ""
    if urlsplit(url).scheme == "http" and not _is_loopback(host):
        print(f"warning: the key goes to {host} over plain http", file=sys.stderr)

    async def run_all() -> List[Dict[str, Any]]:
        return [await run_level(url, key, args, n) for n in args.clients]

    levels = asyncio.run(run_all())
    failures = check(levels, args)
    print_report(levels, failures, args)
    if args.json:
        report = {
            "url": args.url,
            "model": args.model,
            "rounds": args.rounds,
            "max_tokens": args.max_tokens,
            "levels": levels,
            "check": {"passed": not failures, "failures": failures},
        }
        text = _scrub(json.dumps(report, indent=2), key)
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
