#!/usr/bin/env python3
"""Latency bench of a Plugboard gateway against the bare provider CLIs (INC-2).
Not run by the CI; it makes REAL inference calls (a short prompt each).

For each ``--target`` it measures, with 1 then 3 simultaneous calls (``--concurrency``):

* ``cli``          the bare CLI (claude, codex or agy), prompt on stdin: time to
                   its first byte of output and total time;
* ``gateway``      a chat through ``<url>/v1/chat/completions``, no stream: total;
* ``gateway-sse``  the same chat streamed: time to the first byte, time to the
                   first word (first ``delta`` with content) and total.

The gateway URL and the client key come from the environment
(``PLUGBOARD_BENCH_URL``, ``PLUGBOARD_BENCH_KEY``; names set with ``--url-env`` and
``--key-env``). The key is never printed nor written, and never sent over plain
http to another machine without ``--allow-http``. Each call asks for no cached
answer and has its own prompt.

A ``503`` refusal (``providers_busy``) is counted as ``busy``, not as a failure, except
at 1 call at a time. ``--check`` applies one limit to every target: run it once per
provider with that provider's limits.

A target is ``KIND=GATEWAY_MODEL=NATIVE_MODEL``, for example
``agy=antigravity-via-agy-cli:gemini-3.8-flash-low=gemini-3.8-flash-low``.
``--no-cli`` skips the bare CLI runs (a machine without the CLIs).

Output: a table on stdout and, with ``--json PATH``, the same figures plus every
call. Exit codes: 0 done, 1 a call failed or a ``--check`` limit was exceeded,
2 bad usage.

``--check`` compares the medians at 1 call with ``--first-word-max`` (gateway-sse,
seconds) and ``--call-max`` (gateway, seconds): the targets of
docs/guide/performance-targets.md.

Example:
  PLUGBOARD_BENCH_URL=https://gateway.example PLUGBOARD_BENCH_KEY=... \\
    python scripts/bench_latence.py --rounds 3 --concurrency 1,3 \\
      --target agy=antigravity-via-agy-cli:gemini-3.8-flash-low=gemini-3.8-flash-low \\
      --json latence.json --check
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

# Bare command of each CLI: the prompt goes to stdin, as Plugboard sends it.
BARE_ARGV = {
    "claude": ["claude", "-p", "--tools", "", "--model"],
    "codex": ["codex", "exec", "--skip-git-repo-check", "--model"],
    "agy": ["agy", "--model"],
}
BARE_STDIN_OPERAND = {"codex": ["-"]}
MODES = ("cli", "gateway", "gateway-sse")


def prompt() -> str:
    return f"Reply with exactly the single word PONG. ref={uuid.uuid4().hex[:8]}"


def stats(values: Sequence[Optional[float]]) -> Optional[Dict[str, float]]:
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return {
        "n": len(xs),
        "min": round(min(xs), 2),
        "med": round(statistics.median(xs), 2),
        "max": round(max(xs), 2),
    }


def _scrub(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text[:200]


def run_cli(kind: str, native: str, timeout: float) -> Dict[str, Any]:
    """One bare CLI call: time to its first stdout byte, total, exit code."""
    argv = [*BARE_ARGV[kind], native, *BARE_STDIN_OPERAND.get(kind, [])]
    exe = shutil.which(argv[0])
    if exe is None:
        return {"ok": False, "error": f"{argv[0]} not installed"}
    t0 = time.perf_counter()
    try:
        proc = subprocess.Popen(
            [exe, *argv[1:]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/tmp",
        )
    except OSError as exc:
        return {"ok": False, "error": repr(exc)[:120]}
    first: List[float] = []

    def watch() -> None:
        assert proc.stdout is not None
        if proc.stdout.read(1):
            first.append(time.perf_counter() - t0)
            proc.stdout.read()

    reader = threading.Thread(target=watch, daemon=True)
    reader.start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(prompt().encode())
        proc.stdin.close()
    except OSError:
        pass
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return {"ok": False, "error": "timeout", "total": round(timeout, 2)}
    reader.join(2)
    total = time.perf_counter() - t0
    return {
        "ok": code == 0,
        "error": None if code == 0 else f"exit {code}",
        "first_byte": round(first[0], 3) if first else None,
        "total": round(total, 3),
    }


def run_gateway(
    url: str, key: str, model: str, stream: bool, timeout: float
) -> Dict[str, Any]:
    """One chat through the gateway; ``stream`` also times first byte and first word."""
    parts = urlsplit(url)
    conn_cls = (
        http.client.HTTPSConnection
        if parts.scheme == "https"
        else http.client.HTTPConnection
    )
    conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt()}],
            "max_tokens": 256,
            "cache": False,
            "stream": stream,
        }
    )
    t0 = time.perf_counter()
    out: Dict[str, Any] = {"ok": False, "error": None}
    try:
        conn.request(
            "POST",
            (parts.path.rstrip("/") or "") + "/v1/chat/completions",
            body,
            {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        resp = conn.getresponse()
        out["status"] = resp.status
        if resp.status == 503:
            out["refused"] = True  # providers_busy: an answer, not a breakage
        if resp.status != 200:
            out["error"] = _scrub(resp.read().decode("utf-8", "replace"), [key])
            out["total"] = round(time.perf_counter() - t0, 3)
            return out
        if not stream:
            resp.read()
            out.update(ok=True, total=round(time.perf_counter() - t0, 3))
            return out
        first_byte = first_word = None
        words = 0
        done = False
        interrupted = False
        while True:
            line = resp.readline()
            if not line:
                break
            if first_byte is None:
                first_byte = time.perf_counter() - t0
            if line.startswith(b"data:") and b"[DONE]" in line:
                done = True
            elif line.startswith(b"data:"):
                try:
                    event = json.loads(line[5:])
                    if isinstance(event, dict) and event.get("error"):
                        interrupted = True  # stream_interrupted: not a success
                        continue
                    delta = event["choices"][0].get("delta") or {}
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                if delta.get("content") or delta.get("tool_calls"):
                    words += 1
                    if first_word is None:
                        first_word = time.perf_counter() - t0
        good = done and words > 0 and not interrupted
        out.update(
            ok=good,
            error=None
            if good
            else (
                "stream interrupted"
                if interrupted
                else "stream ended without a word or [DONE]"
            ),
            first_byte=round(first_byte, 3) if first_byte is not None else None,
            first_word=round(first_word, 3) if first_word is not None else None,
            total=round(time.perf_counter() - t0, 3),
        )
        return out
    except (OSError, http.client.HTTPException) as exc:
        out["error"] = _scrub(repr(exc), [key])
        out["total"] = round(time.perf_counter() - t0, 3)
        return out
    finally:
        conn.close()


def run_level(call, level: int) -> List[Dict[str, Any]]:
    """``level`` calls at once, started together."""
    results: List[Optional[Dict[str, Any]]] = [None] * level
    gate = threading.Barrier(level)

    def one(i: int) -> None:
        gate.wait()
        results[i] = call()

    threads = [threading.Thread(target=one, args=(i,)) for i in range(level)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [r for r in results if r is not None]


def summarize(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "calls": len(calls),
        "refused": sum(1 for c in calls if c.get("refused")),
        "failed": sum(1 for c in calls if not c.get("ok") and not c.get("refused")),
        "first_byte": stats([c.get("first_byte") for c in calls if c.get("ok")]),
        "first_word": stats([c.get("first_word") for c in calls if c.get("ok")]),
        "total": stats([c.get("total") for c in calls if c.get("ok")]),
        "errors": sorted({c["error"] for c in calls if c.get("error")})[:3],
    }


def render_table(rows: List[Dict[str, Any]]) -> str:
    head = (
        f"{'target':<28} {'mode':<12} {'n':>2} {'fail':>4} {'busy':>4} "
        f"{'1st byte':>9} {'1st word':>9} {'total':>7}"
    )
    lines = [head, "-" * len(head)]

    def cell(s: Optional[Dict[str, float]]) -> str:
        return f"{s['med']:.2f}" if s else "-"

    for r in rows:
        s = r["summary"]
        lines.append(
            f"{r['target'][:28]:<28} {r['mode']:<12} {r['concurrency']:>2} "
            f"{s['failed']:>4} {s['refused']:>4} {cell(s['first_byte']):>9} "
            f"{cell(s['first_word']):>9} {cell(s['total']):>7}"
        )
    return "\n".join(lines)


def _plain_http_allowed(url: str, allow_http: bool) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "https" or allow_http:
        return True
    host = parts.hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_target(spec: str) -> Dict[str, str]:
    kind, _, rest = spec.partition("=")
    gateway_model, _, native = rest.partition("=")
    if kind not in BARE_ARGV or not gateway_model or not native:
        raise ValueError(f"bad --target {spec!r}: KIND=GATEWAY_MODEL=NATIVE_MODEL")
    return {"kind": kind, "gateway_model": gateway_model, "native": native}


def _check(rows: List[Dict[str, Any]], args: argparse.Namespace) -> List[str]:
    problems: List[str] = []
    for r in rows:
        s = r["summary"]
        if r["concurrency"] != 1:
            continue
        word = s["first_word"]
        if r["mode"] == "gateway-sse" and word and word["med"] > args.first_word_max:
            problems.append(
                f"{r['target']} first word {word['med']}s > {args.first_word_max}s"
            )
        total = s["total"]
        if r["mode"] == "gateway" and total and total["med"] > args.call_max:
            problems.append(
                f"{r['target']} short call {total['med']}s > {args.call_max}s"
            )
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url-env", default="PLUGBOARD_BENCH_URL")
    ap.add_argument("--key-env", default="PLUGBOARD_BENCH_KEY")
    ap.add_argument("--target", action="append", default=[])
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--concurrency", default="1,3")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--no-cli", action="store_true", help="skip the bare CLI runs")
    ap.add_argument("--allow-http", action="store_true")
    ap.add_argument("--json", dest="json_path")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--first-word-max", type=float, default=8.0)
    ap.add_argument("--call-max", type=float, default=15.0)
    args = ap.parse_args(argv)

    url = os.environ.get(args.url_env, "")
    key = os.environ.get(args.key_env, "")
    try:
        targets = [parse_target(t) for t in args.target]
        levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if (
        not url
        or not key
        or not targets
        or args.rounds < 1
        or not levels
        or min(levels) < 1
    ):
        print(
            f"error: set {args.url_env} and {args.key_env}, give at least one --target, "
            "--rounds >= 1 and --concurrency levels >= 1",
            file=sys.stderr,
        )
        return 2
    if not _plain_http_allowed(url, args.allow_http):
        print(
            "error: plain http to another machine needs --allow-http", file=sys.stderr
        )
        return 2

    rows: List[Dict[str, Any]] = []
    all_calls: List[Dict[str, Any]] = []
    for target in targets:
        label = f"{target['kind']}:{target['native']}"
        for mode in MODES:
            if mode == "cli" and args.no_cli:
                continue
            for level in levels:
                calls: List[Dict[str, Any]] = []
                for _ in range(args.rounds):
                    if mode == "cli":

                        def call(t=target):
                            return run_cli(t["kind"], t["native"], args.timeout)

                    else:

                        def call(t=target, m=mode):
                            return run_gateway(
                                url,
                                key,
                                t["gateway_model"],
                                m == "gateway-sse",
                                args.timeout,
                            )

                    calls.extend(run_level(call, level))
                rows.append(
                    {
                        "target": label,
                        "mode": mode,
                        "concurrency": level,
                        "summary": summarize(calls),
                    }
                )
                all_calls.extend(
                    {"target": label, "mode": mode, "concurrency": level, **c}
                    for c in calls
                )

    print(render_table(rows))
    problems: List[str] = []
    failed = sum(
        r["summary"]["failed"]
        + (r["summary"]["refused"] if r["concurrency"] == 1 else 0)
        for r in rows
    )
    if failed:
        problems.append(f"{failed} call(s) failed")
    if args.check:
        problems.extend(_check(rows, args))
    for p in problems:
        print(f"FAIL: {p}", file=sys.stderr)
    if args.json_path:
        report = {
            "date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rounds": args.rounds,
            "concurrency": levels,
            "rows": rows,
            "calls": all_calls,
            "problems": problems,
        }
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, ensure_ascii=False)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
