"""
MCP Server — expose Plugboard as a Model Context Protocol server.

Allows MCP-compatible clients (Claude Code, Cursor, LangGraph agents)
to consume Plugboard tools, resources, and prompts natively.

Architecture:
    Client app ──MCP──→ This server ──HTTP──→ Plugboard API ──→ Direct providers

Tools:
    - chat: Send a completion request to any available model
    - list_models: List all available LLM models
    - check_usage: Get current month usage and spending
    - add_provider, refresh_catalog: admin tools, stdio only, off by default

Resources:
    - models://list — All available models with metadata
    - usage://current — Current month usage summary
    - monitoring://status — Provider health and watchdog status

Prompts:
    - quick_chat: Simple one-shot completion
    - analyze: Deep analysis with model selection guidance
"""

from __future__ import annotations

import asyncio
import collections
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Literal
from urllib.parse import quote

import aiohttp
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from plugboard import envcompat
from plugboard.legacy_connectors import is_removed_model, REMOVED_CONNECTOR_ERROR
from plugboard.storage import data_dir, private_sqlite_file

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

_BASE_URL = os.environ.get("PLUGBOARD_URL", "http://localhost:8765")
_API_KEY = os.environ.get("PLUGBOARD_API_KEY", "")
_ADMIN_KEY = os.environ.get("PLUGBOARD_ADMIN_KEY", "")
# PB-MCP-02: the admin tools (add_provider, refresh_catalog) are off unless
# the operator turns them on; an admin key set for provider_status alone
# never grants them.
_ADMIN_TOOLS = os.environ.get("PLUGBOARD_MCP_ADMIN_TOOLS", "").strip() == "1"
_TIMEOUT = envcompat.get_int("PLUGBOARD_MCP_TIMEOUT", default=900)
_ADMIN_TIMEOUT = max(1, min(_TIMEOUT, 120))
_ADMIN_RESPONSE_LIMIT = 1024 * 1024
_DECISION_TIMEOUT = max(1, min(_TIMEOUT, 45))
_DECISION_RESPONSE_LIMIT = 512 * 1024
_EMBEDDING_TIMEOUT = max(1, min(_TIMEOUT, 120))
_EMBEDDING_RESPONSE_LIMIT = 32 * 1024 * 1024
_EMBEDDING_MAX_INPUTS = 256
_EMBEDDING_STATUS = {
    401: ("client_key_refused", "The gateway refused the client API key"),
    403: ("model_not_permitted", "This key may not use the embedding model"),
    404: (
        "model_not_available",
        "Embedding model not available (see list_embedding_models)",
    ),
    413: ("request_too_large", "Embedding request too large"),
    422: ("invalid_request", "Invalid embedding request"),
    429: ("rate_limited", "Rate or spending limit exceeded"),
    502: ("engine_error", "The embedding engine failed"),
    503: ("engine_unavailable", "Embedding engine unavailable"),
    504: ("engine_timeout", "Embedding engine timed out"),
}
_HTTP_SESSION: aiohttp.ClientSession | None = None

# Network transports (SSE, streamable HTTP) need a dedicated bearer token;
# stdio is reached only by the local process that spawned the server.
MCP_TOKEN_MIN_LENGTH = 32
LOOPBACK_ALLOWED_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ALLOWED_ORIGINS = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
]
_NETWORK_TRANSPORT = False
_NETWORK_ADMIN_REFUSAL = (
    "Error: provider_status is not available over a network MCP transport: "
    "PLUGBOARD_ADMIN_KEY is only loaded for stdio."
)


class _StaticBearerVerifier:
    """FastMCP token verifier for the single PLUGBOARD_MCP_TOKEN.

    With no token configured every bearer is refused, so an HTTP app built
    from this server without configure_network_transport() stays closed.
    """

    def __init__(self) -> None:
        self._token = b""

    def configure(self, token: str) -> None:
        self._token = token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self._token or not hmac.compare_digest(
            token.encode("utf-8"), self._token
        ):
            return None
        return AccessToken(token="plugboard-mcp", client_id="plugboard-mcp", scopes=[])


_TOKEN_VERIFIER = _StaticBearerVerifier()

mcp = FastMCP(
    "plugboard",
    instructions="Plugboard: multi-model proxy with billing, routing and monitoring. "
    "Use list_models to discover available models, chat to send completions, "
    "check_usage for billing, provider_status for health monitoring.",
    token_verifier=_TOKEN_VERIFIER,
    # Resource-server mode with a static token: no OAuth routes are served.
    auth=AuthSettings(issuer_url="http://127.0.0.1", resource_server_url=None),
)


def configure_network_transport(
    token: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> None:
    """Prepare the server for SSE/HTTP: bearer auth, bind host, rebinding guard.

    The admin key is dropped: a network caller never acts with it.
    """
    global _ADMIN_KEY, _NETWORK_TRANSPORT
    if len(token) < MCP_TOKEN_MIN_LENGTH:
        raise ValueError(
            f"PLUGBOARD_MCP_TOKEN must hold at least {MCP_TOKEN_MIN_LENGTH} characters"
        )
    _TOKEN_VERIFIER.configure(token)
    _ADMIN_KEY = ""
    _NETWORK_TRANSPORT = True
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts or LOOPBACK_ALLOWED_HOSTS,
        allowed_origins=allowed_origins or LOOPBACK_ALLOWED_ORIGINS,
    )


# ── HTTP helpers ─────────────────────────────────────────────────────────────


async def _get_http_session() -> aiohttp.ClientSession:
    """Reuse one aiohttp session to avoid reconnect overhead on each MCP call."""
    global _HTTP_SESSION
    if _HTTP_SESSION is None or _HTTP_SESSION.closed:
        _HTTP_SESSION = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
        )
    return _HTTP_SESSION


