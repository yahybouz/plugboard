#!/usr/bin/env python3
"""Post-deployment smoke test with a limited client key (PLU-18).

Checks what the consumer applications use, in this order, within one time
budget (60 s by default):

  health      GET /health answers ok (and the expected version, if given);
              an instance with client auth disabled fails
  models      GET /v1/models lists the smoke model for the key
  chat        one small chat completion returns text
  stream      the same call streamed returns text and ends with [DONE]
  structured  response_format json_schema returns the requested JSON object
  embeddings  only when the key may use a declared embedding model
              (GET /v1/embeddings/models): one vector is returned. No
              embedding model is a SKIP, not a failure, unless
              --require-embeddings is given.

Read-only: no admin route, no provider or key change, no quarantine write.
Chat calls send ``"cache": false`` (a cached answer proves nothing) and a
small ``max_tokens``; they are billed to the smoke key like any client call,
so give it a dedicated client key limited to a local model (allowed_models).

The key is read from PLUGBOARD_SMOKE_KEY and never printed: any echo of it in
an error body is masked. Standard library only, so it runs on a host without
the project's virtualenv, or inside the container.

Usage:
  PLUGBOARD_SMOKE_KEY=... PLUGBOARD_SMOKE_MODEL=ollama:<model> \\
    python3 scripts/smoke_consumers.py --url http://127.0.0.1:8765

Exit status: 0 every check passed (SKIP allowed), 1 a check failed or the
time budget ran out, 2 usage error (missing key or model).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

KEY_ENV = "PLUGBOARD_SMOKE_KEY"
MODEL_ENV = "PLUGBOARD_SMOKE_MODEL"
EMBEDDING_ENV = "PLUGBOARD_SMOKE_EMBEDDING_MODEL"

SCHEMA = {
    "name": "smoke_answer",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    },
}


class CheckFailed(Exception):
    """A check did not pass; the message is safe to print."""


class Skipped(Exception):
    """A check does not apply to this deployment; the message says why."""


class Smoke:
    def __init__(self, url: str, key: str, timeout: float, deadline: float):
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.started = time.monotonic()
        self.deadline = deadline
        self.failed = False

    # ── output ──────────────────────────────────────────────────────
    def mask(self, text: str) -> str:
        text = str(text)
        if self.key:
            text = text.replace(self.key, "***")
        return " ".join(text.split())[:300]

    def say(self, status: str, name: str, message: str) -> None:
        print(f"{status} {name}: {self.mask(message)}", flush=True)

    # ── HTTP ────────────────────────────────────────────────────────
    def remaining(self) -> float:
        return self.deadline - (time.monotonic() - self.started)

    def _request(self, method: str, path: str, body: Any = None, auth: bool = True):
        left = self.remaining()
        if left <= 0:
            raise CheckFailed(
                f"time budget of {self.deadline:.0f} s (deadline) exceeded"
            )
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.key}"
        req = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            return urlopen(req, timeout=min(self.timeout, left))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(2000).decode("utf-8", "replace")
            except OSError:
                pass
            raise CheckFailed(f"HTTP {exc.code} on {path}: {detail}") from None
        except (URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            if self.remaining() <= 0:
                raise CheckFailed(
                    f"{path}: time budget of {self.deadline:.0f} s (deadline) exceeded"
                ) from None
            raise CheckFailed(f"{path} unreachable: {reason}") from None

    def get_json(self, path: str, auth: bool = True) -> Any:
        with self._request("GET", path, auth=auth) as resp:
            return self._json(resp, path)

    def post_json(self, path: str, body: dict) -> Any:
        with self._request("POST", path, body) as resp:
            return self._json(resp, path)

    def _json(self, resp, path: str) -> Any:
        try:
            return json.loads(resp.read().decode("utf-8"))
        except (ValueError, OSError) as exc:
            raise CheckFailed(f"{path}: invalid JSON answer ({exc})") from None

    # ── runner ──────────────────────────────────────────────────────
    def run(self, name: str, check: Callable[[], str]) -> None:
        t0 = time.monotonic()
        try:
            message = check()
        except Skipped as exc:
            self.say("SKIP", name, str(exc))
            return
        except CheckFailed as exc:
            self.failed = True
            self.say("FAIL", name, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a smoke must report, not crash
            self.failed = True
            self.say("FAIL", name, f"{type(exc).__name__}: {exc}")
            return
        self.say("PASS", name, f"{message} ({time.monotonic() - t0:.2f} s)")


def _chat_body(model: str, prompt: str, max_tokens: int, **extra) -> dict:
    # A nonce keeps two runs from sharing a cached answer upstream.
    nonce = uuid.uuid4().hex[:8]
    return {
        "model": model,
        "messages": [{"role": "user", "content": f"{prompt} (smoke {nonce})"}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "cache": False,
        **extra,
    }


def _content(answer: Any) -> tuple[str, str]:
    try:
        choice = answer["choices"][0]
        content = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        raise CheckFailed("answer without choices[0].message") from None
    if not isinstance(content, str):
        raise CheckFailed(f"content is {type(content).__name__}, not text")
    return content, str(choice.get("finish_reason") or "")


def _empty(finish: str) -> CheckFailed:
    hint = (
        " (raise --max-tokens: a reasoning model may spend them all)"
        if finish == "length"
        else ""
    )
    return CheckFailed(f"empty content, finish_reason={finish or 'none'}{hint}")


def build_checks(smoke: Smoke, args) -> list[tuple[str, Callable[[], str]]]:
    model = args.model

    def health() -> str:
        payload = smoke.get_json("/health", auth=False)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise CheckFailed(f"/health is not ok: {json.dumps(payload)[:200]}")
        if payload.get("auth_disabled"):
            raise CheckFailed(
                "client auth is disabled on this instance (API_KEY empty)"
            )
        version = str(payload.get("version", "?"))
        if args.expect_version and version != args.expect_version:
            raise CheckFailed(f"version {version}, expected {args.expect_version}")
        return f"version {version}, {payload.get('providers', '?')} active provider(s)"

    def models() -> str:
        payload = smoke.get_json("/v1/models")
        ids = [
            m.get("id") for m in (payload or {}).get("data", []) if isinstance(m, dict)
        ]
        if model not in ids:
            raise CheckFailed(
                f"{model} is not listed for this key ({len(ids)} model(s))"
            )
        try:
            me = smoke.get_json("/v1/me")
        except CheckFailed:
            me = None
        if isinstance(me, dict) and "allowed_models" in me and not me["allowed_models"]:
            smoke.say(
                "WARN", "models", "the smoke key is not limited (allowed_models empty)"
            )
        return f"{model} listed among {len(ids)} model(s)"

    def chat() -> str:
        answer = smoke.post_json(
            "/v1/chat/completions",
            _chat_body(model, "Reply with the single word: pong.", args.max_tokens),
        )
        content, finish = _content(answer)
        if not content.strip():
            raise _empty(finish)
        return f"{len(content)} character(s), finish_reason={finish or 'none'}"

    def stream() -> str:
        body = _chat_body(
            model, "Reply with the single word: pong.", args.max_tokens, stream=True
        )
        text, chunks, done = [], 0, False
        with smoke._request("POST", "/v1/chat/completions", body) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" not in ctype:
                raise CheckFailed(
                    f"Content-Type {ctype or 'missing'}, not text/event-stream"
                )
            while True:
                if smoke.remaining() <= 0:
                    raise CheckFailed(
                        f"time budget of {smoke.deadline:.0f} s (deadline) exceeded"
                    )
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                try:
                    event = json.loads(data)
                except ValueError:
                    raise CheckFailed(f"invalid stream event: {data[:120]}") from None
                if isinstance(event, dict) and event.get("error"):
                    raise CheckFailed(
                        f"error event in stream: {json.dumps(event['error'])[:200]}"
                    )
                chunks += 1
                for choice in (
                    (event.get("choices") or []) if isinstance(event, dict) else []
                ):
                    piece = (choice.get("delta") or {}).get("content")
                    if isinstance(piece, str):
                        text.append(piece)
        if not done:
            raise CheckFailed("stream ended without data: [DONE]")
        if not "".join(text).strip():
            raise CheckFailed(f"no text in {chunks} chunk(s)")
        return f"{chunks} chunk(s), {len(''.join(text))} character(s), [DONE] received"

    def structured() -> str:
        answer = smoke.post_json(
            "/v1/chat/completions",
            _chat_body(
                model,
                'Return a JSON object whose "answer" field is the word pong.',
                args.max_tokens,
                response_format={"type": "json_schema", "json_schema": SCHEMA},
            ),
        )
        content, finish = _content(answer)
        if not content.strip():
            raise _empty(finish)
        try:
            parsed = json.loads(content.strip())
        except ValueError:
            raise CheckFailed(f"content is not JSON: {content[:80]!r}") from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("answer"), str):
            raise CheckFailed(
                "JSON does not match the schema (string field 'answer' required)"
            )
        return "JSON object matching the schema"

    def embeddings() -> str:
        try:
            listed = smoke.get_json("/v1/embeddings/models")
        except CheckFailed as exc:
            if "HTTP 404" in str(exc) and not args.require_embeddings:
                raise Skipped("this version has no /v1/embeddings route") from None
            raise
        rows = [r for r in (listed or {}).get("data", []) if isinstance(r, dict)]
        if args.embedding_model:
            rows = [r for r in rows if r.get("id") == args.embedding_model]
        available = [r["id"] for r in rows if r.get("available") and r.get("id")]
        if not available:
            why = (
                f"{args.embedding_model} is not usable by this key"
                if args.embedding_model
                else "no embedding model declared for this key"
            )
            if rows:
                why = f"declared but unavailable: {', '.join(str(r.get('id')) for r in rows)}"
            if args.require_embeddings:
                raise CheckFailed(why)
            raise Skipped(why)
        target = available[0]
        payload = smoke.post_json(
            "/v1/embeddings", {"model": target, "input": ["smoke"]}
        )
        try:
            vector = payload["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            raise CheckFailed("answer without data[0].embedding") from None
        if not isinstance(vector, list) or not vector:
            raise CheckFailed("empty embedding vector")
        if not all(isinstance(v, (int, float)) for v in vector):
            raise CheckFailed("embedding vector holds non-numbers")
        return f"{target}: {len(vector)} dimension(s)"

    return [
        ("health", health),
        ("models", models),
        ("chat", chat),
        ("stream", stream),
        ("structured", structured),
        ("embeddings", embeddings),
    ]


def parse_args(argv: Optional[list] = None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--url",
        default=os.environ.get("PLUGBOARD_URL", "http://127.0.0.1:8765"),
        help="gateway base URL (default: PLUGBOARD_URL or http://127.0.0.1:8765)",
    )
    p.add_argument(
        "--model",
        default=os.environ.get(MODEL_ENV, ""),
        help=f"chat model the key may use, as /v1/models lists it (default: {MODEL_ENV})",
    )
    p.add_argument(
        "--embedding-model",
        default=os.environ.get(EMBEDDING_ENV, ""),
        help=f"embedding model to check (default: {EMBEDDING_ENV}, else the first available)",
    )
    p.add_argument(
        "--require-embeddings",
        action="store_true",
        help="fail instead of skipping when no embedding model is usable",
    )
    p.add_argument(
        "--expect-version", default="", help="fail unless /health reports it"
    )
    p.add_argument(
        "--max-tokens", type=int, default=64, help="per chat call (default 64)"
    )
    p.add_argument("--timeout", type=float, default=30.0, help="per request, seconds")
    p.add_argument(
        "--deadline", type=float, default=60.0, help="whole run, seconds (default 60)"
    )
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        print(
            f"{KEY_ENV} is empty: export the limited smoke key first.", file=sys.stderr
        )
        return 2
    if not args.model:
        print(f"No model: pass --model or set {MODEL_ENV}.", file=sys.stderr)
        return 2
    smoke = Smoke(args.url, key, args.timeout, args.deadline)
    print(
        f"smoke {smoke.url} model={args.model} budget={args.deadline:.0f} s", flush=True
    )
    for name, check in build_checks(smoke, args):
        smoke.run(name, check)
    elapsed = time.monotonic() - smoke.started
    if elapsed > args.deadline:
        smoke.failed = True
        print(f"FAIL deadline: {elapsed:.1f} s > {args.deadline:.0f} s", flush=True)
    verdict = "FAILED" if smoke.failed else "OK"
    print(f"smoke {verdict} in {elapsed:.1f} s", flush=True)
    return 1 if smoke.failed else 0


if __name__ == "__main__":
    sys.exit(main())
