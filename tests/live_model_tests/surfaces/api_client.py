"""API HTTP surface client for the live-test framework.

Satisfies three surface Protocols simultaneously (structural typing):
  - ``SurfaceClient``           — base unified chat() dict
  - ``StreamingCapableSurface`` — adds ``raw_sse_lines`` for D5
  - ``BillingAwareSurface``     — adds ``billing`` + ``supports_billing`` for D6

Uses httpx (sync) with an injectable transport so unit tests run with
``httpx.MockTransport`` (no server). Pass ``transport=None`` for real network.

Council C7 (2026-05-31), duo:
  - PROPOSER : developer
  - CRITIC   : codex-coder (B-1/B-2 guards confirmed; M-2 stream-billing fixed)

Streaming billing note (Council C7 M-2): the proxy computes cost AFTER the
generation completes (post-yield hook), but HTTP response headers are flushed
BEFORE the streamed body — so ``X-Cost-USD`` is not reliably present on a
streamed response. D5 (streaming) does not need billing; D6 (cost) deliberately
uses ``stream=False``. Therefore ``billing`` is ``None`` for ``stream=True`` and
fully populated only for ``stream=False``.
"""

from __future__ import annotations

import json
import time
from typing import Any, ClassVar, Dict, List, Optional

import httpx

_CHAT_PATH = "/v1/chat/completions"
_DONE_SENTINEL = "[DONE]"
_CACHE_HIT_VALUES = {"hit", "semantic-hit", "1", "true"}


