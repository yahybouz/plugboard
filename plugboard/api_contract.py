"""The client API contract: what an integrator may rely on.

This module describes the public HTTP API (the routes a client key calls) as
an OpenAPI 3.1 document, built from the Pydantic models below. It is the
single source of the contract:

* ``make contract`` writes it to ``docs/api/openapi.json`` (versioned);
* ``GET /v1/openapi.json`` serves it from a running instance;
* ``tests/test_api_contract.py`` fails when the routes, the committed file
  or the real responses drift from it.

The models only document: the route handlers do not validate against them,
so OpenAI-compatible fields the contract does not name still pass through
(``extra="allow"``). Admin routes are not part of this contract; they are
documented by the admin-only ``/openapi.json``.

Versioning: ``CONTRACT_VERSION`` follows semantic versioning on its own.
Removing or renaming a field, a route or a status code is a major change;
adding optional fields or routes is a minor one.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Tuple, Type, Union

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.0.0"
_JSON = "application/json"


class _Open(BaseModel):
    """An object that may carry fields beyond the ones documented here."""

    model_config = ConfigDict(extra="allow")


# ── Chat completions ─────────────────────────────────────────────────────────


class ChatMessage(_Open):
    role: str = Field(description="system, user, assistant, tool (OpenAI roles).")
    content: Optional[Union[str, List[Dict[str, Any]]]] = Field(
        default=None,
        description="Text, or OpenAI content parts (text, image_url).",
    )


class ChatCompletionRequest(_Open):
    """OpenAI ``POST /v1/chat/completions`` body, plus two Plugboard fields."""

    model: str = Field(
        description=(
            "`provider:model` (see GET /v1/models), or a bare model id, which "
            "is routed to the first active provider that serves it."
        ),
        examples=["anthropic-direct:claude-sonnet-5-5"],
    )
    messages: List[ChatMessage] = Field(min_length=1)
    stream: bool = Field(
        default=False, description="true answers with text/event-stream (SSE)."
    )
    temperature: Optional[float] = Field(
        default=None,
        description=(
            "Removed before forwarding for models that reject sampling "
            "parameters (Claude Opus 4.7 and later, Sonnet 5+, Haiku 5+)."
        ),
    )
    top_p: Optional[float] = None
    max_tokens: Optional[int] = Field(
        default=None,
        description="Renamed to max_completion_tokens for OpenAI o-series models.",
    )
    max_completion_tokens: Optional[int] = None
    stop: Optional[Union[str, List[str]]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    response_format: Optional[Dict[str, Any]] = None
    cache: bool = Field(
        default=True,
        description="Plugboard: false bypasses the response cache for this call.",
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Plugboard: the gateway keeps the conversation and prepends it, so "
            "only the new turn is sent. Same as the X-LLM-Session-ID header."
        ),
    )


class AssistantMessage(_Open):
    role: str = "assistant"
    content: Optional[str] = None
    reasoning: Optional[str] = Field(
        default=None, description="Reasoning text, for models that expose it."
    )
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ChatChoice(_Open):
    index: int
    message: AssistantMessage
    finish_reason: Optional[str] = None


class TokenUsage(_Open):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class Billing(BaseModel):
    """What the call cost, as recorded against the caller's key."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    model: str = Field(description="Model id that served the call, after fallback.")
    pricing_month: str = Field(examples=["2026_10"])
    price_key: Optional[str] = Field(
        default=None,
        description="Admin keys only: the pricing-grid entry that priced the call.",
    )


class ChatCompletion(_Open):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]
    usage: Optional[TokenUsage] = None
    billing: Optional[Billing] = Field(
        default=None, description="Present when the call was billed to a key."
    )


class ChunkChoice(_Open):
    index: int
    delta: Dict[str, Any]
    finish_reason: Optional[str] = None


class StreamUsage(TokenUsage):
    cost_usd: Optional[float] = Field(
        default=None, description="Added when the gateway injects the usage chunk."
    )


