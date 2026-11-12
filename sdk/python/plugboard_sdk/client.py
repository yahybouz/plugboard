"""Sync + async client for Plugboard.

Keeps the dependency surface tiny: stdlib `urllib` for the sync path,
and `httpx` only as an optional import for the async path (pulled in on
first async call, never forced on sync-only users).
"""

from __future__ import annotations

import http.client
import json
import math
import re
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, Iterator, List, Literal, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ._version import __version__

USER_AGENT = "plugboard-python-sdk/" + ".".join(__version__.split(".")[:2])
# socket.timeout is TimeoutError from Python 3.10 on, not before.
_TIMEOUTS = (TimeoutError, socket.timeout)
_TIMEOUT_BASES = tuple(dict.fromkeys(_TIMEOUTS))
# A Retry-After past a day is read as a day.
MAX_RETRY_AFTER = 86400.0
# An error code is a short identifier, never free text from the body.
_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class PlugboardError(Exception):
    """Raised when Plugboard returns a non-2xx response, or cannot be reached.

    ``status`` is the HTTP status (0 when no answer came), ``detail`` the
    answer's body (or a short message). ``code`` is the gateway's error
    code when it gave one (``providers_busy``, ``model_not_found``...;
    ``timeout`` and ``connection_failed`` when no answer came), and
    ``retry_after`` the seconds to wait before retrying, from the
    ``Retry-After`` header or the body's ``retry_after``.
    """

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        code: Optional[str] = None,
        retry_after: Optional[float] = None,
    ):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.code = code
        self.retry_after = retry_after

    def __reduce__(self):
        # Rebuilt from its fields, so it survives pickling (multiprocessing).
        fields = (self.status, self.detail, self.code, self.retry_after)
        return (_restore_error, (type(self), *fields))


def _restore_error(cls, status, detail, code, retry_after):
    err = cls.__new__(cls)
    PlugboardError.__init__(err, status, detail, code=code, retry_after=retry_after)
    return err


class PlugboardTimeoutError(PlugboardError, *_TIMEOUT_BASES):
    """No answer within the client's timeout: a ``PlugboardError`` with
    status 0 and code ``timeout``, and a ``TimeoutError`` too (and a
    ``socket.timeout`` on Python 3.9, where the two differ)."""

    def __init__(self, detail: str = "request timed out"):
        super().__init__(0, detail, code="timeout")


def _seconds(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, MAX_RETRY_AFTER)


def _retry_after(header: Optional[str], body: Any) -> Optional[float]:
    """Seconds from ``Retry-After`` (seconds or an HTTP date, a date without
    zone read as UTC), else from the body's ``retry_after``; at most
    ``MAX_RETRY_AFTER``; None when neither is usable."""
    if header:
        header = header.strip()
        try:
            seconds = _seconds(float(header))
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                seconds = _seconds(max(0.0, (when - now).total_seconds()))
            except (TypeError, ValueError, IndexError, OverflowError):
                seconds = None
        if seconds is not None:
            return seconds
    if isinstance(body, dict):
        return _seconds(body.get("retry_after"))
    return None


def _error_code(body: Any) -> Optional[str]:
    """``code`` of a Plugboard error body, of its ``detail`` object, or of an
    OpenAI-style ``error`` object."""
    if not isinstance(body, dict):
        return None
    for holder in (body, body.get("detail"), body.get("error")):
        code = holder.get("code") if isinstance(holder, dict) else None
        if isinstance(code, str) and _CODE.fullmatch(code):
            return code
    return None


def _error_from_response(
    status: int, text: str, retry_after_header: Optional[str]
) -> PlugboardError:
    """The ``PlugboardError`` for an error answer (also used by the async client)."""
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    return PlugboardError(
        status,
        text,
        code=_error_code(body),
        retry_after=_retry_after(retry_after_header, body),
    )


class _RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError with the 3xx status


def urlopen(request: Request, timeout: float):
    """Like urllib's urlopen, but never sends the key to a redirect target."""
    return build_opener(_RefuseRedirect).open(request, timeout=timeout)