def _auth_headers(admin: bool = False, json_body: bool = False) -> dict[str, str]:
    # Client calls use the client key only: the admin key would bypass the
    # per-key budget, rate and model limits.
    key = _ADMIN_KEY if admin else _API_KEY
    # PLU-13: lets /v1/inflight say the call came from the MCP server.
    headers: dict[str, str] = {"X-Plugboard-Surface": "mcp"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _client_error(status: int, text: str) -> str:
    """The client-facing message of a refused gateway call.

    The gateway's error body carries a stable ``code`` for the errors a client
    can act on (quota_exhausted, providers_busy, forbidden_model,
    model_not_supported_by_subscription, provider_timeout...): the message is
    then ``<code>: <detail>``, the same pair an API client reads. Without a
    code it stays ``HTTP <status>: <detail>``.
    """
    try:
        payload = json.loads(text)
    except ValueError:
        return f"HTTP {status}: {text[:_DETAIL_LIMIT]}"
    if isinstance(payload, dict) and ("detail" in payload or "code" in payload):
        detail = _error_detail(payload)[:_DETAIL_LIMIT]
        code = payload.get("code")
        if isinstance(code, str) and code:
            return f"{code}: {detail}"
        return f"HTTP {status}: {detail}"
    return f"HTTP {status}: {text[:_DETAIL_LIMIT]}"


async def _api_get(path: str, admin: bool = False) -> dict[str, Any]:
    """GET request to Plugboard API."""
    headers = _auth_headers(admin=admin)
    session = await _get_http_session()
    try:
        async with session.get(
            f"{_BASE_URL}{path}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                return {"error": _client_error(resp.status, text)}
            return await resp.json()
    except aiohttp.ClientError as exc:
        return {"error": f"HTTP client error: {exc}"}


async def _api_post(path: str, body: dict, admin: bool = False) -> dict[str, Any]:
    """POST request to Plugboard API."""
    headers = _auth_headers(admin=admin, json_body=True)
    session = await _get_http_session()
    try:
        async with session.post(
            f"{_BASE_URL}{path}",
            headers=headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                return {"error": _client_error(resp.status, text)}
            return await resp.json()
    except aiohttp.ClientError as exc:
        return {"error": f"HTTP client error: {exc}"}


def _decision_error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


async def _decision_request(
    method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Client-only bounded transport; never include raw errors or follow redirects."""
    if not _API_KEY:
        return _decision_error("client_key_required", "Configure a client API key")
    session = await _get_http_session()
    try:
        async with session.request(
            method,
            f"{_BASE_URL}{path}",
            headers=_auth_headers(json_body=body is not None),
            json=body,
            timeout=aiohttp.ClientTimeout(total=_DECISION_TIMEOUT),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                return _decision_error(
                    "decision_gateway_error",
                    f"Decision gateway returned HTTP {response.status}",
                )
            chunks = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                chunks.extend(chunk)
                if len(chunks) > _DECISION_RESPONSE_LIMIT:
                    return _decision_error(
                        "invalid_response", "Invalid decision gateway response"
                    )
            result = json.loads(chunks)
            if not isinstance(result, dict) or "error" in result:
                return _decision_error(
                    "invalid_response", "Invalid decision gateway response"
                )
            return result
    except asyncio.TimeoutError:
        return _decision_error("decision_timeout", "Decision gateway timed out")
    except aiohttp.ClientError:
        return _decision_error(
            "decision_connection_error", "Decision gateway unavailable"
        )
    except (ValueError, UnicodeError, RecursionError):
        return _decision_error("invalid_response", "Invalid decision gateway response")


async def _embedding_request(
    method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Client-key-only bounded transport for /v1/embeddings (no admin key)."""
    if not _API_KEY:
        return _decision_error("client_key_required", "Configure a client API key")
    session = await _get_http_session()
    try:
        async with session.request(
            method,
            f"{_BASE_URL}{path}",
            headers=_auth_headers(json_body=body is not None),
            json=body,
            timeout=aiohttp.ClientTimeout(total=_EMBEDDING_TIMEOUT),
            allow_redirects=False,
        ) as response:
            chunks = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                chunks.extend(chunk)
                if len(chunks) > _EMBEDDING_RESPONSE_LIMIT:
                    return _decision_error(
                        "invalid_response", "Embedding response too large"
                    )
            if response.status != 200:
                code, message = _EMBEDDING_STATUS.get(
                    response.status,
                    ("gateway_error", f"Gateway returned HTTP {response.status}"),
                )
                try:
                    detail = json.loads(chunks).get("detail")
                except (ValueError, UnicodeError, AttributeError, RecursionError):
                    detail = None
                if isinstance(detail, str) and 0 < len(detail) <= 200:
                    message = f"{message}: {detail}"
                return _decision_error(code, message)
            result = json.loads(chunks)
            if not isinstance(result, dict) or "error" in result:
                return _decision_error("invalid_response", "Invalid gateway response")
            return result
    except asyncio.TimeoutError:
        return _decision_error("timeout", "Embedding gateway timed out")
    except aiohttp.ClientError:
        return _decision_error("connection_error", "Embedding gateway unavailable")
    except (ValueError, UnicodeError, RecursionError):
        return _decision_error("invalid_response", "Invalid gateway response")


# ── Caller identity ──────────────────────────────────────────────────────────


class CallerIdentityError(Exception):
    """The client of PLUGBOARD_API_KEY could not be established.

    ``str(exc)`` is shown to the MCP caller, after ``Error: ``.
    """


async def _caller_client_id() -> str:
    """The client of this server's key, as the gateway resolves it (/v1/me).

    Asked at each call, never cached: a revoked key is refused at once. The
    admin key is refused: per-client data needs a client key.
    """
    if not _API_KEY:
        raise CallerIdentityError(
            "this tool needs PLUGBOARD_API_KEY: it acts for that key's client"
        )
    try:
        result = await _api_get("/v1/me")
    except (asyncio.TimeoutError, ValueError) as exc:  # timeout, invalid JSON
        result = {"error": type(exc).__name__}
    if not isinstance(result, dict):
        result = {"error": "unexpected gateway answer"}
    if "error" in result:
        raise CallerIdentityError(
            "could not identify the client of PLUGBOARD_API_KEY "
            f"({str(result['error'])[:120]})"
        )
    client_id = result.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise CallerIdentityError("PLUGBOARD_API_KEY does not identify a client")
    if client_id == "admin":
        raise CallerIdentityError(
            "PLUGBOARD_API_KEY is the admin key: give the MCP server a client key"
        )
    return client_id


async def _registry_client(claimed: str = "") -> str:
    """The calling key's client; ``claimed`` may only name that client."""
    client_id = await _caller_client_id()
    claimed = (claimed or "").strip()
    if claimed and claimed != client_id:
        raise CallerIdentityError(
            f"client_id '{claimed}' is not the client of this key "
            f"('{client_id}'): leave client_id out"
        )
    return client_id


# ── Admin tools (PB-MCP-02) ──────────────────────────────────────────────────
#
# add_provider and refresh_catalog call the gateway's admin routes with the
# admin key: validation, DNS pinning, audit and webhooks are the gateway's.
# This process only gates them (stdio, switch, key) and limits an agent that
# loops; anyone holding the admin key can still call the routes directly.

_CREATE_LIMIT = 5
_CREATE_WINDOW_S = 3600.0
_REFRESH_COOLDOWN_S = 60.0
_NAMES_SHOWN = 20
_NAME_MAX = 120
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CLI_AUTH_PREFIXES = ("subprocess:", "cli:")
_DETAIL_LIMIT = 300
_MFA_DETAIL = "Admin MFA required"
_MAYBE_CREATED = (
    ": the provider may have been created, check the dashboard before retrying"
)
_MAYBE_REFRESHED = ": the catalog may have been refreshed, check the dashboard"

_clock = time.monotonic
# Times of the add_provider calls of the last hour (refusals included).
_create_calls: collections.deque[float] = collections.deque()
# (provider_id, dry_run) -> time of the last refresh_catalog call.
_refresh_last: dict[tuple[str, bool], float] = {}
_refresh_lock = asyncio.Lock()


class AdminToolError(Exception):
    """An admin tool call refused or failed.

    ``str(exc)`` is shown to the MCP caller, after ``Error: ``.
    ``maybe_applied``: the request may have reached the gateway (timeout,
    connection lost mid-answer), so it may have taken effect.
    """

    def __init__(self, message: str, *, maybe_applied: bool = False) -> None:
        super().__init__(message)
        self.maybe_applied = maybe_applied


def _admin_gate(tool: str) -> None:
    """Refuse an admin tool unless it runs over stdio, switched on, with a key.

    No HTTP call is made: the refusal comes before anything else.
    """
    if _NETWORK_TRANSPORT:
        raise AdminToolError(
            f"{tool} is not available over a network MCP transport: "
            "PLUGBOARD_ADMIN_KEY is only loaded for stdio."
        )
    if not _ADMIN_TOOLS:
        raise AdminToolError(
            "MCP admin tools are off: set PLUGBOARD_MCP_ADMIN_TOOLS=1 and "
            "PLUGBOARD_ADMIN_KEY (stdio only)"
        )
    if not _ADMIN_KEY:
        raise AdminToolError(f"{tool} needs PLUGBOARD_ADMIN_KEY (stdio only)")


async def _admin_request(
    method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """One admin call to the gateway: ``(status, JSON body)``.

    Sends the admin key and the ``mcp`` surface, follows no redirect and
    reads at most 1 MiB. A body that is not JSON comes back as
    ``{"detail": <text>}``; a larger one as
    ``{"detail": "gateway answer too large"}``.
    """
    session = await _get_http_session()
    raw = bytearray()
    status: int | None = None
    try:
        async with session.request(
            method,
            f"{_BASE_URL}{path}",
            headers=_auth_headers(admin=True, json_body=body is not None),
            json=body,
            timeout=aiohttp.ClientTimeout(total=_ADMIN_TIMEOUT),
            allow_redirects=False,
        ) as resp:
            status = resp.status
            async for chunk in resp.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > _ADMIN_RESPONSE_LIMIT:
                    break
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # Only the exception type: its text could quote the request. Only a
        # failed connection is sure to come before the request was sent.
        name = type(exc).__name__
        if status is not None:
            raise AdminToolError(
                f"the gateway answer was cut off ({name})", maybe_applied=True
            ) from None
        unsent = isinstance(
            exc, (aiohttp.ClientConnectorError, aiohttp.ConnectionTimeoutError)
        )
        raise AdminToolError(
            f"could not reach the gateway ({name})", maybe_applied=not unsent
        ) from None
    if len(raw) > _ADMIN_RESPONSE_LIMIT:
        return status, {"detail": "gateway answer too large"}
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except (ValueError, RecursionError):
        return status, {"detail": text}


def _error_detail(payload: Any) -> str:
    """The gateway's reason: ``detail``, else ``error.message``, else the body."""
    if isinstance(payload, dict):
        if "detail" in payload:
            detail = payload["detail"]
        elif isinstance(payload.get("error"), dict) and "message" in payload["error"]:
            detail = payload["error"]["message"]
        else:
            detail = payload
    else:
        detail = payload
    return detail if isinstance(detail, str) else json.dumps(detail)


def _gateway_error(status: int, payload: Any, secret: str = "") -> str:
    """The message for a refused admin call (without ``Error: ``).

    ``secret`` is removed before the detail is cut to 300 characters, so a
    cut cannot leave part of it behind.
    """
    detail = _scrub(_error_detail(payload), secret)
    if status == 401:
        if detail.startswith(_MFA_DETAIL):
            return (
                "the gateway enforces admin MFA: MCP admin tools cannot sign in, "
                "use the dashboard"
            )
        return "the gateway refused PLUGBOARD_ADMIN_KEY"
    return f"HTTP {status}: {detail[:_DETAIL_LIMIT]}"


def _scrub(text: str, secret: str) -> str:
    """``text`` with every copy of ``secret`` replaced by ``***``.

    The JSON-escaped spelling is replaced too: a detail that is a list or a
    mapping is shown through ``json.dumps``.
    """
    if not secret:
        return text
    text = text.replace(secret, "***")
    escaped = json.dumps(secret)[1:-1]
    return text.replace(escaped, "***") if escaped != secret else text


def _retry_in(remaining: float, window: float) -> int:
    return min(int(remaining) + 1, int(window))


def _take_create_slot() -> None:
    """Count one add_provider call; refuse a sixth within an hour."""
    now = _clock()
    while _create_calls and now - _create_calls[0] >= _CREATE_WINDOW_S:
        _create_calls.popleft()
    if len(_create_calls) >= _CREATE_LIMIT:
        wait = _retry_in(_CREATE_WINDOW_S - (now - _create_calls[0]), _CREATE_WINDOW_S)
        raise AdminToolError(
            f"add_provider limit reached ({_CREATE_LIMIT} per hour): retry in {wait}s"
        )
    _create_calls.append(now)


def _take_refresh_slot(provider_id: str, dry_run: bool) -> None:
    """Count one refresh; refuse the same provider and mode within 60 s."""
    now = _clock()
    for old in [k for k, t in _refresh_last.items() if now - t >= _REFRESH_COOLDOWN_S]:
        del _refresh_last[old]
    key = (provider_id, dry_run)
    last = _refresh_last.get(key)
    if last is not None and now - last < _REFRESH_COOLDOWN_S:
        wait = _retry_in(_REFRESH_COOLDOWN_S - (now - last), _REFRESH_COOLDOWN_S)
        raise AdminToolError(
            f"catalog of '{provider_id}' was refreshed less than "
            f"{int(_REFRESH_COOLDOWN_S)} s ago: retry in {wait}s"
        )
    _refresh_last[key] = now


# Loggers of the MCP SDK that log whole received messages at DEBUG
# (tool arguments, so a provider key): stdio, SSE and streamable HTTP.
_SDK_MESSAGE_LOGGERS = (
    "mcp.server.lowlevel.server",
    "mcp.server.sse",
    "mcp.server.streamable_http",
)


class _DropDebug(logging.Filter):
    """Drop the DEBUG records of a logger, whatever level it is set to."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno > logging.DEBUG


def _cap_sdk_message_logging() -> None:
    """Keep the MCP SDK from logging whole messages (tool arguments, keys).

    At DEBUG these loggers write every message they receive, so an
    add_provider call would put the provider key in the log, even when the
    tool then refuses. A filter survives a later ``setLevel`` and does not
    loosen a stricter level set by the operator. Idempotent.
    """
    for name in _SDK_MESSAGE_LOGGERS:
        sdk = logging.getLogger(name)
        if not any(isinstance(f, _DropDebug) for f in sdk.filters):
            sdk.addFilter(_DropDebug())


_cap_sdk_message_logging()


# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_decision_models() -> str:
    """List typed decision engines, availability, limits and confidence semantics.

    These engines accept named questions through decide and cannot be used for chat.
    Requires PLUGBOARD_API_KEY; an admin key is never substituted.
    """
    return json.dumps(await _decision_request("GET", "/v1/decisions/models"))


@mcp.tool()
async def decide(
    model: Literal["jev", "laya", "laya-multilingual"],
    state: str,
    questions: dict[str, dict[str, Any]],
) -> str:
    """Answer named typed questions through the dedicated decision API.

    Args:
        model: Exact engine ID from list_decision_models.
        state: Bounded state text the engine evaluates.
        questions: Mapping of answer IDs to typed questions. Each has type and
            instructions. choice has criteria mapping labels to descriptions or
            null; score has an ordered criteria list; noul has no criteria.

    The gateway validates input and engine capabilities. Probabilities and
    confidence retain each engine's documented semantics. No chat fallback.
    Requires PLUGBOARD_API_KEY; an admin key is never substituted.
    """
    if model == "kev-0.8b":
        return json.dumps(
            _decision_error("invalid_request", "Unsupported decision model")
        )
    return json.dumps(
        await _decision_request(
            "POST",
            "/v1/decisions",
            {"model": model, "state": state, "questions": questions},
        )
    )


@mcp.tool()
async def list_embedding_models() -> str:
    """List the embedding model ids this client key may use.

    Returns ids (send them as `model` to embed), availability and the vector
    dimensions once known. Embedding models run on the local engine and cannot
    chat. Requires PLUGBOARD_API_KEY; an admin key is never substituted.
    """
    return json.dumps(await _embedding_request("GET", "/v1/embeddings/models"))


@mcp.tool()
async def embed(
    input: str | list[str], model: str, include_vectors: bool = True
) -> str:
    """Embed one text or a list of texts through POST /v1/embeddings.

    Args:
        input: A text, or a list of up to 256 texts (never truncated: an input
            too long for the model is refused).
        model: Exact id from list_embedding_models, e.g. ollama:qwen3-embedding:0.6b.
        include_vectors: False returns only model, count, dimensions and usage.

    Returns JSON with model, count, dimensions, usage, provider_id, digest and
    vectors (ordered like the inputs). Requires PLUGBOARD_API_KEY.
    """
    texts = [input] if isinstance(input, str) else input
    if (
        not isinstance(texts, list)
        or not texts
        or len(texts) > _EMBEDDING_MAX_INPUTS
        or not all(isinstance(t, str) and t for t in texts)
    ):
        return json.dumps(
            _decision_error(
                "invalid_request",
                f"input must be a non-empty text or 1 to {_EMBEDDING_MAX_INPUTS} non-empty texts",
            )
        )
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        return json.dumps(_decision_error("invalid_request", "model is required"))
    result = await _embedding_request(
        "POST",
        "/v1/embeddings",
        {"model": model.strip(), "input": input, "encoding_format": "float"},
    )
    if "error" in result:
        return json.dumps(result)
    data = result.get("data")
    if not isinstance(data, list) or len(data) != len(texts):
        return json.dumps(
            _decision_error("invalid_response", "Invalid gateway response")
        )
    ordered = sorted(
        (item for item in data if isinstance(item, dict)),
        key=lambda item: item.get("index", 0),
    )
    vectors = [item.get("embedding") for item in ordered]
    if len(vectors) != len(texts) or not all(
        isinstance(v, list) and v for v in vectors
    ):
        return json.dumps(
            _decision_error("invalid_response", "Invalid gateway response")
        )
    served = (
        result.get("plugboard") if isinstance(result.get("plugboard"), dict) else {}
    )
    out: dict[str, Any] = {
        "model": result.get("model", model),
        "count": len(vectors),
        "dimensions": len(vectors[0]),
        "usage": result.get("usage", {}),
        "provider_id": served.get("provider_id"),
        "digest": served.get("digest"),
    }
    if include_vectors:
        out["vectors"] = vectors
    return json.dumps(out)


@mcp.tool()
async def chat(
    model: str,
    message: str,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    system_prompt: str = "",
    session_id: str = "",
) -> str:
    """Send a chat completion request to an LLM via Plugboard.

    Use list_models to see available models.
    Use list_models to discover available model IDs for configured providers.

    Args:
        model: Model ID as returned by list_models (e.g. my-provider:claude-sonnet-4-6)
        message: The user message to send
        max_tokens: Maximum tokens in the response (default: 4096)
        temperature: Sampling temperature 0.0-1.0 (default: 0.7)
        system_prompt: Optional system prompt to set context
        session_id: Optional server-side session ID (PERF-1). When set,
            Plugboard prepends the stored history so you only need to send the
            new turn. TTL 24h, 50-turn cap, isolated per API key. Use a UUID
            per conversation.
    """
    # This client has no registry. Qualified names whose owner is ambiguous
    # are admitted by the authenticated API, which guards the actual transport.
    if is_removed_model(model, effective_name=True):
        return f"Error: {REMOVED_CONNECTOR_ERROR}"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": message})

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if session_id:
        body["session_id"] = session_id

    result = await _api_post("/v1/chat/completions", body)

    if "error" in result:
        return f"Error: {result['error']}"

    choices = result.get("choices", [])
    if not choices:
        return "Error: Empty response from LLM (no choices)"

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        return "Error: Empty content in LLM response"

    # Append usage info
    usage = result.get("usage", {})
    billing = result.get("billing", {})
    meta = []
    if usage.get("total_tokens"):
        meta.append(f"tokens: {usage['total_tokens']}")
    if billing.get("cost_usd"):
        meta.append(f"cost: ${billing['cost_usd']:.4f}")

    if meta:
        return f"{content}\n\n---\n_({', '.join(meta)})_"
    return content


@mcp.tool()
async def chat_stream(
    model: str,
    message: str,
    max_tokens: int = 4096,
    temperature: float = 0.7,
    system_prompt: str = "",
    session_id: str = "",
) -> str:
    """Send a streaming chat completion. Same as chat but with stream=true.

    Args:
        model: Model ID
        message: The user message
        max_tokens: Maximum tokens (default: 4096)
        temperature: Sampling temperature (default: 0.7)
        system_prompt: Optional system prompt
        session_id: Optional PERF-1 session ID to preserve history server-side.
    """
    if is_removed_model(model, effective_name=True):
        return f"Error: {REMOVED_CONNECTOR_ERROR}"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": message})

    headers = _auth_headers(json_body=True)

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if session_id:
        body["session_id"] = session_id

    chunks = []
    session = await _get_http_session()
    try:
        async with session.post(
            f"{_BASE_URL}/v1/chat/completions",
            headers=headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                return f"Error: {_client_error(resp.status, text)}"
            reasoning_chunks: List[str] = []
            stream_error = ""
            async for line in resp.content:
                text = line.decode("utf-8", errors="replace").strip()
                if text.startswith("data: ") and text != "data: [DONE]":
                    try:
                        data = json.loads(text[6:])
                        if isinstance(data.get("error"), dict):
                            err = data["error"]
                            stream_error = (
                                f"{err.get('code') or 'stream_error'}: "
                                f"{err.get('message', '')}"[:_DETAIL_LIMIT]
                            )
                            continue
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        # Thinking models (Ollama qwen3.5/kimi/glm/deepseek,
                        # OpenAI o1) emit chain-of-thought in delta.reasoning.
                        # Surface it inside a [thinking]...[/thinking] block so
                        # MCP clients see the model was working, not silent.
                        if delta.get("reasoning"):
                            reasoning_chunks.append(delta["reasoning"])
                        if delta.get("content"):
                            chunks.append(delta["content"])
                    except (json.JSONDecodeError, IndexError):
                        pass
    except aiohttp.ClientError as exc:
        return f"Error: HTTP client error: {exc}"

    if stream_error:
        return "".join(chunks) + f"\n\nError: {stream_error}"
    if chunks:
        if reasoning_chunks:
            return (
                "[thinking]\n"
                + "".join(reasoning_chunks).strip()
                + "\n[/thinking]\n\n"
                + "".join(chunks)
            )
        return "".join(chunks)
    if reasoning_chunks:
        # Truncation case: model spent the whole budget thinking.
        return (
            "[thinking]\n" + "".join(reasoning_chunks).strip() + "\n[/thinking]\n\n"
            "Error: model produced reasoning but no final content "
            "(likely max_tokens exhausted — retry with a larger budget)."
        )
    return "Error: Empty streaming response"


def _model_ids_from_text(text: str) -> List[str]:
    """The model IDs a ``list_models`` answer shows (each one in backticks)."""
    return re.findall(r"^\s*- `([^`]+)`", text, flags=re.MULTILINE)


@mcp.tool()
async def list_models() -> str:
    """List all available LLM models from Plugboard.

    Returns model IDs grouped by vendor (Anthropic, OpenAI, Google, etc.).
    Use these exact IDs in the chat tool's model parameter.
    """
    result = await _api_get("/v1/models")
    if "error" in result:
        return f"Error: {result['error']}"

    models = result.get("data", result) if isinstance(result, dict) else result
    if not isinstance(models, list):
        return f"Unexpected response: {str(result)[:300]}"

    # Preserve the catalogue's actual owner, including namespaced native IDs.
    from plugboard.legacy_connectors import is_removed_catalogue_model

    models = [m for m in models if not is_removed_catalogue_model(m)]
    by_vendor: dict[str, list[dict[str, Any]]] = {}
    for m in models:
        vendor = m.get("owned_by") or m.get("provider") or "unknown"
        by_vendor.setdefault(vendor, []).append(m)

    lines = [f"**{len(models)} models available:**\n"]
    for vendor in sorted(by_vendor):
        lines.append(f"\n**{vendor}** ({len(by_vendor[vendor])}):")
        for m in sorted(by_vendor[vendor], key=lambda x: x.get("id", "")):
            mid = m.get("id", "")
            connected = m.get("connected", False)
            last_probed = m.get("last_probed_at")
            if connected is True:
                conn_icon = "🟢"
            elif last_probed or m.get("reason"):
                conn_icon = "🔴"
            else:
                conn_icon = "⚪"
            has_internet = m.get("internet_access", False)
            internet_str = " 🌐" if has_internet else ""
            probed_str = (
                f" (probed {last_probed})" if last_probed else " (never probed)"
            )
            lines.append(f"  - `{mid}` {conn_icon}{internet_str}{probed_str}")

    return "\n".join(lines)


@mcp.tool()
async def check_usage() -> str:
    """Check current month usage, spending, and billing for the API key.

    Returns token counts, costs, and spending limit status.
    """
    result = await _api_get("/v1/me")
    if "error" in result:
        return f"Error: {result['error']}"

    usage = result.get("usage", {})
    client_id = result.get("client_id", "unknown")
    limit = result.get("monthly_limit_usd", 0)
    remaining = result.get("remaining_usd")

    lines = [
        f"**Client:** {client_id}",
        f"**Month:** {result.get('current_month', '?')}",
        f"**Requests:** {usage.get('total_entries', 0)}",
        f"**Tokens:** {usage.get('total_tokens', 0):,} "
        f"({usage.get('total_prompt_tokens', 0):,} in / "
        f"{usage.get('total_completion_tokens', 0):,} out)",
        f"**Cost:** ${usage.get('total_cost_usd', 0):.4f}",
    ]
    if limit:
        lines.append(f"**Limit:** ${limit:.2f}")
    if remaining is not None:
        lines.append(f"**Remaining:** ${remaining:.2f}")

    # Per-model breakdown
    by_model = usage.get("by_model", {})
    if by_model:
        lines.append("\n**By model:**")
        for model, stats in sorted(by_model.items()):
            lines.append(
                f"  - {model}: {stats.get('entries', 0)} calls, ${stats.get('cost_usd', 0):.4f}"
            )

    return "\n".join(lines)


@mcp.tool()
async def inflight_requests() -> str:
    """Show the chat calls running right now and the last results.

    Use it when a call is slow, to tell "still running" from "broken": each
    running call shows its model, how long it has run and its phase (queued,
    calling the provider, CLI running...). A call flagged slow is still
    running. Only the calls of this key's client are listed. Refused when
    PLUGBOARD_API_KEY is the admin key.
    """
    # The admin key would list every client's calls.
    try:
        await _caller_client_id()
    except CallerIdentityError as exc:
        return f"Error: {exc}"
    data = await _api_get("/v1/inflight")
    if "error" in data:
        return f"Error: {data['error']}"
    lines = [f"**In flight:** {data.get('active_count', 0)}"]
    for e in data.get("active", []):
        slow = " (slow, still running)" if e.get("slow") else ""
        lines.append(
            f"- {e.get('model') or '-'} via {e.get('surface')}: "
            f"{e.get('elapsed_s')}s, {e.get('phase')}{slow}"
        )
    lines.append("")
    lines.append("**Last results:**")
    for e in data.get("recent", [])[:10]:
        lines.append(
            f"- {e.get('model') or '-'} via {e.get('surface')}: "
            f"{e.get('outcome')} {e.get('status') or ''} in {e.get('elapsed_s')}s"
        )
    return "\n".join(lines)


@mcp.tool()
async def provider_status() -> str:
    """Check the health and status of all LLM providers.

    Shows provider health, watchdog status, and recent events.
    Requires admin access.
    """
    if _NETWORK_TRANSPORT:
        return _NETWORK_ADMIN_REFUSAL
    # Watchdog status
    wd = await _api_get("/admin/watchdog", admin=True)
    if "error" in wd:
        return f"Error (need admin key): {wd['error']}"

    lines = [
        f"**Watchdog:** {'Running' if wd.get('running') else 'Stopped'}",
        f"**Check interval:** {wd.get('check_interval_s', '?')}s",
        "",
    ]

    for pid, state in wd.get("providers", {}).items():
        status = "Healthy" if state.get("is_healthy") else "UNHEALTHY"
        lines.append(f"**{pid}:** {status}")
        lines.append(f"  Failures: {state.get('consecutive_failures', 0)}")
        lines.append(f"  Restarts: {state.get('total_restarts', 0)}")
        deep = state.get("last_deep_ok")
        if deep is not None:
            lines.append(f"  Deep check: {'OK' if deep else 'FAILED'}")

    # Model connectivity summary from model_status.json
    try:
        from plugboard.model_availability import load_model_status

        ms = load_model_status()
        status_models = ms.get("models", {})
        if status_models:
            lines.append("")
            lines.append(
                f"**Model connectivity** (last probed: {ms.get('last_run_at', 'unknown')}):"
            )
            by_provider: dict[str, dict[str, int]] = {}
            for entry in status_models.values():
                pid = entry.get("provider", "unknown")
                bucket = by_provider.setdefault(pid, {"online": 0, "offline": 0})
                if entry.get("connected"):
                    bucket["online"] += 1
                else:
                    bucket["offline"] += 1
            for pid, counts in sorted(by_provider.items()):
                total = counts["online"] + counts["offline"]
                lines.append(f"  {pid}: {counts['online']}/{total} online")
    except Exception:
        pass

    return "\n".join(lines)


@mcp.tool()
async def add_provider(
    name: str, base_url: str, api_key: str, default_model: str = ""
) -> str:
    """Add an OpenAI-compatible provider to the gateway (admin, stdio only).

    The provider is created inactive, with "Public addresses only" on, so an
    internal or private address is refused; the gateway checks the rest as
    POST /providers does. Then run refresh_catalog with the id this tool
    answers; an admin activates the provider in the dashboard.

    Needs PLUGBOARD_MCP_ADMIN_TOOLS=1 and PLUGBOARD_ADMIN_KEY. At most 5
    calls per hour reach the gateway, refused ones included. If the answer
    says the provider may have been created, check the dashboard instead of
    retrying.

    Args:
        name: Display name; the gateway derives the id from it (a taken id
            gets a suffix such as "-1": use the id in the answer).
        base_url: OpenAI-compatible root URL, usually ending in /v1.
        api_key: The provider's API key; sent to the gateway only, never
            shown back.
        default_model: Optional model ID used when none is given.
    """
    try:
        _admin_gate("add_provider")
        _take_create_slot()
        status, payload = await _admin_request(
            "POST",
            "/providers",
            {
                "name": name,
                "base_url": base_url,
                "bearer_token": api_key,
                "default_model": default_model,
                "is_active": False,
                "egress_guard": True,
            },
        )
    except AdminToolError as exc:
        message = str(exc)
        if exc.maybe_applied:
            message += _MAYBE_CREATED
        return "Error: " + _scrub(message, api_key)
    if status != 201:
        message = _gateway_error(status, payload, api_key)
        if status in (500, 502, 504):
            message += _MAYBE_CREATED
        return "Error: " + message
    provider_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(provider_id, str) or not provider_id:
        return f"Error: unexpected gateway answer (HTTP {status}){_MAYBE_CREATED}"
    if payload.get("egress_guard") is not True or payload.get("is_active") is not False:
        # A gateway older than 4.9.0 ignores egress_guard: say so, never
        # claim a protection the gateway did not apply.
        return (
            f"Error: provider '{provider_id}' was created, but the gateway did "
            'not apply "inactive, public addresses only" (gateway older than '
            "4.9.0?): check it in the dashboard"
        )
    shown_url = _scrub(str(payload.get("base_url", "")), api_key)
    return (
        f"Provider '{provider_id}' created, inactive, public addresses only "
        f"({shown_url}). Run refresh_catalog('{provider_id}'), then activate it "
        "in the dashboard (Providers)."
    )


def _name(item: Any) -> str:
    """One model ID as shown to the agent: one line, 120 characters at most.

    IDs come from the provider's own API, which may be hostile.
    """
    name = _CONTROL_CHARS.sub(" ", str(item))
    return name if len(name) <= _NAME_MAX else name[:_NAME_MAX] + "…"


def _names(items: Any) -> str:
    """Model IDs for a tool answer: the first 20, then a count."""
    if not isinstance(items, list) or not items:
        return "none"
    names = [_name(item) for item in items]
    shown = ", ".join(names[:_NAMES_SHOWN])
    if len(names) > _NAMES_SHOWN:
        shown += f" (+{len(names) - _NAMES_SHOWN} more)"
    return shown


@mcp.tool()
async def refresh_catalog(provider_id: str, dry_run: bool = False) -> str:
    """Refresh a provider's model list from its API (admin, stdio only).

    Same as the dashboard's Discover button: the gateway lists the
    provider's models and writes them; models it no longer lists are
    removed at once, unless its list is not trusted, and pinned models are
    kept: run it with dry_run=True first. CLI providers are refused (their
    refresh depends on a local CLI and can run model probes): use the
    dashboard. Needs
    PLUGBOARD_MCP_ADMIN_TOOLS=1 and PLUGBOARD_ADMIN_KEY; one refresh at a
    time, and once a minute per provider and mode.

    Args:
        provider_id: The provider's id, as add_provider or the dashboard
            shows it.
        dry_run: Show what would be added and removed, write nothing.
    """
    try:
        _admin_gate("refresh_catalog")
        if provider_id.strip() in ("", ".", ".."):
            # "." and ".." would be folded into another URL path.
            raise AdminToolError(f"invalid provider_id '{provider_id}'")
        if _refresh_lock.locked():
            raise AdminToolError("a catalog refresh is already running")
        async with _refresh_lock:
            status, providers = await _admin_request("GET", "/providers")
            if status != 200 or isinstance(providers, dict):
                return "Error: " + _gateway_error(status, providers)
            if not isinstance(providers, list):
                return f"Error: unexpected gateway answer (HTTP {status})"
            for entry in providers:
                if (
                    isinstance(entry, dict)
                    and entry.get("id") == provider_id
                    and str(entry.get("auth_source") or "").startswith(
                        _CLI_AUTH_PREFIXES
                    )
                ):
                    raise AdminToolError(
                        f"'{provider_id}' is a CLI provider: refresh it from "
                        "the dashboard"
                    )
            _take_refresh_slot(provider_id, dry_run)
            path = f"/providers/{quote(provider_id, safe='')}/discover"
            if dry_run:
                path += "?dry_run=true"
            status, payload = await _admin_request("POST", path)
    except AdminToolError as exc:
        maybe = exc.maybe_applied and not dry_run
        return f"Error: {exc}{_MAYBE_REFRESHED if maybe else ''}"
    # A write may have happened before a 500/504 or an odd 200 (the gateway's
    # own 502 comes before any write).
    maybe = "" if dry_run else _MAYBE_REFRESHED
    if status != 200:
        return (
            "Error: "
            + _gateway_error(status, payload)
            + (maybe if status in (500, 504) else "")
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("discovered"), int):
        return f"Error: unexpected gateway answer (HTTP {status}){maybe}"
    if dry_run and payload.get("dry_run") is not True:
        return (
            "Error: the gateway did not confirm the preview: it may have written "
            "the catalog, check the dashboard"
        )
    added, removed = payload.get("added"), payload.get("removed")
    first = payload.get("first_population")
    if first is True:
        # The gateway reports no added model for a first listing.
        text = (
            f"Catalog of '{provider_id}': first listing, "
            f"{payload['discovered']} models: {_names(payload.get('models'))}"
        )
    elif first is None and not added and not removed and payload.get("models"):
        # A gateway without first_population cannot tell a first listing
        # from no change: never claim "added: none".
        text = (
            f"Catalog of '{provider_id}': {payload['discovered']} models: "
            f"{_names(payload.get('models'))} (the gateway did not report "
            "what changed)"
        )
    else:
        text = (
            f"Catalog of '{provider_id}': {payload['discovered']} models; "
            f"added: {_names(added)}; removed: {_names(removed)}"
        )
    if dry_run:
        text += " (preview, nothing written)"
    if payload.get("removals_allowed") is False and first is not True:
        text += "; removals skipped: the provider's list is not trusted"
    return text


# ── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("models://list")
async def resource_models() -> str:
    """All available LLM models with their IDs."""
    return await list_models()


@mcp.resource("usage://current")
async def resource_usage() -> str:
    """Current month usage summary."""
    return await check_usage()


@mcp.resource("monitoring://status")
async def resource_monitoring() -> str:
    """Provider health and monitoring status."""
    return await provider_status()


# ── Prompts ──────────────────────────────────────────────────────────────────


@mcp.prompt()
def quick_chat(message: str, model: str = "claude-code:claude-haiku-4-5") -> str:
    """Quick one-shot chat — fast model, no system prompt.

    Args:
        message: What to ask the LLM
        model: Model to use (default: Claude Haiku for speed)
    """
    return f"Use the chat tool with model='{model}' and message='{message}'"


@mcp.prompt()
def deep_analysis(topic: str) -> str:
    """Deep analysis prompt — uses a powerful model with structured output.

    Args:
        topic: The topic or question to analyze in depth
    """
    return (
        f"Use the chat tool with:\n"
        f"- model: claude-code:claude-sonnet-4-6\n"
        f"- system_prompt: 'You are an expert analyst. Provide structured, "
        f"thorough analysis with clear sections and evidence-based reasoning.'\n"
        f"- message: '{topic}'\n"
        f"- max_tokens: 8192\n"
        f"- temperature: 0.3"
    )


@mcp.prompt()
def code_review(code: str, language: str = "python") -> str:
    """Code review prompt — uses a coding-optimized model.

    Args:
        code: The code to review
        language: Programming language (default: python)
    """
    return (
        f"Use the chat tool with:\n"
        f"- model: claude-code:claude-sonnet-4-6\n"
        f"- system_prompt: 'You are a senior {language} developer. Review this code "
        f"for bugs, security issues, performance, and best practices. Be specific and actionable.'\n"
        f"- message: '```{language}\\n{code}\\n```'\n"
        f"- temperature: 0.2"
    )


# ── Client Registry — push updates to consumers ─────────────────────────────

_DATA_DIR = data_dir()
# The registry holds the update prompts pushed to your own clients: it is
# operator data, kept outside the repository. See registry.example.json.
_REGISTRY_PATH = Path(
    os.environ.get("PLUGBOARD_REGISTRY_PATH") or (_DATA_DIR / "registry.json")
).expanduser()
_TRACKING_DB = _DATA_DIR / "client_updates.db"

_TRACKING_CREATE = """
CREATE TABLE IF NOT EXISTS applied_updates (
    client_id   TEXT NOT NULL,
    prompt_id   TEXT NOT NULL,
    applied_at  TEXT NOT NULL,
    applied_by  TEXT NOT NULL DEFAULT 'agent',
    notes       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (client_id, prompt_id)
);
"""
_TRACKING_SCHEMA_READY = False


def _tracking_conn() -> sqlite3.Connection:
    private_sqlite_file(_TRACKING_DB)
    conn = sqlite3.connect(str(_TRACKING_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    global _TRACKING_SCHEMA_READY
    if not _TRACKING_SCHEMA_READY:
        conn.execute(_TRACKING_CREATE)
        conn.commit()
        _TRACKING_SCHEMA_READY = True
    return conn


_LEGACY_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "registry.json"
)


def _load_registry() -> dict:
    path = _REGISTRY_PATH
    if not path.exists() and _LEGACY_REGISTRY_PATH.exists():
        log.warning(
            "Update registry read from its old location %s: move it to %s "
            "(or set PLUGBOARD_REGISTRY_PATH)",
            _LEGACY_REGISTRY_PATH,
            _REGISTRY_PATH,
        )
        path = _LEGACY_REGISTRY_PATH
    if not path.exists():
        log.warning("No update registry at %s: clients see no updates", path)
        return {"version": "unknown", "prompts": [], "missing": str(path)}
    return json.loads(path.read_text(encoding="utf-8"))


def _missing_registry_message(registry: dict) -> str:
    return (
        f"The update registry is missing on the server ({registry['missing']}): "
        "updates cannot be checked. Ask the operator to restore it "
        "(see PLUGBOARD_REGISTRY_PATH)."
    )


def _targets(prompt: dict, client_id: str) -> bool:
    """Whether the update ``prompt`` is aimed at ``client_id``.

    ``clients`` must be a list: a string typed by mistake would otherwise
    match any substring of a client ID.
    """
    clients = prompt.get("clients")
    return isinstance(clients, list) and client_id in clients


def _client_prompt(registry: dict, prompt_id: str, client_id: str) -> dict | None:
    """The update ``prompt_id`` aimed at ``client_id``, or None.

    Another client's update answers like a missing one: no oracle on its IDs.
    """
    return next(
        (
            p
            for p in registry["prompts"]
            if p["id"] == prompt_id and _targets(p, client_id)
        ),
        None,
    )


def _get_applied(client_id: str) -> set[str]:
    conn = _tracking_conn()
    try:
        rows = conn.execute(
            "SELECT prompt_id FROM applied_updates WHERE client_id = ?",
            (client_id,),
        ).fetchall()
        return {r["prompt_id"] for r in rows}
    finally:
        conn.close()


@mcp.tool()
async def check_updates(client_id: str = "") -> str:
    """Check for pending Plugboard updates that need to be applied to your project.

    Call this at the START of every session to see if there are breaking changes
    or new features to integrate. CRITIQUE updates must be applied immediately.
    The project is the client of this server's key (PLUGBOARD_API_KEY).

    Args:
        client_id: Optional; if given, it must be the client of this server's key
    """
    try:
        client_id = await _registry_client(client_id)
    except CallerIdentityError as exc:
        return f"Error: {exc}"
    registry = _load_registry()
    if registry.get("missing"):
        return _missing_registry_message(registry)
    applied = _get_applied(client_id)

    pending = [
        p
        for p in registry["prompts"]
        if _targets(p, client_id) and p["id"] not in applied
    ]

    if not pending:
        return (
            f"No pending updates for '{client_id}' (Plugboard v{registry['version']})"
        )

    lines = [
        f"**{len(pending)} update(s) pending** for '{client_id}' "
        f"(Plugboard v{registry['version']})\n",
    ]

    critiques = [p for p in pending if p["impact"] == "CRITIQUE"]
    importants = [p for p in pending if p["impact"] == "IMPORTANT"]
    evolutions = [p for p in pending if p["impact"] == "EVOLUTION"]

    if critiques:
        lines.append("**CRITIQUE (apply IMMEDIATELY):**")
        for p in critiques:
            lines.append(f"  - `{p['id']}` — {p['title']}")
        lines.append("")

    if importants:
        lines.append("**IMPORTANT:**")
        for p in importants:
            lines.append(f"  - `{p['id']}` — {p['title']}")
        lines.append("")

    if evolutions:
        lines.append("**EVOLUTION (when ready):**")
        for p in evolutions:
            lines.append(f"  - `{p['id']}` — {p['title']}")
        lines.append("")

    lines.append("Use `get_update_prompt(prompt_id)` to get the full instructions.")
    return "\n".join(lines)


@mcp.tool()
async def get_update_prompt(prompt_id: str) -> str:
    """Get the full technical prompt for a specific update.

    Returns the complete instructions: what changed in Plugboard,
    which files to modify in your project, exact code changes,
    and how to verify it works.

    Apply the changes, then call mark_applied(prompt_id). Only the updates
    for the client of this server's key (PLUGBOARD_API_KEY) can be read.

    Args:
        prompt_id: The update ID (e.g. CP-001, CP-002)
    """
    try:
        client_id = await _caller_client_id()
    except CallerIdentityError as exc:
        return f"Error: {exc}"
    registry = _load_registry()

    prompt = _client_prompt(registry, prompt_id, client_id)
    if not prompt:
        return f"Error: prompt '{prompt_id}' not found in registry"

    lines = [
        f"# [{prompt['id']}] {prompt['title']}",
        f"**Impact:** {prompt['impact']}",
        f"**Version:** Plugboard {prompt['version']}",
        f"**Date:** {prompt['date']}",
        f"**Client:** {client_id}",
        "",
        "## Summary",
        prompt["summary"],
        "",
        "## Changes to apply",
    ]

    for i, c in enumerate(prompt["changes"], 1):
        lines.append(f"\n### {i}. [{c['action'].upper()}] `{c['file']}`")
        lines.append(c["description"])
        if "detail" in c:
            lines.append(f"\n```\n{c['detail']}\n```")
        if "old" in c:
            lines.append("\n**Replace:**")
            for old in c["old"]:
                lines.append(f"  - `{old}`")
            lines.append("\n**With:**")
            for new in c["new"]:
                lines.append(f"  + `{new}`")

    if prompt.get("mcp_server"):
        mcp_cfg = prompt["mcp_server"]
        lines.append("\n## MCP Server config")
        lines.append("```json")
        lines.append(json.dumps({"mcpServers": {"plugboard": mcp_cfg}}, indent=2))
        lines.append("```")

    if prompt.get("verification"):
        lines.append("\n## Verification")
        lines.append(prompt["verification"])

    lines.append("\n---")
    lines.append(f'After applying, call: `mark_applied("{prompt_id}")`')

    return "\n".join(lines)


@mcp.tool()
async def mark_applied(prompt_id: str, notes: str = "", client_id: str = "") -> str:
    """Mark an update as applied for your project. This prevents it from showing up again.

    Call this AFTER you have successfully applied and tested the changes.
    The project is the client of this server's key (PLUGBOARD_API_KEY).

    Args:
        prompt_id: The update ID that was applied (e.g. CP-001)
        notes: Optional notes about the application (e.g. 'applied with minor adaptation')
        client_id: Optional; if given, it must be the client of this server's key
    """
    try:
        client_id = await _registry_client(client_id)
    except CallerIdentityError as exc:
        return f"Error: {exc}"
    registry = _load_registry()
    prompt = _client_prompt(registry, prompt_id, client_id)
    if not prompt:
        return f"Error: prompt '{prompt_id}' not found in registry"

    # Check if already applied
    applied = _get_applied(client_id)
    if prompt_id in applied:
        return f"Already applied: '{prompt_id}' was already marked as done for '{client_id}'"

    # Record
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _tracking_conn()
    try:
        conn.execute(
            "INSERT INTO applied_updates (client_id, prompt_id, applied_at, notes) VALUES (?, ?, ?, ?)",
            (client_id, prompt_id, now, notes),
        )
        conn.commit()
    finally:
        conn.close()

    # Check remaining
    remaining = [
        p
        for p in registry["prompts"]
        if _targets(p, client_id) and p["id"] not in applied and p["id"] != prompt_id
    ]

    if remaining:
        return (
            f"Marked `{prompt_id}` as applied for '{client_id}'.\n"
            f"**{len(remaining)} update(s) still pending.** "
            "Use `check_updates()` to see them."
        )
    return f"Marked `{prompt_id}` as applied for '{client_id}'. All updates are now applied!"


@mcp.tool()
async def list_applied(client_id: str = "") -> str:
    """List all updates that have been applied for your project.

    The project is the client of this server's key (PLUGBOARD_API_KEY).

    Args:
        client_id: Optional; if given, it must be the client of this server's key
    """
    try:
        client_id = await _registry_client(client_id)
    except CallerIdentityError as exc:
        return f"Error: {exc}"
    conn = _tracking_conn()
    try:
        rows = conn.execute(
            "SELECT prompt_id, applied_at, notes FROM applied_updates WHERE client_id = ? ORDER BY applied_at",
            (client_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return f"No updates applied yet for '{client_id}'"

    lines = [f"**{len(rows)} update(s) applied for '{client_id}':**\n"]
    for r in rows:
        line = f"  - `{r['prompt_id']}` — applied {r['applied_at']}"
        if r["notes"]:
            line += f" ({r['notes']})"
        lines.append(line)

    return "\n".join(lines)


@mcp.resource("updates://pending")
async def resource_pending_updates() -> str:
    """Pending updates for the client of this server's key."""
    try:
        client_id = await _caller_client_id()
    except CallerIdentityError as exc:
        return f"Error: {exc}"
    registry = _load_registry()
    if registry.get("missing"):
        return _missing_registry_message(registry)
    applied = _get_applied(client_id)
    pending = [
        p
        for p in registry["prompts"]
        if _targets(p, client_id) and p["id"] not in applied
    ]

    lines = [f"**Plugboard v{registry['version']}** — Pending updates\n"]
    if pending:
        lines.append(f"**{client_id}** — {len(pending)} pending:")
        for p in pending:
            lines.append(f"  [{p['impact']}] `{p['id']}` — {p['title']}")
    else:
        lines.append(f"**{client_id}** — all up to date")
    lines.append("")
    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────


def main():
    """Run the MCP server (stdio transport for Claude Desktop / IDE integration)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