class ChatCompletionChunk(_Open):
    """One `data:` event of a streamed completion (SSE).

    The stream ends with `data: [DONE]`. A chunk carrying `usage` (and
    `cost_usd` when the gateway computed it) always comes before `[DONE]`.
    """

    id: str
    object: str = "chat.completion.chunk"
    created: Optional[int] = None
    model: Optional[str] = None
    choices: List[ChunkChoice]
    usage: Optional[StreamUsage] = None


# ── Models ───────────────────────────────────────────────────────────────────


class ModelEntry(_Open):
    id: str = Field(description="Pass it as `model` to the chat endpoint.")
    object: str = "model"
    owned_by: Optional[str] = Field(default=None, description="Provider id.")
    display_name: Optional[str] = None
    family: Optional[str] = None
    variant: Optional[str] = None
    internet_access: Optional[bool] = None
    connected: Optional[bool] = Field(
        default=None, description="false when the last probe could not reach it."
    )
    last_probed_at: Optional[str] = None
    account_compatible: Optional[bool] = Field(
        default=None, description="Whether the calling key may use this model."
    )


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: List[ModelEntry]


class ModelListV2(ModelList):
    api_version: Literal["2"] = "2"


class ModelProfile(_Open):
    """Static knowledge about a model: capabilities, benchmarks, cost tier."""

    provider: Optional[str] = None
    family: Optional[str] = None
    tier: Optional[str] = None
    description: Optional[str] = None
    release_date: Optional[str] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    capabilities: List[str] = []
    benchmarks: Dict[str, Any] = {}
    cost_tier: Optional[str] = None
    best_for: List[str] = []


class ModelWithProfile(ModelEntry):
    info: Optional[ModelProfile] = None


class ModelProfileList(BaseModel):
    object: Literal["list"] = "list"
    data: List[ModelWithProfile]


class ModelProfileListV2(ModelProfileList):
    api_version: Literal["2"] = "2"


class ModelInfo(ModelProfile):
    model_key: str = Field(description="Profile entry the model id matched.")
    matched_from: str
    info_source: str


# ── Preflight ────────────────────────────────────────────────────────────────


class PreflightItem(BaseModel):
    provider: str
    model: str


class PreflightRequest(BaseModel):
    items: List[PreflightItem]


class PreflightResult(BaseModel):
    provider: str
    model: str
    ok: bool
    latency_ms: int
    error: Optional[str] = None


class PreflightResponse(BaseModel):
    results: List[PreflightResult]


# ── Caller account ───────────────────────────────────────────────────────────


class UsageSummary(_Open):
    total_entries: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    by_model: Dict[str, Any] = {}


class ClientInfo(_Open):
    """Who the key belongs to, its limits and this month's spend.

    Every field but `client_id` is absent for an unauthenticated call.
    """

    client_id: Optional[str]
    key_id: Optional[str] = None
    monthly_limit_usd: Optional[float] = Field(default=None, description="0 = none.")
    allowed_models: Optional[List[str]] = Field(
        default=None, description="Empty = every model."
    )
    rpm_limit: Optional[int] = Field(default=None, description="0 = none.")
    org_id: Optional[str] = None
    current_month: Optional[str] = None
    usage: Optional[UsageSummary] = None
    remaining_usd: Optional[float] = Field(
        default=None, description="null when the key has no monthly limit."
    )


class ClientKey(BaseModel):
    key_id: str
    key_display: str = Field(description="Prefix of the key followed by ***.")
    label: str
    is_active: bool
    created_at: str


class UsageEntry(_Open):
    model: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[float] = None


class ClientUsage(BaseModel):
    month: Optional[str] = Field(default=None, examples=["2026_10"])
    client_id: Optional[str] = None
    entries: List[UsageEntry]
    summary: UsageSummary


# ── Health, errors ───────────────────────────────────────────────────────────


