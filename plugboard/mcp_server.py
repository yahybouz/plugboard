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
import hmac
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Literal

import aiohttp
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from plugboard.legacy_connectors import is_removed_model, REMOVED_CONNECTOR_ERROR
from plugboard.storage import data_dir

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

_BASE_URL = os.environ.get("PLUGBOARD_URL", "http://localhost:8765")
_API_KEY = os.environ.get("PLUGBOARD_API_KEY", "")
_ADMIN_KEY = os.environ.get("PLUGBOARD_ADMIN_KEY", "")
_TIMEOUT = int(os.environ.get("PLUGBOARD_MCP_TIMEOUT", "900"))
_DECISION_TIMEOUT = max(1, min(_TIMEOUT, 45))
_DECISION_RESPONSE_LIMIT = 512 * 1024
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
    headers: dict[str, str] = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


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
                return {"error": f"HTTP {resp.status}: {text[:300]}"}
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
                return {"error": f"HTTP {resp.status}: {text[:300]}"}
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
                return f"Error: HTTP {resp.status}: {text[:300]}"
            reasoning_chunks: List[str] = []
            async for line in resp.content:
                text = line.decode("utf-8", errors="replace").strip()
                if text.startswith("data: ") and text != "data: [DONE]":
                    try:
                        data = json.loads(text[6:])
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
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
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
async def check_updates(client_id: str) -> str:
    """Check for pending Plugboard updates that need to be applied to your project.

    Call this at the START of every session to see if there are breaking changes
    or new features to integrate. CRITIQUE updates must be applied immediately.

    Args:
        client_id: Your project identifier, as listed in the registry (e.g. my-app)
    """
    registry = _load_registry()
    if registry.get("missing"):
        return (
            f"The update registry is missing on the server ({registry['missing']}): "
            "updates cannot be checked. Ask the operator to restore it "
            "(see PLUGBOARD_REGISTRY_PATH)."
        )
    applied = _get_applied(client_id)

    pending = [
        p
        for p in registry["prompts"]
        if client_id in p.get("clients", []) and p["id"] not in applied
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

    Apply the changes, then call mark_applied(client_id, prompt_id).

    Args:
        prompt_id: The update ID (e.g. CP-001, CP-002)
    """
    registry = _load_registry()

    prompt = next((p for p in registry["prompts"] if p["id"] == prompt_id), None)
    if not prompt:
        return f"Error: prompt '{prompt_id}' not found in registry"

    lines = [
        f"# [{prompt['id']}] {prompt['title']}",
        f"**Impact:** {prompt['impact']}",
        f"**Version:** Plugboard {prompt['version']}",
        f"**Date:** {prompt['date']}",
        f"**Clients:** {', '.join(prompt['clients'])}",
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
    lines.append(f'After applying, call: `mark_applied(client_id, "{prompt_id}")`')

    return "\n".join(lines)


@mcp.tool()
async def mark_applied(client_id: str, prompt_id: str, notes: str = "") -> str:
    """Mark an update as applied for your project. This prevents it from showing up again.

    Call this AFTER you have successfully applied and tested the changes.

    Args:
        client_id: Your project identifier, as listed in the registry (e.g. my-app)
        prompt_id: The update ID that was applied (e.g. CP-001)
        notes: Optional notes about the application (e.g. 'applied with minor adaptation')
    """
    # Verify prompt exists
    registry = _load_registry()
    prompt = next((p for p in registry["prompts"] if p["id"] == prompt_id), None)
    if not prompt:
        return f"Error: prompt '{prompt_id}' not found in registry"
    if client_id not in prompt.get("clients", []):
        return f"Error: prompt '{prompt_id}' does not target client '{client_id}'"

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
        if client_id in p.get("clients", [])
        and p["id"] not in applied
        and p["id"] != prompt_id
    ]

    if remaining:
        return (
            f"Marked `{prompt_id}` as applied for '{client_id}'.\n"
            f"**{len(remaining)} update(s) still pending.** "
            f'Use `check_updates("{client_id}")` to see them.'
        )
    return f"Marked `{prompt_id}` as applied for '{client_id}'. All updates are now applied!"


@mcp.tool()
async def list_applied(client_id: str) -> str:
    """List all updates that have been applied for a client.

    Args:
        client_id: Your project identifier, as listed in the registry (e.g. my-app)
    """
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
    """All pending updates across all clients."""
    registry = _load_registry()
    clients = set()
    for p in registry["prompts"]:
        clients.update(p.get("clients", []))

    lines = [f"**Plugboard v{registry['version']}** — Pending updates\n"]
    for cid in sorted(clients):
        applied = _get_applied(cid)
        pending = [
            p
            for p in registry["prompts"]
            if cid in p.get("clients", []) and p["id"] not in applied
        ]
        if pending:
            lines.append(f"**{cid}** — {len(pending)} pending:")
            for p in pending:
                lines.append(f"  [{p['impact']}] `{p['id']}` — {p['title']}")
        else:
            lines.append(f"**{cid}** — all up to date")
        lines.append("")

    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────


def main():
    """Run the MCP server (stdio transport for Claude Desktop / IDE integration)."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
