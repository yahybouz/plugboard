#!/usr/bin/env python3
"""Recette « Ollama éteint » (PLU-53): read-only checks after a deployment.

Checks, in order, and stops at the first failure (exit 1):

  ready     GET /health/ready answers 200
  models    GET /v1/models lists no ``ollama:`` model
  chat      a chat to ``ollama:<model>`` is refused with 503 ``provider_disabled``
            or 404 ``model_not_found`` (never 200, 500 or 502); the refusal
            calls no model, so nothing is billed
  embed     POST /v1/embeddings to an ``ollama:`` identity is refused with 404
            or 503 (never 200, 500 or 502)

Environment: PLUGBOARD_URL (default http://127.0.0.1:8765) and
PLUGBOARD_SMOKE_KEY (a client key). The key is never printed. Standard
library only; redirects are not followed.
Exit status: 0 all passed, 1 a check failed, 2 usage error.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

URL = os.environ.get("PLUGBOARD_URL", "http://127.0.0.1:8765").rstrip("/")
KEY = os.environ.get("PLUGBOARD_SMOKE_KEY", "").strip()
MODEL = "ollama:recette-absent"
EMBEDDING = "ollama:recette-embedding"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = build_opener(ProxyHandler({}), _NoRedirect)


def call(method: str, path: str, body=None, timeout: float = 20.0):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {KEY}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw.decode("utf-8") or "null")
    except ValueError:
        return status, None


def _fail(name: str, why: str) -> int:
    print(f"FAIL {name}: {why.replace(KEY, '***') if KEY else why}")
    return 1


def run() -> int:
    status, _ = call("GET", "/health/ready")
    if status != 200:
        return _fail("ready", f"HTTP {status}")
    print("PASS ready")

    status, body = call("GET", "/v1/models")
    if status != 200 or not isinstance(body, dict):
        return _fail("models", f"HTTP {status}")
    leaked = [
        m.get("id")
        for m in body.get("data", [])
        if str(m.get("id", "")).lower().startswith("ollama:")
    ]
    if leaked:
        return _fail("models", f"ollama models listed: {', '.join(leaked[:5])}")
    print("PASS models: no ollama: model listed")

    status, body = call(
        "POST",
        "/v1/chat/completions",
        {
            "model": MODEL,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "cache": False,
        },
    )
    code = (body or {}).get("code") if isinstance(body, dict) else None
    if (status, code) not in {(503, "provider_disabled"), (404, "model_not_found")}:
        return _fail("chat", f"HTTP {status} code={code}, expected a clear refusal")
    print(f"PASS chat: refused, HTTP {status} {code}")

    status, body = call(
        "POST", "/v1/embeddings", {"model": EMBEDDING, "input": ["ping"]}
    )
    if status not in (404, 503):
        return _fail("embed", f"HTTP {status}, expected 404 or 503")
    print(f"PASS embed: refused, HTTP {status}")
    return 0


def main() -> int:
    if not KEY:
        print(
            "PLUGBOARD_SMOKE_KEY is empty: export a client key first.", file=sys.stderr
        )
        return 2
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in KEY):
        # Python's refusal of such a header value would print the key.
        print(
            "PLUGBOARD_SMOKE_KEY holds a space or a control character "
            "(value not shown).",
            file=sys.stderr,
        )
        return 2
    try:
        return run()
    except (URLError, OSError) as exc:
        return _fail("gateway", f"unreachable ({type(exc).__name__})")


if __name__ == "__main__":
    sys.exit(main())