def _http_error(exc: HTTPError) -> PlugboardError:
    if 300 <= exc.code < 400:
        return PlugboardError(exc.code, "Redirect refused")
    header = exc.headers.get("Retry-After") if exc.headers is not None else None
    return _error_from_response(exc.code, exc.read().decode(errors="replace"), header)


def _url_error(exc: URLError) -> PlugboardError:
    if isinstance(exc.reason, _TIMEOUTS):
        return PlugboardTimeoutError()
    return PlugboardError(0, f"connection failed: {exc}", code="connection_failed")


@contextmanager
def _transport():
    """Turn every failure to get an answer into a ``PlugboardError``."""
    try:
        yield
    except PlugboardError:
        raise
    except HTTPError as exc:
        raise _http_error(exc) from exc
    except URLError as exc:
        raise _url_error(exc) from exc
    except _TIMEOUTS:
        raise PlugboardTimeoutError() from None
    except (OSError, http.client.HTTPException) as exc:
        # Closed, reset or truncated after the connection was made.
        raise PlugboardError(
            0, f"connection failed: {type(exc).__name__}", code="connection_failed"
        ) from None


def _json_answer(resp: Any) -> Dict[str, Any]:
    raw = resp.read()
    try:
        return json.loads(raw.decode())
    except ValueError:
        status = getattr(resp, "status", 200)
        raise PlugboardError(status, "Invalid response: not JSON") from None


