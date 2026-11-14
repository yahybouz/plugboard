#!/usr/bin/env python3
"""One-command proof that a client key reaches the models, by API and by MCP.

For every provider the key sees in ``GET /v1/models``, it picks one model and
makes a short real call:

  * ``api``         POST /v1/chat/completions, no stream
  * ``api-stream``  the same with ``stream: true`` (first byte measured)
  * ``mcp``         the ``chat`` tool of ``scripts/run_mcp.py`` over stdio

plus ``GET /v1/models`` and ``GET /v1/me`` once, the MCP ``list_models``
tool, and a check that the MCP catalogue holds exactly the API's model IDs.
It prints a table, writes the results as JSON to the file given as argument,
and exits non-zero when any surface fails.

Usage:
    PLUGBOARD_URL=https://gateway.example PLUGBOARD_KEY=pb_... \\
        python scripts/recette_acces.py results.json
    ... --model claude-code=claude-sonnet-4-6 --model Codex=gpt-5.6-luna

Environment:
    PLUGBOARD_URL      gateway base URL (required)
    PLUGBOARD_KEY      client key (required); never printed or written
    PLUGBOARD_MCP_PYTHON  interpreter that has the ``mcp`` package (default:
                       .venv-mcp, /opt/venv-mcp, else this interpreter)

Exit codes: 0 every surface passed; 1 a surface failed (or no provider is
visible); 2 bad usage or environment.

Only the standard library is used. At most 60 requests a minute are sent to
the gateway, the MCP server's own calls included. Each call costs a few
tokens (``--max-tokens``, default 16): it is a real inference.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
RATE_LIMIT_PER_MINUTE = 60
PROMPT = "Reply with the single word: pong"


# ── rate limit ──────────────────────────────────────────────────────────────


class RateLimiter:
    """Sliding window: at most ``limit`` requests in any 60 seconds.

    ``clock`` and ``sleep`` are injectable so a test needs no real waiting.
    """

    def __init__(
        self,
        limit: int = RATE_LIMIT_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.limit = limit
        self.clock = clock
        self.sleep = sleep
        self._stamps: collections.deque = collections.deque()

    def acquire(self, n: int = 1) -> None:
        for _ in range(n):
            while True:
                now = self.clock()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                if len(self._stamps) < self.limit:
                    self._stamps.append(now)
                    break
                self.sleep(max(0.05, 60.0 - (now - self._stamps[0])))


# ── results ─────────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self, key: str):
        self.key = key
        self.rows: List[Dict[str, Any]] = []

    def clean(self, text: str) -> str:
        text = str(text)
        if self.key:
            text = text.replace(self.key, "***")
        return re.sub(r"pb_[A-Za-z0-9_\-]{8,}", "pb_***", text)

    def add(
        self,
        provider: str,
        model: str,
        surface: str,
        ok: bool,
        started: float,
        first_byte: Optional[float] = None,
        detail: str = "",
    ) -> bool:
        self.rows.append(
            {
                "provider": provider,
                "model": model,
                "surface": surface,
                "status": "OK" if ok else "FAIL",
                "duration_s": round(time.monotonic() - started, 3),
                "first_byte_s": None if first_byte is None else round(first_byte, 3),
                "detail": self.clean(detail)[:300],
            }
        )
        return ok


def render_table(rows: List[Dict[str, Any]]) -> str:
    head = ("provider", "model", "surface", "status", "duration", "1st byte", "detail")
    body = [
        (
            r["provider"],
            r["model"],
            r["surface"],
            r["status"],
            f"{r['duration_s']:.2f}s",
            "-" if r["first_byte_s"] is None else f"{r['first_byte_s']:.2f}s",
            r["detail"][:60],
        )
        for r in rows
    ]
    widths = [max(len(str(x[i])) for x in [head, *body]) for i in range(len(head))]
    lines = [
        "  ".join(str(c).ljust(w) for c, w in zip(row, widths)) for row in [head, *body]
    ]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


# ── HTTP ────────────────────────────────────────────────────────────────────


class Gateway:
    def __init__(self, base: str, key: str, limiter: RateLimiter, timeout: float):
        self.base = base.rstrip("/")
        self.key = key
        self.limiter = limiter
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[dict]):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("User-Agent", "plugboard-recette-acces/1")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        self.limiter.acquire()
        return urllib.request.urlopen(req, timeout=self.timeout)

    @staticmethod
    def error_text(exc: urllib.error.HTTPError) -> str:
        """``<code>: <detail>`` like the MCP tools, else ``HTTP <status>``."""
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
            detail = payload.get("detail", raw) if isinstance(payload, dict) else raw
            code = payload.get("code") if isinstance(payload, dict) else None
        except ValueError:
            detail, code = raw, None
        if not isinstance(detail, str):
            detail = json.dumps(detail)
        if code:
            return f"{code}: {detail[:240]}"
        return f"HTTP {exc.code}: {detail[:240]}"

    def get_json(self, path: str) -> Tuple[Optional[dict], str]:
        try:
            with self._request("GET", path, None) as resp:
                return json.loads(resp.read().decode("utf-8")), ""
        except urllib.error.HTTPError as exc:
            return None, self.error_text(exc)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def chat(self, model: str, max_tokens: int) -> Tuple[bool, str]:
        body = _chat_body(model, max_tokens, stream=False)
        try:
            with self._request("POST", "/v1/chat/completions", body) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return False, self.error_text(exc)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        choices = payload.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
        if not content:
            return False, "empty answer (no content)"
        return True, ""

    def chat_stream(self, model: str, max_tokens: int, t0: float):
        """(ok, detail, first_byte_seconds)."""
        body = _chat_body(model, max_tokens, stream=True)
        first: Optional[float] = None
        content = False
        error = ""
        done = False
        try:
            with self._request("POST", "/v1/chat/completions", body) as resp:
                for raw in resp:
                    if first is None:
                        first = time.monotonic() - t0
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                        continue
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(data.get("error"), dict):
                        err = data["error"]
                        error = (
                            f"{err.get('code') or 'stream_error'}: "
                            f"{err.get('message', '')}"
                        )
                        continue
                    for choice in data.get("choices") or []:
                        if (choice.get("delta") or {}).get("content"):
                            content = True
        except urllib.error.HTTPError as exc:
            return False, self.error_text(exc), first
        except (urllib.error.URLError, OSError) as exc:
            return False, f"{type(exc).__name__}: {exc}", first
        if error:
            return False, error, first
        if not content:
            return False, "empty stream (no content)", first
        if not done:
            return False, "stream ended without [DONE]", first
        return True, "", first


def _chat_body(model: str, max_tokens: int, stream: bool) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "stream": stream,
        "cache": False,
    }


# ── MCP over stdio (JSON-RPC, newline-delimited) ────────────────────────────


class McpStdio:
    """A minimal MCP client: initialize, then ``tools/call``."""

    def __init__(self, python: str, url: str, key: str, timeout: float):
        env = {
            "PLUGBOARD_URL": url,
            "PLUGBOARD_API_KEY": key,
            "PLUGBOARD_SKIP_DOTENV": "1",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        self.timeout = timeout
        self.proc = subprocess.Popen(
            [python, str(ROOT / "scripts" / "run_mcp.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            bufsize=1,
        )
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next_id = 0

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def _call(self, method: str, params: dict) -> dict:
        self._next_id += 1
        want = self._next_id
        self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"MCP {method}: no answer in {self.timeout:.0f}s")
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError("MCP server exited")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == want:
                if "error" in msg:
                    raise RuntimeError(f"MCP error: {msg['error'].get('message')}")
                return msg.get("result") or {}

    def start(self) -> None:
        self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "plugboard-recette-acces", "version": "1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool(self, name: str, arguments: dict) -> Tuple[bool, str]:
        result = self._call("tools/call", {"name": name, "arguments": arguments})
        text = "".join(
            c.get("text", "")
            for c in result.get("content", [])
            if c.get("type") == "text"
        )
        return (not result.get("isError", False)), text

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def mcp_model_ids(text: str) -> List[str]:
    return re.findall(r"^\s*- `([^`]+)`", text, flags=re.MULTILINE)


def default_mcp_python() -> str:
    explicit = os.environ.get("PLUGBOARD_MCP_PYTHON")
    if explicit:
        return explicit
    for candidate in (
        ROOT / ".venv-mcp" / "bin" / "python",
        Path("/opt/venv-mcp/bin/python"),
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


# ── the run ─────────────────────────────────────────────────────────────────


def pick_models(catalogue: List[dict], wanted: Dict[str, str]) -> List[Tuple[str, str]]:
    """One (provider, model ID) per provider the key sees.

    A client key cannot read a provider's default model, so without
    ``--model`` the choice is the first model the catalogue lists as
    connected, else the first one listed.
    """
    by_provider: Dict[str, List[dict]] = {}
    for item in catalogue:
        owner = item.get("owned_by") or str(item.get("id", "")).split(":", 1)[0]
        by_provider.setdefault(owner, []).append(item)
    picks: List[Tuple[str, str]] = []
    for provider, items in by_provider.items():
        ids = [i["id"] for i in items]
        want = wanted.get(provider)
        if want:
            full = want if want in ids else f"{provider}:{want}"
            picks.append((provider, full))
            continue
        connected = [i["id"] for i in items if i.get("connected") is True]
        picks.append((provider, (connected or ids)[0]))
    return picks


def run(
    url: str,
    key: str,
    out_path: str,
    wanted: Dict[str, str],
    max_tokens: int,
    timeout: float,
    mcp_python: Optional[str],
    skip_mcp: bool,
    limiter: Optional[RateLimiter] = None,
    out=None,
) -> int:
    out = out or sys.stdout
    rec = Recorder(key)
    limiter = limiter or RateLimiter()
    gw = Gateway(url, key, limiter, timeout)

    t0 = time.monotonic()
    payload, err = gw.get_json("/v1/models")
    api_ids: List[str] = []
    catalogue: List[dict] = []
    if payload is not None:
        catalogue = [m for m in payload.get("data", []) if isinstance(m, dict)]
        api_ids = [m.get("id", "") for m in catalogue]
    rec.add(
        "-",
        f"{len(api_ids)} models",
        "api-models",
        payload is not None and bool(api_ids),
        t0,
        detail=err or ("" if api_ids else "the key sees no model"),
    )
    t0 = time.monotonic()
    me, err = gw.get_json("/v1/me")
    rec.add("-", "-", "api-me", me is not None, t0, detail=err)

    picks = pick_models(catalogue, wanted)
    for provider, model in picks:
        t0 = time.monotonic()
        ok, detail = gw.chat(model, max_tokens)
        rec.add(provider, model, "api", ok, t0, detail=detail)
        t0 = time.monotonic()
        ok, detail, first = gw.chat_stream(model, max_tokens, t0)
        rec.add(provider, model, "api-stream", ok, t0, first_byte=first, detail=detail)

    if not skip_mcp:
        _run_mcp(
            rec, url, key, picks, api_ids, max_tokens, timeout, mcp_python, limiter
        )

    failed = [r for r in rec.rows if r["status"] != "OK"]
    summary = {
        "ok": not failed and bool(picks),
        "surfaces": len(rec.rows),
        "failed": len(failed),
        "providers": [p for p, _ in picks],
    }
    result = {
        "url": url,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": summary,
        "results": rec.rows,
    }
    Path(out_path).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(render_table(rec.rows), file=out)
    print(
        f"\n{summary['surfaces'] - summary['failed']}/{summary['surfaces']} surfaces OK"
        f" · providers: {', '.join(summary['providers']) or 'none'} · {out_path}",
        file=out,
    )
    return 0 if summary["ok"] else 1


def _run_mcp(rec, url, key, picks, api_ids, max_tokens, timeout, mcp_python, limiter):
    python = mcp_python or default_mcp_python()
    t0 = time.monotonic()
    try:
        client = McpStdio(python, url, key, timeout)
        client.start()
    except Exception as exc:  # noqa: BLE001 - reported as the surface's failure
        rec.add("-", "-", "mcp-start", False, t0, detail=f"{type(exc).__name__}: {exc}")
        return
    try:
        rec.add("-", "-", "mcp-start", True, t0)
        t0 = time.monotonic()
        limiter.acquire()
        try:
            ok, text = client.tool("list_models", {})
        except Exception as exc:  # noqa: BLE001
            ok, text = False, f"{type(exc).__name__}: {exc}"
        ids = mcp_model_ids(text) if ok else []
        ok = ok and not text.startswith("Error:")
        rec.add(
            "-", f"{len(ids)} models", "mcp-list", ok, t0, detail="" if ok else text
        )
        t0 = time.monotonic()
        same = sorted(ids) == sorted(api_ids)
        only_api = sorted(set(api_ids) - set(ids))[:5]
        only_mcp = sorted(set(ids) - set(api_ids))[:5]
        rec.add(
            "-",
            "-",
            "mcp-same-ids",
            same,
            t0,
            detail="" if same else f"api only: {only_api} mcp only: {only_mcp}",
        )
        for provider, model in picks:
            t0 = time.monotonic()
            limiter.acquire()
            try:
                ok, text = client.tool(
                    "chat",
                    {"model": model, "message": PROMPT, "max_tokens": max_tokens},
                )
            except Exception as exc:  # noqa: BLE001
                ok, text = False, f"{type(exc).__name__}: {exc}"
            ok = ok and not text.startswith("Error:") and bool(text.strip())
            rec.add(
                provider,
                model,
                "mcp",
                ok,
                t0,
                detail="" if ok else text.removeprefix("Error: "),
            )
    finally:
        client.close()


def parse_models(values: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for value in values:
        provider, sep, model = value.partition("=")
        if not sep or not provider or not model:
            raise ValueError(f"--model wants provider=model, got {value!r}")
        out[provider] = model
    return out


def main(argv: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("output", help="file that receives the JSON results")
    ap.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="PROVIDER=MODEL",
        help="model to call for a provider (repeatable); default: one the "
        "catalogue lists",
    )
    ap.add_argument("--max-tokens", type=int, default=16)
    ap.add_argument("--timeout", type=float, default=180.0, help="seconds per call")
    ap.add_argument(
        "--mcp-python", default=None, help="interpreter with the mcp package"
    )
    ap.add_argument("--skip-mcp", action="store_true")
    args = ap.parse_args(argv)

    url = env.get("PLUGBOARD_URL", "").strip()
    key = env.get("PLUGBOARD_KEY", "").strip()
    if not url or not key:
        print(
            "PLUGBOARD_URL and PLUGBOARD_KEY must be set in the environment.",
            file=sys.stderr,
        )
        return 2
    try:
        wanted = parse_models(args.model)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return run(
        url,
        key,
        args.output,
        wanted,
        args.max_tokens,
        args.timeout,
        args.mcp_python,
        args.skip_mcp,
    )


if __name__ == "__main__":
    sys.exit(main())
