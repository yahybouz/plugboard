#!/usr/bin/env python3
"""Post-deployment smoke test with a limited client key (PLU-18).

Checks what the consumer applications use, in this order, within one time
budget (60 s by default):

  health      GET /health answers ok (and the expected version, if given);
              an instance with client auth disabled fails
  models      GET /v1/models lists the smoke model as allowed for the key,
              and the key is a limited client key (GET /v1/me)
  chat        one small chat completion returns text
  stream      the same call streamed returns text and ends with [DONE]
  structured  response_format json_schema: the answer is exactly the JSON
              object of a schema whose value is a nonce, which the prompt
              does not give (a gateway that drops response_format fails).
              A model only CLI subscription routes serve is a SKIP: they
              refuse response_format (400 unsupported_on_cli, nothing called)
  embeddings  when the key may use a declared embedding model
              (GET /v1/embeddings/models): it is available and one vector is
              returned. No embedding model declared for the key is a SKIP,
              not a failure, unless --require-embeddings is given.

Read-only: no admin route, no provider or key change, no quarantine write.
Chat calls send ``"cache": false`` (a cached answer proves nothing) and a
small ``max_tokens``; they are billed to the smoke key like any client call,
so give it a dedicated client key limited (allowed_models) to one chat
model and, where embeddings are served, to the identity listed in
PLUGBOARD_EMBEDDING_MODELS. Redirects are never followed and no proxy is
used, so the key only goes to --url.

The key is read from PLUGBOARD_SMOKE_KEY and never printed: any echo of it in
an error body is masked. Standard library only, so it runs on a host without
the project's virtualenv, or inside the container.

Usage:
  PLUGBOARD_SMOKE_KEY=... python3 scripts/smoke_consumers.py --url http://127.0.0.1:8765
  # Without --model / PLUGBOARD_SMOKE_MODEL the first model of a CLI provider
  # (Codex:, claude-code:, Gemini:, Antigravity:) that the key may use is chosen;
  # an ollama: model never is. Name one to check another: PLUGBOARD_SMOKE_MODEL=Codex:<model>

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
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

KEY_ENV = "PLUGBOARD_SMOKE_KEY"
MODEL_ENV = "PLUGBOARD_SMOKE_MODEL"
EMBEDDING_ENV = "PLUGBOARD_SMOKE_EMBEDDING_MODEL"
# Default chat model: a CLI provider's, in this order. Ollama is off by default.
CLI_PREFIXES = ("codex:", "claude-code:", "gemini:", "antigravity:", "agy:")


def pick_default_model(rows: list, allowed: Optional[list]) -> str:
    """First model of a CLI provider the key may use, else ''."""
    for prefix in CLI_PREFIXES:
        for row in rows:
            mid = str(row.get("id") or "")
            if not mid.lower().startswith(prefix):
                continue
            if row.get("account_compatible") is False:
                continue
            # 4.4.0 flags what the key may use; only an older list needs allowed_models.
            if "account_compatible" not in row and allowed and mid not in allowed:
                continue
            return mid
    return ""


def _cli_refuses_response_format(failure: "CheckFailed") -> bool:
    """The gateway's documented refusal of a structured output on a model that
    only CLI subscription routes serve: 400, code ``unsupported_on_cli``,
    ``response_format`` the only refused field."""
    if failure.status != 400:
        return False
    try:
        payload = json.loads(failure.body)
    except ValueError:
        return False
    return (
        isinstance(payload, dict)
        and payload.get("code") == "unsupported_on_cli"
        and payload.get("unsupported") == ["response_format"]
    )


def structured_schema(word: str) -> dict:
    """A schema only the gateway can tell the model: its value is a nonce.

    The prompt never mentions JSON nor the field, so a gateway that drops
    ``response_format`` gets plain text back and the check fails.
    """
    return {
        "name": "smoke_answer",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string", "enum": [word]}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    }


class CheckFailed(Exception):
    """A check did not pass; the message is safe to print.

    ``status`` and ``body`` hold the HTTP answer when the gateway refused.
    """

    status: Optional[int] = None
    body: str = ""


class Skipped(Exception):
    """A check does not apply to this deployment; the message says why."""


class _NoRedirect(HTTPRedirectHandler):
    """Never follow a redirect: it would send the key to another place."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CheckFailed(
            f"HTTP {code} redirect to {newurl}: not followed (the key stays here)"
        )