class ApiSurfaceError(Exception):
    """HTTP non-2xx from the proxy. ``error_type`` is read by run_dX handlers."""

    def __init__(self, status_code: int, body: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.error_type = f"http_error:{status_code}"
        super().__init__(f"HTTP {status_code}: {body[:200]}")


class MalformedResponseError(Exception):
    """200 OK but unparseable structure. ``error_type`` read by run_dX handlers."""

    def __init__(self, detail: str) -> None:
        self.error_type = "malformed_response"
        super().__init__(detail)


def _try_float(v: Optional[str]) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _try_int(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _billing_from_headers(
    headers: httpx.Headers,
    *,
    billed_model: Optional[str],
    pricing_month: Optional[str],
) -> Dict[str, Any]:
    return {
        "cost_usd": _try_float(headers.get("X-Cost-USD")),
        "prompt_tokens": _try_int(headers.get("X-Prompt-Tokens")),
        "completion_tokens": _try_int(headers.get("X-Completion-Tokens")),
        "provider": headers.get("X-Provider") or None,
        "billed_model": billed_model,
        "pricing_month": pricing_month,
        "cache_hit": headers.get("X-Cache", "").lower() in _CACHE_HIT_VALUES,
    }


class ApiSurfaceClient:
    """HTTP surface client for Plugboard's ``/v1/chat/completions``."""

    name: str = "api"
    supports_billing: ClassVar[bool] = True

    def __init__(
        self,
        base_url: str = "http://localhost:8765",
        api_key: str = "",
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._transport = transport

    def chat(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        stream: bool,
        timeout: float,
        max_tokens: int,
        temperature: float,
    ) -> Dict[str, Any]:
        if stream:
            return self._chat_stream(model, messages, timeout, max_tokens, temperature)
        return self._chat_no_stream(model, messages, timeout, max_tokens, temperature)

    def _build_body(
        self, model, messages, stream, max_tokens, temperature
    ) -> Dict[str, Any]:
        return {
            "model": model,
            "messages": list(messages),
            "stream": stream,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": accept,
        }

    # ── Non-streaming (used by D6) ───────────────────────────────────────────

    def _chat_no_stream(
        self, model, messages, timeout, max_tokens, temperature
    ) -> Dict[str, Any]:
        body = self._build_body(model, messages, False, max_tokens, temperature)
        with httpx.Client(transport=self._transport) as client:
            try:
                resp = client.post(
                    self._base_url + _CHAT_PATH,
                    json=body,
                    headers=self._headers(),
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                raise TimeoutError("request timed out") from exc
            except httpx.RequestError as exc:
                err = ApiSurfaceError(0, str(exc))
                err.error_type = "http_error:unknown"
                raise err from exc

        if resp.status_code != 200:
            raise ApiSurfaceError(resp.status_code, resp.text)

        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedResponseError(
                f"non-JSON body (200): {resp.text[:200]}"
            ) from exc

        choices = data.get("choices") or []
        choice = choices[0] if choices else {}
        message = choice.get("message") or {}
        body_billing = data.get("billing") or {}

        return {
            "content": message.get("content") or "",
            "reasoning": message.get("reasoning") or "",
            "finish_reason": choice.get("finish_reason"),
            "first_token_latency_ms": None,
            "done_received": True,
            "raw_chunks": [],
            "raw_sse_lines": None,
            "billing": _billing_from_headers(
                resp.headers,
                billed_model=body_billing.get("model") or None,
                pricing_month=body_billing.get("pricing_month") or None,
            ),
            "model_echoed": data.get("model"),
            "request_id": resp.headers.get("X-Request-ID") or None,
        }

    # ── Streaming (used by D1-D5) ────────────────────────────────────────────

    def _chat_stream(
        self, model, messages, timeout, max_tokens, temperature
    ) -> Dict[str, Any]:
        body = self._build_body(model, messages, True, max_tokens, temperature)
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        raw_chunks: List[Dict[str, Any]] = []
        raw_sse_lines: List[str] = []
        finish_reason: Optional[str] = None
        done_received = False
        first_token_latency_ms: Optional[float] = None
        model_echoed: Optional[str] = None
        request_id: Optional[str] = None
        t_start = time.monotonic()

        try:
            with httpx.Client(transport=self._transport) as client:
                with client.stream(
                    "POST",
                    self._base_url + _CHAT_PATH,
                    json=body,
                    headers=self._headers("text/event-stream"),
                    timeout=timeout,
                ) as resp:
                    request_id = resp.headers.get("X-Request-ID") or None
                    if resp.status_code != 200:
                        try:
                            resp.read()
                            err_body = resp.text
                        except Exception:  # noqa: BLE001
                            err_body = ""
                        raise ApiSurfaceError(resp.status_code, err_body)

                    for line in resp.iter_lines():
                        stripped = (line or "").strip()
                        if not stripped:
                            continue
                        raw_sse_lines.append(line)  # raw wire line for D5
                        if not stripped.startswith("data:"):
                            continue
                        payload = stripped[len("data:") :].strip()
                        if payload == _DONE_SENTINEL:
                            done_received = True
                            continue
                        if not payload:
                            continue
                        try:
                            chunk = json.loads(payload)
                        except (json.JSONDecodeError, ValueError):
                            continue  # D5 re-parses raw_sse_lines and flags it
                        raw_chunks.append(chunk)
                        if model_echoed is None:
                            model_echoed = chunk.get("model") or None
                        choices = chunk.get("choices")
                        if not choices:
                            continue  # usage-only chunk — tolerated
                        choice = (
                            choices[0] if isinstance(choices, list) and choices else {}
                        )
                        delta = choice.get("delta") or {}
                        dc = delta.get("content") or ""
                        if dc:
                            if first_token_latency_ms is None:
                                first_token_latency_ms = (
                                    time.monotonic() - t_start
                                ) * 1000.0
                            content_parts.append(dc)
                        dr = delta.get("reasoning") or ""
                        if dr:
                            reasoning_parts.append(dr)
                        fr = choice.get("finish_reason")
                        if fr is not None:
                            finish_reason = fr
        except httpx.TimeoutException as exc:
            raise TimeoutError("stream timed out") from exc
        except httpx.RequestError as exc:
            err = ApiSurfaceError(0, str(exc))
            err.error_type = "http_error:unknown"
            raise err from exc

        return {
            "content": "".join(content_parts),
            "reasoning": "".join(reasoning_parts),
            "finish_reason": finish_reason,
            "first_token_latency_ms": first_token_latency_ms,
            "done_received": done_received,
            "raw_chunks": raw_chunks,
            "raw_sse_lines": raw_sse_lines,
            "billing": None,  # Council C7 M-2 — unreliable on streamed responses
            "model_echoed": model_echoed,
            "request_id": request_id,
        }
