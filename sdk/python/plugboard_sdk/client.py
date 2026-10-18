"""Sync + async client for Plugboard.

Keeps the dependency surface tiny: stdlib `urllib` for the sync path,
and `httpx` only as an optional import for the async path (pulled in on
first async call, never forced on sync-only users).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Iterator, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PlugboardError(Exception):
    """Raised when Plugboard returns a non-2xx response."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class PlugboardClient:
    """Minimal, dependency-light client.

    Parameters
    ----------
    api_key:
        Your `pb_xxx` client key. Sent as `Authorization: Bearer <key>`.
    base_url:
        Base URL of Plugboard (default http://localhost:8765).
    timeout:
        Per-request wall-clock limit in seconds.
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

    def list_models(self) -> List[Dict[str, Any]]:
        result = self._get_json("/v1/models")
        return result.get("data", [])

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
            "User-Agent": "plugboard-python-sdk/0.1",
        }

    def _get_json(self, path: str) -> Dict[str, Any]:
        req = Request(self.base_url + path, method="GET", headers=self._headers())
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as exc:
            raise PlugboardError(
                exc.code, exc.read().decode(errors="replace")
            ) from exc
        except URLError as exc:
            raise PlugboardError(0, f"connection failed: {exc}") from exc

    def _post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body).encode()
        req = Request(
            self.base_url + path,
            data=data,
            method="POST",
            headers=self._headers(),
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as exc:
            raise PlugboardError(
                exc.code, exc.read().decode(errors="replace")
            ) from exc
        except URLError as exc:
            raise PlugboardError(0, f"connection failed: {exc}") from exc

    def _post_sse(self, path: str, body: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        data = json.dumps(body).encode()
        headers = {**self._headers(), "Accept": "text/event-stream"}
        req = Request(self.base_url + path, data=data, method="POST", headers=headers)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        except HTTPError as exc:
            raise PlugboardError(
                exc.code, exc.read().decode(errors="replace")
            ) from exc
        except URLError as exc:
            raise PlugboardError(0, f"connection failed: {exc}") from exc


# Names used before the Plugboard rename (deprecated aliases).
LLMEnablerError = PlugboardError
LLMEnablerClient = PlugboardClient