class Health(BaseModel):
    ok: bool
    version: str
    providers: int = Field(description="Number of active providers.")
    active: List[str] = Field(description="Ids of the active providers.")
    auth_disabled: Optional[bool] = Field(
        default=None, description="Present (true) when client auth is off."
    )
    dev_mode: Optional[bool] = None


class ErrorResponse(_Open):
    """FastAPI's error body. Upstream timeouts (503) add `retry_after` and a
    Retry-After header; a failing CLI provider (502) adds its exit details."""

    detail: Union[str, List[Any], Dict[str, Any]]
    retry_after: Optional[int] = None


# ── Routes ───────────────────────────────────────────────────────────────────

_AUTH_ERRORS = {
    "401": "Missing or invalid API key.",
    "429": "Rate limit or monthly spending limit exceeded.",
}
_CHAT_ERRORS = {
    **_AUTH_ERRORS,
    "403": "The key may not use this model.",
    "413": "Request body larger than MAX_BODY_BYTES (10 MB by default).",
    "502": "The provider failed or answered with an error.",
    "503": "Provider timeout (retry_after) or too many concurrent requests.",
}
_CHAT_HEADERS = {
    "X-Cache": "HIT, MISS or SEMANTIC-HIT-<layer>.",
    "X-Cost-USD": "Cost of the call, when billed (non-streamed answers).",
    "X-Prompt-Tokens": "Prompt tokens billed (non-streamed answers).",
    "X-Completion-Tokens": "Completion tokens billed (non-streamed answers).",
    "X-Request-ID": "Correlation id, also written to the gateway logs.",
}

# (method, path, summary, request model, response model, errors, needs a key)
_Route = Tuple[
    str, str, str, Optional[Type[BaseModel]], Type[BaseModel], Dict[str, str], bool
]
_ROUTES: List[_Route] = [
    ("post", "/v1/chat/completions", "Create a chat completion (OpenAI-compatible)",
     ChatCompletionRequest, ChatCompletion, _CHAT_ERRORS, True),
    ("post", "/v2/chat/completions", "Same as v1, with the X-API-Version: 2 header",
     ChatCompletionRequest, ChatCompletion, _CHAT_ERRORS, True),
    ("get", "/v1/models", "List the models the gateway serves",
     None, ModelList, _AUTH_ERRORS, True),
    ("get", "/v2/models", "Same as v1, with api_version",
     None, ModelListV2, _AUTH_ERRORS, True),
    ("get", "/v1/models/benchmarks", "List the models with their profile",
     None, ModelProfileList, _AUTH_ERRORS, True),
    ("get", "/v2/models/benchmarks", "Same as v1, with api_version",
     None, ModelProfileListV2, _AUTH_ERRORS, True),
    ("get", "/v1/models/{model_id}/info", "Profile of one model",
     None, ModelInfo, {**_AUTH_ERRORS, "404": "No profile for this model."}, True),
    ("post", "/v1/preflight", "Check that provider/model pairs answer",
     PreflightRequest, PreflightResponse, _AUTH_ERRORS, True),
    ("get", "/v1/me", "The calling key: limits and this month's usage",
     None, ClientInfo, _AUTH_ERRORS, True),
    ("get", "/v1/me/keys", "The calling client's keys (masked)",
     None, ClientKey, _AUTH_ERRORS, True),
    ("get", "/usage/me", "The calling client's usage entries for a month",
     None, ClientUsage, _AUTH_ERRORS, True),
    ("get", "/health", "Liveness and active providers",
     None, Health, {}, False),
    ("get", "/v1/openapi.json", "This contract (OpenAPI 3.1)",
     None, BaseModel, {}, False),
]  # fmt: skip

# Routes whose 200 body is a JSON array of the response model.
_ARRAY_RESPONSES = {"/v1/me/keys"}

#: (METHOD, path) of every route in the contract, as the contract writes them.
PUBLIC_ROUTES = {(method.upper(), path) for method, path, *_ in _ROUTES}

