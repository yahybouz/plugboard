"""Client-key-only OpenAI-compatible embeddings, served by a local Ollama engine.

Admission is separate from chat and never falls back to it:

- the requested ``model`` must be one of the exact ``provider_id:native_model``
  identities listed in ``PLUGBOARD_EMBEDDING_MODELS`` (empty = route closed);
- its owner is the registered provider whose id prefixes it, never a guess;
  that provider must be active, supported, ``sovereign`` and bound to a
  loopback address — the destination comes from the admin-registered record,
  never from the request;
- the native model must be installed on the engine (``GET /api/tags``), its
  digest is returned so the caller can pin the model it indexed with;
- the calling key's ``allowed_models``, budget and rpm limit apply.

The engine call is ``POST /api/embed`` with ``truncate: false``: an input the
model cannot hold is refused, never cut. Vectors are checked (count, one
dimension, finite values, non-zero norm) before anything is returned.
Usage is recorded as ``embedding:<native>`` at cost 0 (local engine);
logs carry the request id, key id, counts and timings, never the input text.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import math
import os
import struct
import time
from urllib.parse import urlsplit

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from plugboard.apikeys import get_key_record
from plugboard.embeddings_schema import EmbeddingRequest
from plugboard.ratelimit import check_client_rate_limit

MODELS_ENV = "PLUGBOARD_EMBEDDING_MODELS"
TIMEOUT_ENV = "PLUGBOARD_EMBEDDING_TIMEOUT_S"
DEFAULT_TIMEOUT = 60.0
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_INPUTS = 256
MAX_INPUT_CHARS = 16_384
MAX_DIMENSIONS = 8_192
MAX_UPSTREAM_BYTES = 64 * 1024 * 1024
MAX_TOKENS = 2**31 - 1
PRICE_SOURCE = "local:embeddings"
_LOOPBACK_NAMES = {"localhost"}

log = logging.getLogger(__name__)

# Dimensions seen on served calls, per identity (process memory, best effort).
_KNOWN_DIMENSIONS: dict[str, int] = {}

CONCURRENCY_ENV = "PLUGBOARD_EMBEDDING_CONCURRENCY"
DEFAULT_CONCURRENCY = 2
_SLOTS: dict[str, object] = {}


def _concurrency() -> int:
    try:
        value = int(os.environ.get(CONCURRENCY_ENV, "") or DEFAULT_CONCURRENCY)
    except ValueError:
        return DEFAULT_CONCURRENCY
    return value if 1 <= value <= 64 else DEFAULT_CONCURRENCY


def _try_acquire_slot() -> bool:
    """Global, non-blocking cap on in-flight engine calls (small hosts): a
    full engine answers 429 instead of queueing CPU/RAM work."""
    limit = _concurrency()
    state = _SLOTS.setdefault("state", {"busy": 0})
    if state["busy"] >= limit:
        return False
    state["busy"] += 1
    return True


def _release_slot() -> None:
    state = _SLOTS.setdefault("state", {"busy": 0})
    state["busy"] = max(0, state["busy"] - 1)


class EngineError(Exception):
    """Engine failure mapped to a bounded HTTP answer (no upstream body)."""

    def __init__(self, status: int, detail: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.detail = detail
        self.reason = reason


def allowed_identities() -> frozenset[str]:
    raw = os.environ.get(MODELS_ENV, "")
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _timeout() -> float:
    try:
        value = float(os.environ.get(TIMEOUT_ENV, "") or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if 0 < value <= 600 else DEFAULT_TIMEOUT


def _is_loopback_base(base_url: str) -> bool:
    try:
        parts = urlsplit(base_url)
        host = (parts.hostname or "").lower()
        _ = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return False
    if parts.query or parts.fragment or parts.path.strip("/"):
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        return False
    return _is_operator_ollama_url(parts)


def _is_operator_ollama_url(parts) -> bool:
    """The operator's own ``OLLAMA_URL`` (deployment env, not caller input) is
    also a local engine: in a container the host daemon is reached through a
    private bridge address, e.g. ``http://172.30.114.1:11434``. Only an exact
    scheme/host/port match on a private IP literal is accepted."""
    raw = os.environ.get("OLLAMA_URL", "").strip()
    if not raw:
        return False
    try:
        ref = urlsplit(raw.rstrip("/"))
        ref_host = (ref.hostname or "").lower()
        if not ipaddress.ip_address(ref_host).is_private:
            return False
        return (
            ref.scheme == parts.scheme
            and ref_host == (parts.hostname or "").lower()
            and (ref.port or None) == (parts.port or None)
            and not ref.path.strip("/")
        )
    except ValueError:
        return False


def resolve_route(model: str) -> tuple[dict, str]:
    """(provider, native_model) for an explicitly listed identity, else HTTP error."""
    from plugboard.config import load_providers
    from plugboard.legacy_connectors import is_removed_provider
    from plugboard.model_identity import registered_model_owner

    if model not in allowed_identities():
        raise HTTPException(404, "Embedding model not available")
    providers = load_providers()
    owner = registered_model_owner(model, (p.get("id", "") for p in providers))
    if owner is None:
        raise HTTPException(404, "Embedding model not available")
    provider = next(p for p in providers if p.get("id") == owner)
    native = model[len(owner) + 1 :]
    if not native or "cloud" in native.lower():
        raise HTTPException(404, "Embedding model not available")
    if not provider.get("is_active", True) or is_removed_provider(provider):
        raise HTTPException(503, "Embedding provider unavailable")
    if provider.get("provider_type") != "sovereign" or not _is_loopback_base(
        str(provider.get("base_url") or "")
    ):
        log.error("embeddings refused provider=%s reason=not_local_engine", owner)
        raise HTTPException(503, "Embedding provider is not a local engine")
    return provider, native


def require_embedding_key(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    record = get_key_record(token.strip()) if scheme.lower() == "bearer" else None
    if not record:
        raise HTTPException(401, "Client API key required for embeddings")
    return record


class _EmbeddingRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request: Request):
            # Authenticate before reading a potentially sensitive body.
            require_embedding_key(request)
            chunks = bytearray()
            async for chunk in request.stream():
                if len(chunks) + len(chunk) > MAX_BODY_BYTES:
                    raise HTTPException(413, "Embedding request too large")
                chunks.extend(chunk)
            request._body = bytes(chunks)
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI's default errors would echo the submitted text.
                return JSONResponse(
                    status_code=422, content={"detail": "Invalid embedding request"}
                )

        return safe_handler


router = APIRouter(
    prefix="/v1/embeddings", route_class=_EmbeddingRoute, tags=["embeddings"]
)


def _inputs(body: EmbeddingRequest) -> list[str]:
    inputs = [body.input] if isinstance(body.input, str) else list(body.input)
    if not inputs or len(inputs) > MAX_INPUTS:
        raise HTTPException(422, f"input must hold 1 to {MAX_INPUTS} strings")
    for text in inputs:
        if not text.strip() or len(text) > MAX_INPUT_CHARS:
            raise HTTPException(
                422,
                f"each input must be non-blank and at most {MAX_INPUT_CHARS} characters",
            )
    if body.dimensions is not None:
        raise HTTPException(
            422, "dimensions is not supported; the model's own size is returned"
        )
    return inputs


async def _read_json(resp: aiohttp.ClientResponse):
    import json

    buf = bytearray()
    async for chunk in resp.content.iter_chunked(65_536):
        buf.extend(chunk)
        if len(buf) > MAX_UPSTREAM_BYTES:
            raise EngineError(502, "Invalid embedding engine response", "too_large")
    try:
        return json.loads(bytes(buf))
    except ValueError:
        raise EngineError(
            502, "Invalid embedding engine response", "not_json"
        ) from None


async def _call(session, method: str, url: str, **kwargs):
    try:
        async with session.request(
            method, url, allow_redirects=False, **kwargs
        ) as resp:
            if resp.status != 200:
                if resp.status == 400:
                    raise EngineError(
                        422,
                        "Embedding engine rejected the input",
                        f"http_{resp.status}",
                    )
                raise EngineError(
                    502,
                    f"Embedding engine error (HTTP {resp.status})",
                    f"http_{resp.status}",
                )
            return await _read_json(resp)
    except (asyncio.TimeoutError, TimeoutError):
        raise EngineError(504, "Embedding engine timed out", "timeout") from None
    except aiohttp.ClientError as exc:
        raise EngineError(
            503, "Embedding engine unavailable", type(exc).__name__
        ) from None


def _digest(tags, native: str) -> str:
    models = tags.get("models") if isinstance(tags, dict) else None
    if not isinstance(models, list):
        raise EngineError(502, "Invalid embedding engine response", "tags_shape")
    for item in models:
        if isinstance(item, dict) and native in (item.get("name"), item.get("model")):
            digest = item.get("digest")
            if isinstance(digest, str) and digest:
                return digest
            raise EngineError(502, "Invalid embedding engine response", "tags_digest")
    raise EngineError(
        404, "Embedding model not installed on the engine", "not_installed"
    )


def validate_vectors(raw, expected: int) -> list[list[float]]:
    """Exactly ``expected`` vectors of one dimension, finite and non-zero."""
    vectors = raw.get("embeddings") if isinstance(raw, dict) else None
    if not isinstance(vectors, list) or len(vectors) != expected:
        raise EngineError(502, "Invalid embedding engine response", "cardinality")
    dims = None
    clean: list[list[float]] = []
    for vector in vectors:
        if not isinstance(vector, list) or not 0 < len(vector) <= MAX_DIMENSIONS:
            raise EngineError(502, "Invalid embedding engine response", "dimension")
        if dims is None:
            dims = len(vector)
        elif len(vector) != dims:
            raise EngineError(502, "Invalid embedding engine response", "dimension")
        values = []
        for x in vector:
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                raise EngineError(502, "Invalid embedding engine response", "value")
            try:
                x = float(x)
            except OverflowError:  # a JSON integer beyond float range
                raise EngineError(
                    502, "Invalid embedding engine response", "value"
                ) from None
            if not math.isfinite(x):
                raise EngineError(
                    502, "Invalid embedding engine response", "non_finite"
                )
            values.append(x)
        if not any(values):
            raise EngineError(502, "Invalid embedding engine response", "zero_norm")
        clean.append(values)
    return clean


def _prompt_tokens(raw: dict, inputs: list[str]) -> int:
    count = raw.get("prompt_eval_count")
    if type(count) is int and 0 <= count <= MAX_TOKENS:
        return count
    from plugboard.tokenizer import count_tokens

    return sum(count_tokens(t) for t in inputs)


def _encode(vector: list[float], encoding: str):
    if encoding == "base64":
        try:
            packed = struct.pack(f"<{len(vector)}f", *vector)
        except (OverflowError, struct.error):  # finite but beyond float32
            raise EngineError(
                502, "Invalid embedding engine response", "float32_range"
            ) from None
        return base64.b64encode(packed).decode()
    return vector


def _record_usage(record: dict, native: str, provider_id: str, tokens: int) -> bool:
    from plugboard import billing, usage

    try:
        usage.append_usage(
            client_id=record["client_id"],
            model=f"embedding:{native}",
            provider_id=provider_id,
            prompt_tokens=tokens,
            completion_tokens=0,
            pricing_month=billing.current_pricing_month(),
            cost_usd=0.0,
            key_id=record.get("key_id", ""),
            price_source=PRICE_SOURCE,
        )
    except Exception as exc:  # noqa: BLE001 - the engine already answered
        log.error(
            "embeddings served but not recorded key_id=%s model=%s error=%s",
            record.get("key_id", ""),
            native,
            type(exc).__name__,
        )
        return False
    return True


@router.post("")
async def create_embeddings(
    body: EmbeddingRequest, record: dict = Depends(require_embedding_key)
):
    from plugboard.app import _check_spending_limit
    from plugboard.model_identity import model_access_matches
    from plugboard.ollama import ollama_headers

    inputs = _inputs(body)
    provider, native = resolve_route(body.model)
    owner = provider["id"]
    if not model_access_matches(record.get("allowed_models") or [], owner, native):
        raise HTTPException(403, "Embedding model not permitted for this key")
    _check_spending_limit(record["client_id"], key_record=record)
    rpm = int(record.get("rpm_limit", 0) or 0)
    if rpm and not check_client_rate_limit(
        record.get("key_id", record["client_id"]), rpm
    ):
        raise HTTPException(429, "Client rate limit exceeded")

    base = str(provider["base_url"]).rstrip("/")
    # PLUGBOARD_OLLAMA_TOKEN for an Ollama behind an authenticating relay:
    # empty unless ``base`` is exactly the operator's OLLAMA_URL.
    headers = ollama_headers(base)
    started = time.perf_counter()
    if not _try_acquire_slot():
        raise HTTPException(429, "Embedding engine busy, retry shortly")
    try:
        timeout = aiohttp.ClientTimeout(total=_timeout())
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            digest = _digest(
                await _call(session, "GET", f"{base}/api/tags", headers=headers),
                native,
            )
            raw = await _call(
                session,
                "POST",
                f"{base}/api/embed",
                headers=headers,
                json={"model": native, "input": inputs, "truncate": False},
            )
        vectors = validate_vectors(raw, len(inputs))
        # Encode before usage is recorded: an unservable vector is never billed.
        data = [
            {
                "object": "embedding",
                "index": i,
                "embedding": _encode(v, body.encoding_format),
            }
            for i, v in enumerate(vectors)
        ]
    except EngineError as exc:
        log.warning(
            "embeddings failed provider=%s model=%s inputs=%d reason=%s key_id=%s elapsed_ms=%d",
            owner,
            native,
            len(inputs),
            exc.reason,
            record.get("key_id", ""),
            (time.perf_counter() - started) * 1000,
        )
        raise HTTPException(exc.status, exc.detail) from None
    finally:
        _release_slot()

    tokens = _prompt_tokens(raw, inputs)
    recorded = _record_usage(record, native, owner, tokens)
    dims = len(vectors[0])
    _KNOWN_DIMENSIONS[body.model] = dims
    log.info(
        "embeddings served provider=%s model=%s inputs=%d dims=%d tokens=%d key_id=%s elapsed_ms=%d",
        owner,
        native,
        len(inputs),
        dims,
        tokens,
        record.get("key_id", ""),
        (time.perf_counter() - started) * 1000,
    )
    return {
        "object": "list",
        "data": data,
        "model": body.model,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        "plugboard": {
            "provider_id": owner,
            "native_model": native,
            "digest": digest,
            "dimensions": dims,
            "cost_usd": 0.0,
            "usage_recorded": recorded,
        },
    }


@router.get("/models")
async def list_embedding_models(record: dict = Depends(require_embedding_key)):
    """Identities listed in PLUGBOARD_EMBEDDING_MODELS this key may use.

    Unregistered owners and identities the key may not use are left out.
    ``available`` says whether the route resolves (owner registered, active,
    loopback local engine); it does not probe the engine. ``dimensions`` is
    known once a call was served since the last restart, else null.
    """
    from plugboard.config import load_providers
    from plugboard.model_identity import model_access_matches, registered_model_owner

    provider_ids = [p.get("id", "") for p in load_providers()]
    allowed = record.get("allowed_models") or []
    models = []
    for model in sorted(allowed_identities()):
        owner = registered_model_owner(model, provider_ids)
        if owner is None or not model_access_matches(
            allowed, owner, model[len(owner) + 1 :]
        ):
            continue
        try:
            resolve_route(model)
            available = True
        except HTTPException:
            available = False
        models.append(
            {
                "id": model,
                "provider_id": owner,
                "available": available,
                "dimensions": _KNOWN_DIMENSIONS.get(model),
            }
        )
    return {"object": "list", "data": models}