class PlugboardClient:
    """Minimal, dependency-light client.

    Parameters
    ----------
    api_key:
        Your `pb_xxx` client key. Sent as `Authorization: Bearer <key>`.
    base_url:
        Base URL of Plugboard (default http://localhost:8765).
    timeout:
        Seconds to wait for each network operation (connecting, then each
        read): a slow but steady stream never times out.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:8765",
        timeout: float = 120.0,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ── Public API ──────────────────────────────────────────────────────

    def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        session_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Non-streaming chat completion. Returns the raw response dict."""
        body = self._build_body(model, messages, max_tokens, temperature, extra)
        if session_id:
            body["session_id"] = session_id
        return self._post_json("/v1/chat/completions", body)

    def chat_stream(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        session_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Iterator[str]:
        """Streaming chat completion.

        Yields raw text deltas (``choices[0].delta.content``). Non-text
        chunks (usage, tool calls, etc.) are dropped — callers that want
        the full stream should use :meth:`chat_stream_raw` instead.

        Note: thinking models (Ollama qwen3.5/kimi/glm/deepseek, OpenAI o1)
        emit chain-of-thought in ``delta.reasoning``, which this method
        ignores by design (backward-compat). Use
        :meth:`chat_stream_with_reasoning` to also receive reasoning chunks.
        """
        for chunk in self.chat_stream_raw(
            model,
            messages,
            session_id=session_id,
            max_tokens=max_tokens,
            temperature=temperature,
            extra=extra,
        ):
            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
            if isinstance(delta, dict) and delta.get("content"):
                yield delta["content"]

    def chat_stream_with_reasoning(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        session_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Dict[str, str]]:
        """Streaming chat completion that surfaces both reasoning and content.

        Yields typed dicts ``{"kind": "reasoning" | "content", "text": <str>}``
        so callers can render thinking models cleanly (e.g. a collapsed
        "Thinking…" panel followed by the final answer). Backward-compat
        with the legacy :meth:`chat_stream` is preserved.
        """
        for chunk in self.chat_stream_raw(
            model,
            messages,
            session_id=session_id,
            max_tokens=max_tokens,
            temperature=temperature,
            extra=extra,
        ):
            delta = (chunk.get("choices") or [{}])[0].get("delta", {})
            if not isinstance(delta, dict):
                continue
            if delta.get("reasoning"):
                yield {"kind": "reasoning", "text": delta["reasoning"]}
            if delta.get("content"):
                yield {"kind": "content", "text": delta["content"]}

    def chat_stream_raw(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        session_id: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Streaming chat completion, yielding parsed SSE chunks."""
        body = self._build_body(model, messages, max_tokens, temperature, extra)
        body["stream"] = True
        if session_id:
            body["session_id"] = session_id
        yield from self._post_sse("/v1/chat/completions", body)

    def embeddings(
        self,
        model: str,
        input: Union[str, List[str]],
        *,
        encoding_format: Optional[Literal["float", "base64"]] = None,
        user: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Embed one text or a list of texts (``POST /v1/embeddings``).

        Returns the raw response: ``data[i]["embedding"]`` holds the vector of
        ``input[i]`` (a list of floats, or a base64 string with
        ``encoding_format="base64"``), with ``model`` and ``usage``.
        """
        body: Dict[str, Any] = {"model": model, "input": input}
        if encoding_format is not None:
            body["encoding_format"] = encoding_format
        if user is not None:
            body["user"] = user
        return self._post_json("/v1/embeddings", body)

    def list_models(self) -> List[Dict[str, Any]]:
        result = self._get_json("/v1/models")
        return result.get("data", [])

    def list_decision_models(self) -> List[Dict[str, Any]]:
        """List decision engines, including availability and capability limits."""
        try:
            return self._get_json("/v1/decisions/models").get("data", [])
        except _TIMEOUTS:
            err: PlugboardError = PlugboardTimeoutError("Decision request timed out")
        except PlugboardError as exc:
            err = _decision_error(exc)
        raise err  # outside the handler: the body stays out of __context__

    def decide(
        self,
        model: Literal["jev", "laya", "laya-multilingual"],
        state: str,
        questions: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Submit named typed questions. The gateway validates engine capabilities.

        ``choice`` uses a criteria mapping, ``score`` an ordered criteria list,
        and ``noul`` has no criteria. This operation never calls chat.
        """
        if model == "kev-0.8b":
            raise ValueError("Unsupported decision model")
        try:
            return self._post_json(
                "/v1/decisions",
                {"model": model, "state": state, "questions": questions},
            )
        except _TIMEOUTS:
            err: PlugboardError = PlugboardTimeoutError("Decision request timed out")
        except PlugboardError as exc:
            err = _decision_error(exc)
        raise err  # outside the handler: the body stays out of __context__

    def usage(self, month: Optional[str] = None) -> Dict[str, Any]:
        path = f"/usage/me?month={month}" if month else "/usage/me"
        return self._get_json(path)

    # ── Internals ───────────────────────────────────────────────────────

    def _build_body(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        extra: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"model": model, "messages": list(messages)}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if extra:
            body.update(extra)
        return body

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _get_json(self, path: str) -> Dict[str, Any]:
        req = Request(self.base_url + path, method="GET", headers=self._headers())
        with _transport(), urlopen(req, timeout=self.timeout) as resp:
            return _json_answer(resp)

    def _post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body).encode()
        req = Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers=self._headers(),
        )
        with _transport(), urlopen(req, timeout=self.timeout) as resp:
            return _json_answer(resp)

    def _post_sse(self, path: str, body: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        """Parsed SSE events. A stream that ends with neither ``[DONE]`` nor a
        ``finish_reason`` was cut: it raises code ``stream_interrupted``."""
        data = json.dumps(body).encode()
        headers = {**self._headers(), "Accept": "text/event-stream"}
        req = Request(self.base_url + path, data=data, method="POST", headers=headers)
        finished = False
        with _transport(), urlopen(req, timeout=self.timeout) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") if isinstance(event, dict) else None
                if isinstance(choices, list) and any(
                    isinstance(c, dict) and c.get("finish_reason") for c in choices
                ):
                    finished = True
                yield event
        if not finished:
            raise PlugboardError(
                200, "stream ended before [DONE]", code="stream_interrupted"
            )


def _decision_error(exc: PlugboardError) -> PlugboardError:
    """A decision error keeps its status, code and delay, never its body."""
    return PlugboardError(
        exc.status,
        "Decision request failed",
        code=exc.code,
        retry_after=exc.retry_after,
    )


# Names used before the Plugboard rename (deprecated aliases).
LLMEnablerError = PlugboardError
LLMEnablerClient = PlugboardClient