#: Response model of each route, for the tests that check real answers.
RESPONSE_MODELS = {
    (method.upper(), path): resp
    for method, path, _s, _r, resp, *_ in _ROUTES
    if resp is not BaseModel
}


def _schema_ref(model: Type[BaseModel], components: Dict[str, Any]) -> Dict[str, Any]:
    """Register `model` (and the models it uses) and return a $ref to it."""
    schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
    for name, sub in schema.pop("$defs", {}).items():
        components.setdefault(name, sub)
    components.setdefault(model.__name__, schema)
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _parameters(path: str) -> List[Dict[str, Any]]:
    if "{model_id}" in path:
        return [
            {
                "name": "model_id",
                "in": "path",
                "required": True,
                "description": "May contain slashes and colons.",
                "schema": {"type": "string"},
            }
        ]
    if path == "/usage/me":
        return [
            {
                "name": "month",
                "in": "query",
                "required": False,
                "description": "YYYY_MM; the current month when omitted.",
                "schema": {"type": "string"},
            }
        ]
    return []


def _ok_response(
    path: str, resp: Type[BaseModel], components: Dict[str, Any]
) -> Dict[str, Any]:
    if path == "/v1/openapi.json":
        schema: Dict[str, Any] = {"type": "object"}
    elif path in _ARRAY_RESPONSES:
        schema = {"type": "array", "items": _schema_ref(resp, components)}
    else:
        schema = _schema_ref(resp, components)
    ok: Dict[str, Any] = {
        "description": "OK",
        "content": {_JSON: {"schema": schema}},
    }
    if path.endswith("/chat/completions"):
        _schema_ref(ChatCompletionChunk, components)
        ok["content"]["text/event-stream"] = {
            "schema": {
                "type": "string",
                "description": (
                    "When stream is true: `data: <ChatCompletionChunk>` events, "
                    "then `data: [DONE]`. The usage chunk comes before [DONE]."
                ),
            }
        }
        ok["headers"] = {
            name: {"description": text, "schema": {"type": "string"}}
            for name, text in _CHAT_HEADERS.items()
        }
    return ok


def _operation_id(method: str, path: str) -> str:
    words = [w.strip("{}") for w in path.strip("/").replace(".", "_").split("/")]
    return "_".join([method] + [w for w in words if w])


def build_openapi() -> Dict[str, Any]:
    """The client API contract as an OpenAPI 3.1 document."""
    components: Dict[str, Any] = {}
    paths: Dict[str, Any] = {}
    for method, path, summary, req, resp, errors, needs_key in _ROUTES:
        op: Dict[str, Any] = {
            "summary": summary,
            "operationId": _operation_id(method, path),
        }
        params = _parameters(path)
        if params:
            op["parameters"] = params
        if req is not None:
            op["requestBody"] = {
                "required": True,
                "content": {_JSON: {"schema": _schema_ref(req, components)}},
            }
        responses: Dict[str, Any] = {"200": _ok_response(path, resp, components)}
        if errors:
            error_ref = _schema_ref(ErrorResponse, components)
            for code, text in errors.items():
                responses[code] = {
                    "description": text,
                    "content": {_JSON: {"schema": error_ref}},
                }
        op["responses"] = responses
        op["security"] = [{"bearer": []}] if needs_key else []
        paths.setdefault(path, {})[method] = op

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Plugboard client API",
            "version": CONTRACT_VERSION,
            "description": (
                "OpenAI-compatible gateway API for client keys. "
                "Integration guide: docs/api/README.md."
            ),
        },
        "servers": [{"url": "http://localhost:8765", "description": "Local install"}],
        "components": {
            "securitySchemes": {
                "bearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "A client key (pb_...) or the instance API_KEY.",
                }
            },
            "schemas": dict(sorted(components.items())),
        },
        "paths": dict(sorted(paths.items())),
    }