class Smoke:
    def __init__(self, url: str, key: str, timeout: float, deadline: float):
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.started = time.monotonic()
        self.deadline = deadline
        self.failed = False
        # No proxy (the key would cross it) and no redirect (see _NoRedirect).
        self._opener = build_opener(ProxyHandler({}), _NoRedirect)

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
            return self._opener.open(req, timeout=min(self.timeout, left))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(2000).decode("utf-8", "replace")
            except OSError:
                pass
            failure = CheckFailed(f"HTTP {exc.code} on {path}: {detail}")
            failure.status, failure.body = exc.code, detail
            raise failure from None
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
        version = str(payload.get("version", "?"))
        if args.expect_version and version != args.expect_version:
            raise CheckFailed(f"version {version}, expected {args.expect_version}")
        # Since 4.12.0 /health shows only the status and the version; whether
        # the instance can serve (client auth on, usage writable, CLIs present)
        # is on /health/ready, which answers 503 when it cannot.
        try:
            ready = smoke.get_json("/health/ready", auth=False)
        except CheckFailed as exc:
            if "HTTP 404" not in str(exc):
                raise
            # A gateway before 4.12.0: read the field its /health still had.
            if payload.get("auth_disabled"):
                raise CheckFailed(
                    "client auth is disabled on this instance (API_KEY empty)"
                ) from None
            return f"version {version} (no /health/ready before 4.12.0)"
        if not isinstance(ready, dict) or ready.get("ready") is not True:
            raise CheckFailed(f"/health/ready is not ready: {json.dumps(ready)[:200]}")
        return f"version {version}, ready"

    def models() -> str:
        payload = smoke.get_json("/v1/models")
        rows = [m for m in (payload or {}).get("data", []) if isinstance(m, dict)]
        row = next((m for m in rows if m.get("id") == model), None)
        if row is None:
            raise CheckFailed(
                f"{model} is not listed for this key ({len(rows)} model(s))"
            )
        # 4.4.0 lists every model and flags the ones the key may not use.
        if row.get("account_compatible") is False:
            raise CheckFailed(f"{model} is listed but not allowed for this key")
        me = smoke.get_json("/v1/me")
        if not isinstance(me, dict) or me.get("client_id") in (None, "admin"):
            raise CheckFailed("not a client key: use a dedicated, limited client key")
        if not me.get("allowed_models"):
            message = "the smoke key is not limited (allowed_models empty)"
            if not args.allow_unlimited_key:
                raise CheckFailed(f"{message}; pass --allow-unlimited-key to accept it")
            smoke.say("WARN", "models", message)
        return f"{model} listed among {len(rows)} model(s), key limited"

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
        word = f"pong{uuid.uuid4().hex[:6]}"
        try:
            answer = smoke.post_json(
                "/v1/chat/completions",
                _chat_body(
                    model,
                    "Say pong.",
                    args.max_tokens,
                    response_format={
                        "type": "json_schema",
                        "json_schema": structured_schema(word),
                    },
                ),
            )
        except CheckFailed as exc:
            if _cli_refuses_response_format(exc):
                raise Skipped(
                    "the model is served by CLI subscription routes only, which "
                    "take no response_format (400 unsupported_on_cli, nothing called)"
                ) from None
            raise
        content, finish = _content(answer)
        if not content.strip():
            raise _empty(finish)
        try:
            parsed = json.loads(content.strip())
        except ValueError:
            raise CheckFailed(
                f"content is not JSON (response_format ignored?): {content[:80]!r}"
            ) from None
        if parsed != {"answer": word}:
            raise CheckFailed(
                "JSON does not match the schema sent (response_format ignored?)"
            )
        return "JSON object matching the schema sent"

    def embeddings() -> str:
        # The route exists since 4.4.0: a 404 is a routing fault, not a version.
        listed = smoke.get_json("/v1/embeddings/models")
        rows = [r for r in (listed or {}).get("data", []) if isinstance(r, dict)]
        if args.embedding_model:
            rows = [r for r in rows if r.get("id") == args.embedding_model]
            if not rows:
                raise CheckFailed(
                    f"{args.embedding_model} is not declared for this key "
                    "(PLUGBOARD_EMBEDDING_MODELS and the key's allowed_models)"
                )
        if not rows:
            why = (
                "no embedding provider configured for this key "
                "(no embedding model declared)"
            )
            if args.require_embeddings:
                raise CheckFailed(why)
            raise Skipped(why)
        broken = [str(r.get("id")) for r in rows if not r.get("available")]
        if broken:
            raise CheckFailed(
                f"declared for this key but unavailable: {', '.join(broken)}"
            )
        target = str(rows[0]["id"])
        payload = smoke.post_json(
            "/v1/embeddings", {"model": target, "input": ["smoke"]}
        )
        try:
            vector = payload["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            raise CheckFailed("answer without data[0].embedding") from None
        if not isinstance(vector, list) or not vector:
            raise CheckFailed("empty embedding vector")
        if not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in vector
        ):
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
        help="fail instead of skipping when no embedding model is declared for the key",
    )
    p.add_argument(
        "--allow-unlimited-key",
        action="store_true",
        help="accept a client key without allowed_models (a warning instead of a failure)",
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
    smoke = Smoke(args.url, key, args.timeout, args.deadline)
    if not args.model:
        try:
            listed = smoke.get_json("/v1/models")
            me = smoke.get_json("/v1/me")
            data = listed.get("data", []) if isinstance(listed, dict) else []
            args.model = pick_default_model(
                [m for m in data if isinstance(m, dict)],
                me.get("allowed_models") if isinstance(me, dict) else None,
            )
        except (CheckFailed, OSError, ValueError) as exc:
            print(
                f"FAIL models: cannot choose a default model: {smoke.mask(str(exc))}",
                file=sys.stderr,
            )
            return 1
        if not args.model:
            print(
                "No CLI-provider model found for this key: "
                f"pass --model or set {MODEL_ENV}.",
                file=sys.stderr,
            )
            return 2
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
