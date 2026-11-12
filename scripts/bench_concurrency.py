#!/usr/bin/env python3
"""Concurrency bench of a Plugboard gateway (PLU-17, D-26, D-27). Not run by the CI.

Sends streamed chats to ``<url>/v1/chat/completions`` from 1, 2, 4 then 8
clients at once (``--clients``), ``--rounds`` chats each, one level after
the other, and prints per level: status codes, time to the status of the
served chats and of the refusals, time to the first word (the first
generated chunk: text, reasoning or a tool call), total time and tokens per
second (after the first word).

A chat is served when it gets a 200 event stream that carries at least one
word and ends (``[DONE]`` or a ``finish_reason``); any other 200 is a
``200 stream error``. The key is read from an environment variable
(``--key-env``, default ``PLUGBOARD_BENCH_KEY``) and is never printed nor
written; it is never sent over plain http to another machine without
``--allow-http``. Each chat asks for no cached answer and carries a distinct
prompt.

``--check`` (PLU-17): every answer is a 200 or a 503 ``providers_busy``;
every 503 comes within ``--max-wait-s`` (10.5: the gateway's 10 s admission
wait plus a margin) and every 200 within ``--max-wait-s`` plus
``--max-first-chunk-s`` (the gateway sends a 200 with the provider's first
chunk, after the wait).

D-26 targets, checked at the levels of ``--targets-at`` (default 4 clients):
every chat served, ``--first-word-p95-max`` seconds (nearest rank, the max
for up to 20 chats) and ``--tps-min`` tokens per second for each client
(the median of its rounds).

To measure what a provider holds, its ``max_concurrency`` must be at least
the level: a local provider takes one chat at a time by default.

Exit codes: 0 done (checks passed), 1 a check failed, 2 bad usage,
3 the bench itself failed.

Example (D-26 at 4 clients, 3 chats each, after one warm-up chat):
  PLUGBOARD_BENCH_KEY=... python scripts/bench_concurrency.py \\
      --url https://gateway.example --model ollama:qwen2.5:7b \\
      --clients 1,2,4,8 --rounds 3 --warmup 1 --check \\
      --first-word-p95-max 5 --tps-min 5 --json bench.json
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
from typing import Any, Dict, List, Optional, TextIO
from urllib.parse import urlsplit

import aiohttp

BUSY = "503 providers_busy"
STREAM_ERROR = "200 stream error"
DEFAULT_PROMPT = (
    "Write a short paragraph, about eighty words, on why the sea looks blue."
)
# A rate is kept only when the words came over this long, in 3 reads at least.
MIN_RATE_SPAN_S = 0.05
MIN_RATE_EVENTS = 3


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


def _scrub(text: Optional[str], key: str) -> Optional[str]:
    return text.replace(key, "***") if text and key else text


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
    done = False
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
            out["retry_after"] = _scrub(resp.headers.get("Retry-After"), key)
            if resp.status != 200:
                raw = await resp.text(errors="replace")
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = None
                if isinstance(data, dict) and isinstance(data.get("code"), str):
                    out["code"] = _scrub(data["code"], key)
                return out
            if resp.content_type != "text/event-stream":
                out["error"] = "200 without an event stream"
                return out
            async for raw_line in resp.content:
                line = raw_line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    done = True
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
                choices = [c for c in choices if isinstance(c, dict)]
                if any(c.get("finish_reason") for c in choices):
                    done = True
                if any(_generated(c.get("delta")) for c in choices):
                    now = time.perf_counter() - t0
                    first = now if first is None else first
                    last = now
                    events += 1
    except asyncio.TimeoutError:
        out["error"] = f"timeout after {timeout} s"
    except aiohttp.ClientError as exc:
        out["error"] = f"connection failed: {type(exc).__name__}"
    except Exception as exc:  # never lose the bench for one chat
        out["error"] = f"unexpected error: {type(exc).__name__}"
    out["total_s"] = time.perf_counter() - t0
    if out["status"] != 200:
        return out
    if out["error"] is None and first is None:
        out["error"] = "no word generated"
    elif out["error"] is None and not done:
        out["error"] = "stream ended without [DONE]"
    out["first_word_s"] = first
    tokens = usage_tokens if usage_tokens and usage_tokens > 0 else events
    out["completion_tokens"] = tokens if first is not None else None
    if (
        first is not None
        and last is not None
        and events >= MIN_RATE_EVENTS
        and tokens >= 2
        and last - first >= MIN_RATE_SPAN_S
    ):
        out["tokens_per_second"] = (tokens - 1) / (last - first)
    return out


def _served(result: Dict[str, Any]) -> bool:
    return result["status"] == 200 and not result["error"]


def _label(result: Dict[str, Any]) -> str:
    if result["status"] == 503 and result["code"] == "providers_busy":
        return BUSY
    if result["status"] == 200:
        return "200" if not result["error"] else STREAM_ERROR
    if result["status"] == 0:
        return "no answer"
    return str(result["status"])


def _body(args: argparse.Namespace, tag: str) -> Dict[str, Any]:
    return {
        "model": args.model,
        "messages": [{"role": "user", "content": f"{args.prompt} ({tag})"}],
        "stream": True,
        "cache": False,
        "max_tokens": args.max_tokens,
    }


async def run_level(
    session: aiohttp.ClientSession,
    url: str,
    key: str,
    args: argparse.Namespace,
    clients: int,
    stamp: str,
) -> Dict[str, Any]:
    async def client(n: int) -> List[Dict[str, Any]]:
        results = []
        for r in range(1, args.rounds + 1):
            tag = f"bench {stamp}, {clients} clients, client {n}, round {r}"
            res = await one_chat(session, url, key, _body(args, tag), args.timeout)
            results.append({"client": n, "round": r, **res})
        return results

    # The tasks start together: none awaits anything before its request.
    batches = await asyncio.gather(*(client(n) for n in range(1, clients + 1)))
    requests = [res for batch in batches for res in batch]

    codes: Dict[str, int] = {}
    for res in requests:
        codes[_label(res)] = codes.get(_label(res), 0) + 1
    ok = [r for r in requests if _served(r)]
    tps = [r["tokens_per_second"] for r in ok if r["tokens_per_second"] is not None]
    return {
        "clients": clients,
        "codes": codes,
        "status_200_s": _stats(
            [
                r["status_s"]
                for r in requests
                if r["status"] == 200 and r["status_s"] is not None
            ]
        ),
        "refusal_s": _stats([r["status_s"] for r in requests if _label(r) == BUSY]),
        "first_word_s": _stats([r["first_word_s"] for r in ok]),
        "total_s": _stats([r["total_s"] for r in ok]),
        "tokens_per_second": {
            "min": min(tps) if tps else None,
            "median": statistics.median(tps) if tps else None,
        },
        "requests": requests,
    }


def check(levels: List[Dict[str, Any]], args: argparse.Namespace) -> List[str]:
    failures: List[str] = []
    if args.check:
        bound_200 = args.max_wait_s + args.max_first_chunk_s
        for lvl in levels:
            n = lvl["clients"]
            other = {c: k for c, k in lvl["codes"].items() if c not in ("200", BUSY)}
            if other:
                failures.append(
                    f"{n} clients: answers other than 200 and {BUSY}: {other}"
                )
            slow = lvl["refusal_s"]["max"]
            if slow is not None and slow > args.max_wait_s:
                failures.append(
                    f"{n} clients: a {BUSY} came after {slow:.2f} s "
                    f"(bound {args.max_wait_s} s)"
                )
            slow = lvl["status_200_s"]["max"]
            if slow is not None and slow > bound_200:
                failures.append(
                    f"{n} clients: a 200 came after {slow:.2f} s (bound {bound_200} s)"
                )
    if args.first_word_p95_max is None and args.tps_min is None:
        return failures
    by_clients = {lvl["clients"]: lvl for lvl in levels}
    for n in args.targets_at:
        lvl = by_clients.get(n)
        if lvl is None:
            failures.append(f"targets: no level at {n} clients")
            continue
        requests = lvl["requests"]
        missed = sum(1 for r in requests if not _served(r))
        if missed:
            failures.append(f"{n} clients: {missed}/{len(requests)} chats not served")
        if args.first_word_p95_max is not None:
            p95 = lvl["first_word_s"]["p95"]
            if p95 is None:
                failures.append(f"{n} clients: no first word measured")
            elif p95 > args.first_word_p95_max:
                failures.append(
                    f"{n} clients: first word p95 {p95:.2f} s "
                    f"> {args.first_word_p95_max} s"
                )
        if args.tps_min is not None:
            for c in range(1, n + 1):
                rates = [
                    r["tokens_per_second"]
                    for r in requests
                    if r["client"] == c and r["tokens_per_second"] is not None
                ]
                if not rates:
                    failures.append(f"{n} clients: client {c} has no rate measured")
                elif statistics.median(rates) < args.tps_min:
                    failures.append(
                        f"{n} clients: client {c} at {statistics.median(rates):.1f} "
                        f"tok/s < {args.tps_min} tok/s"
                    )
    return failures


def _fmt(values: Dict[str, Optional[float]], digits: int = 2) -> str:
    return "/".join("-" if v is None else f"{v:.{digits}f}" for v in values.values())


def print_report(levels: List[Dict[str, Any]], failures: List[str], args) -> None:
    print(
        f"Plugboard bench: model {args.model}, {args.rounds} chat(s) per client, "
        "streamed; seconds as p50/p95/max"
    )
    head = (
        "clients",
        "codes",
        "200 status",
        "refusal",
        "first word",
        "total",
        "tok/s min/med",
    )
    rows = [head]
    for lvl in levels:
        codes = ", ".join(f"{c} x{k}" for c, k in sorted(lvl["codes"].items()))
        rows.append(
            (
                str(lvl["clients"]),
                codes,
                _fmt(lvl["status_200_s"]),
                _fmt(lvl["refusal_s"]),
                _fmt(lvl["first_word_s"]),
                _fmt(lvl["total_s"]),
                _fmt(lvl["tokens_per_second"], 1),
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    errors = sorted(
        {r["error"] for lvl in levels for r in lvl["requests"] if r["error"]}
    )
    for err in errors:
        print(f"error: {err}")
    if args.check or args.first_word_p95_max is not None or args.tps_min is not None:
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
    try:
        values = [int(v) for v in text.split(",") if v.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("comma-separated client counts") from None
    if not values or any(v < 1 or v > 64 for v in values):
        raise argparse.ArgumentTypeError("1 to 64 clients per level")
    return values


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("a number") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("a finite number above 0")
    return value


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("a whole number") from None
    if value < 0:
        raise argparse.ArgumentTypeError("0 or more")
    return value


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    p.add_argument("--url", required=True, help="gateway base URL, e.g. https://host")
    p.add_argument("--model", required=True, help="model id, as in GET /v1/models")
    p.add_argument(
        "--key-env",
        default="PLUGBOARD_BENCH_KEY",
        help="environment variable holding the client key (never printed)",
    )
    p.add_argument(
        "--allow-http", action="store_true", help="plain http to a remote host"
    )
    p.add_argument("--clients", type=_levels, default=[1, 2, 4, 8])
    p.add_argument("--rounds", type=_count, default=1, help="chats per client")
    p.add_argument("--warmup", type=_count, default=0, help="unmeasured chats first")
    p.add_argument("--max-tokens", type=_count, default=128)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--timeout", type=_positive, default=180.0, help="per chat, seconds")
    p.add_argument("--check", action="store_true", help="the PLU-17 criterion")
    p.add_argument("--max-wait-s", type=_positive, default=10.5)
    p.add_argument("--max-first-chunk-s", type=_positive, default=5.0)
    p.add_argument("--targets-at", type=_levels, default=[4], help="D-26 levels")
    p.add_argument("--first-word-p95-max", type=_positive, default=None)
    p.add_argument("--tps-min", type=_positive, default=None)
    p.add_argument("--json", default=None, help="write the full results there")
    args = p.parse_args(argv)
    if args.rounds < 1 or args.max_tokens < 1:
        p.error("--rounds and --max-tokens must be at least 1")
    return args


async def _bench(url: str, key: str, args: argparse.Namespace) -> List[Dict[str, Any]]:
    stamp = time.strftime("%H%M%S")
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        for w in range(1, args.warmup + 1):
            await one_chat(session, url, key, _body(args, f"warm-up {w}"), args.timeout)
        return [
            await run_level(session, url, key, args, n, stamp) for n in args.clients
        ]


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
    parts = urlsplit(url)
    if parts.scheme == "http" and not _is_loopback(parts.hostname or ""):
        if not args.allow_http:
            print(
                f"error: the key would go to {parts.hostname} over plain http; "
                "use https, or --allow-http",
                file=sys.stderr,
            )
            return 2
        print(
            f"warning: the key goes to {parts.hostname} over plain http",
            file=sys.stderr,
        )
    out: Optional[TextIO] = None
    try:
        if args.json:
            out = open(args.json, "w", encoding="utf-8")  # fail before the bench
        levels = asyncio.run(_bench(url, key, args))
        failures = check(levels, args)
        print_report(levels, failures, args)
        if out is not None:
            report = {
                "url": args.url,
                "model": args.model,
                "rounds": args.rounds,
                "max_tokens": args.max_tokens,
                "levels": levels,
                "check": {"passed": not failures, "failures": failures},
            }
            out.write(_scrub(json.dumps(report, indent=2), key) + "\n")
    except KeyboardInterrupt:
        print("interrupted: no report", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"error: the bench failed ({type(exc).__name__})", file=sys.stderr)
        return 3
    finally:
        if out is not None:
            out.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
