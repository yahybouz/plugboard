"""
Request router — forward /v1/chat/completions to the right backend.

Model format: "provider_id:real_model_name"
Examples:
  "claude-via-claude-cli:claude-sonnet-4-6"
  "my-openai:gpt-4o"
  "anthropic-direct:claude-3-5-sonnet-20241022"

If only one provider is configured and the model contains no known provider_id prefix,
the default provider is used with the full model string as-is.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import os
import re
import time as _time
import uuid as _uuid
from typing import Any, AsyncIterator, Callable, Dict, Iterator, List, Optional

import aiohttp

from plugboard import metrics
from plugboard.alerts import record_provider_failure, record_provider_success
from plugboard.config import load_providers
from plugboard.logging_config import _request_id_var
from plugboard.endpoints import openai_url
from plugboard.legacy_connectors import (
    is_removed_catalogue_model,
    is_removed_model,
    is_removed_provider,
    require_supported_model,
    require_supported_provider,
)
from plugboard.model_identity import (
    catalogue_native_model,
    observation_owner,
    published_model_id,
    registered_model_owner,
)

log = logging.getLogger(__name__)

_RETRYABLE = {429, 500, 502, 503, 529}
_TIMEOUT = float(900)  # Allow long-running direct CLI requests.


# BENCH-006 closed: OpenAI's o-series (o1/o3/o4-mini/...) deprecated
# `max_tokens` in favour of `max_completion_tokens`. Forwarders pass through
# `max_tokens` from clients written against the legacy spec, which now
# returns HTTP 400 from upstream. We auto-translate so callers don't have
# to know which model family enforces which name.
_O_SERIES_MODEL_PREFIXES = ("openai/o", "o1", "o3", "o4")


def _normalize_payload_for_o_series(
    model: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Translate `max_tokens` → `max_completion_tokens` for o-series models.

    Returns a new dict; never mutates the caller's payload. No-op for any
    other model family. Idempotent — if both keys are already present,
    `max_completion_tokens` wins (assumed canonical).
    """
    if not model or "max_tokens" not in payload:
        return payload
    name = model.lower()
    if not any(name.startswith(p) for p in _O_SERIES_MODEL_PREFIXES):
        return payload
    converted = {**payload}
    if "max_completion_tokens" not in converted:
        converted["max_completion_tokens"] = converted["max_tokens"]
    converted.pop("max_tokens", None)
    return converted


# Claude Opus 4.7 and later, Sonnet 5+, Haiku 5+ and the Fable / Mythos models
# reject sampling parameters: Anthropic answers HTTP 400 ("`temperature` is
# deprecated for this model"), on the native API and on its OpenAI-compatible
# endpoint alike. Older Claude models (Opus 4.6, Sonnet 4.6, Haiku 4.5, 3.x)
# still accept them. Names are matched on the upstream model id, whatever the
# provider in front of it: claude-opus-4-7, anthropic/claude-opus-4.7, ...
_CLAUDE_SAMPLING_PARAMS = ("temperature", "top_p", "top_k")
_CLAUDE_MODEL_RE = re.compile(
    r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2})(?!\d))?"
)
_CLAUDE_FIRST_WITHOUT_SAMPLING = (4, 7)


def _claude_rejects_sampling(model: str) -> bool:
    name = model.rsplit("/", 1)[-1].lower().replace(".", "-")
    match = _CLAUDE_MODEL_RE.search(name)
    if not match:
        return False
    family, major, minor = match.group(1), int(match.group(2)), match.group(3)
    if family in ("fable", "mythos"):
        return True
    return (major, int(minor or 0)) >= _CLAUDE_FIRST_WITHOUT_SAMPLING


def _drop_unsupported_sampling_params(
    model: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    """Remove temperature / top_p / top_k for Claude models that reject them.

    Returns a new dict; never mutates the caller's payload. No-op for any
    other model, or when the payload carries none of these parameters.
    """
    present = [k for k in _CLAUDE_SAMPLING_PARAMS if k in payload]
    if not present or not model or not _claude_rejects_sampling(model):
        return payload
    log.debug("Dropping %s for %s (not accepted by this model)", present, model)
    return {k: v for k, v in payload.items() if k not in _CLAUDE_SAMPLING_PARAMS}


def _adapt_payload(model: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Model-family request adjustments applied before any HTTP forward."""
    return _drop_unsupported_sampling_params(
        model, _normalize_payload_for_o_series(model, payload)
    )


def _read_env_float(name: str, default: float, *, min_value: float = 0.0) -> float:
    """Read float env var with validation and sane fallback."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning(
            "Invalid %s=%r (float expected) — using default %.3f", name, raw, default
        )
        return default
    if value < min_value:
        log.warning(
            "Invalid %s=%.3f (must be >= %.3f) — using default %.3f",
            name,
            value,
            min_value,
            default,
        )
        return default
    return value


def _read_retry_delays() -> List[float]:
    """Read retry backoff delays from env with robust fallback.

    PLUGBOARD_RETRY_DELAYS supports comma-separated seconds, e.g.:
      "2,5,15" (default)
      "0.05,0.1,0.2" (fast CI)
    """
    raw = os.environ.get("PLUGBOARD_RETRY_DELAYS", "").strip()
    if not raw:
        return [2.0, 5.0, 15.0]
    out: List[float] = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            value = float(token)
        except ValueError:
            log.warning(
                "Invalid PLUGBOARD_RETRY_DELAYS token %r (float expected) — using default",
                token,
            )
            return [2.0, 5.0, 15.0]
        if value < 0:
            log.warning(
                "Invalid PLUGBOARD_RETRY_DELAYS token %r (must be >= 0) — using default",
                token,
            )
            return [2.0, 5.0, 15.0]
        out.append(value)
    return out if out else [2.0, 5.0, 15.0]


_ERROR_RATE_WEIGHT = _read_env_float("PLUGBOARD_LB_ERROR_RATE_WEIGHT", 5.0)
_INFLIGHT_WEIGHT = _read_env_float("PLUGBOARD_LB_INFLIGHT_WEIGHT", 0.35)
_DELAYS = _read_retry_delays()
_IN_FLIGHT_BY_PROVIDER: Dict[str, int] = {}
_LEGACY_PROVIDER_PREFIX_TO_AUTH_SOURCE: Dict[str, str] = {
    # BENCH-004: avoid fallback/latency when clients still send legacy
    # subprocess prefixes.
    "gemini-cli": "subprocess:gemini",
    "gemini-cli-subprocess": "subprocess:gemini",
    "codex-cli": "subprocess:codex",
    "codex-cli-subprocess": "subprocess:codex",
    "claude-cli": "subprocess:claude",
    "claude-cli-subprocess": "subprocess:claude",
    "Gemini": "subprocess:gemini",
    "Codex": "subprocess:codex",
    "claude-code": "subprocess:claude",
}


@contextmanager
def _track_in_flight(provider_id: str) -> Iterator[None]:
    """Track in-flight requests per provider to improve load balancing decisions."""
    _IN_FLIGHT_BY_PROVIDER[provider_id] = _IN_FLIGHT_BY_PROVIDER.get(provider_id, 0) + 1
    try:
        yield
    finally:
        remaining = _IN_FLIGHT_BY_PROVIDER.get(provider_id, 0) - 1
        if remaining > 0:
            _IN_FLIGHT_BY_PROVIDER[provider_id] = remaining
        else:
            _IN_FLIGHT_BY_PROVIDER.pop(provider_id, None)


def _provider_lb_score(provider_id: str, stats: Dict[str, Dict[str, Any]]) -> float:
    """Lower score is better."""
    s = stats.get(provider_id, {})
    latency = s.get("latency_avg", 1.0) or 1.0
    reqs = s.get("requests", 0)
    errs = s.get("errors", 0)
    error_rate = errs / reqs if reqs > 0 else 0.0
    inflight = _IN_FLIGHT_BY_PROVIDER.get(provider_id, 0)
    return (
        latency
        * (1 + error_rate * _ERROR_RATE_WEIGHT)
        * (1 + inflight * _INFLIGHT_WEIGHT)
    )


def _chat_candidate(
    provider: Dict[str, Any], real_model: str
) -> tuple[Dict[str, Any], str]:
    """Guard the actual selected model, including defaults and normalized aliases."""
    from plugboard.decisions import is_decision_model

    require_supported_provider(provider)
    require_supported_model(
        real_model or provider.get("default_model", ""), effective_name=True
    )
    if is_decision_model(
        real_model or provider.get("default_model", ""),
        provider_ids=(provider["id"],),
        effective_name=True,
    ):
        raise ValueError("Use /v1/decisions for decision models")
    return provider, real_model


@dataclass(frozen=True)
class _ModelRoute:
    """A selected owner and its opaque native model name."""

    provider: Dict[str, Any]
    native_model: str

    def candidate(self) -> tuple[Dict[str, Any], str]:
        return self.provider, self.native_model


def _model_route(provider: Dict[str, Any], native_model: str) -> _ModelRoute:
    # The selected provider never comes from an alias target. Catalogue hints
    # are owner-qualified native IDs, captured before display cleaning.
    native_model = native_model or provider.get("default_model", "")
    _chat_candidate(provider, native_model)
    owner_prefix = provider["id"] + ":"
    promoted = _resolve_model_alias(owner_prefix + native_model)
    if promoted.startswith(owner_prefix):
        native_model = promoted[len(owner_prefix) :]
    else:
        # Keep the final decision defense for malformed/legacy cache hints,
        # without using their display syntax to select another owner.
        _chat_candidate(provider, promoted)
    _chat_candidate(provider, native_model)
    return _ModelRoute(provider, native_model)


def _select_request(
    model_str: str,
) -> tuple[List[Dict[str, Any]], Optional[_ModelRoute]]:
    """Select from the original request before any promotion or display alias."""
    from plugboard.decisions import is_decision_model

    if not isinstance(model_str, str):
        raise ValueError("model must be a string")
    # Unambiguous native decisions and removed IDs never require a private
    # provider-store read. Colons may belong to a registered owner or native tag.
    require_supported_model(model_str, effective_name=True)
    if ":" not in model_str.partition("/")[0] and is_decision_model(
        model_str, effective_name=True
    ):
        raise ValueError("Use /v1/decisions for decision models")
    providers = load_providers()
    require_supported_model(model_str, provider_ids=(p["id"] for p in providers))
    owner = registered_model_owner(model_str, (p["id"] for p in providers))
    if owner is not None:
        require_supported_provider(next(p for p in providers if p["id"] == owner))
    active = [
        p for p in providers if p.get("is_active", True) and not is_removed_provider(p)
    ]
    owner = registered_model_owner(model_str, (p["id"] for p in active))
    if owner is not None:
        provider = next(p for p in active if p["id"] == owner)
        return active, _model_route(provider, model_str[len(owner) + 1 :])
    if is_decision_model(model_str, effective_name=True):
        raise ValueError("Use /v1/decisions for decision models")
    if not active:
        for provider in providers:
            if is_removed_provider(provider):
                require_supported_provider(provider)
        raise ValueError("No active providers configured")

    # Presentation/legacy aliases apply only when no registered ID matched.
    for alias, canonical in _CLEAN_ALIAS_TO_PROVIDER.items():
        if model_str.startswith(alias):
            for provider in active:
                if provider["id"] == canonical:
                    return active, _model_route(provider, model_str[len(alias) :])
    prefix, sep, native_model = model_str.partition(":")
    wanted_auth = _LEGACY_PROVIDER_PREFIX_TO_AUTH_SOURCE.get(prefix)
    if sep and wanted_auth and native_model:
        for provider in active:
            if provider.get("auth_source") == wanted_auth:
                return active, _model_route(provider, native_model)
    return active, None


def resolve_provider_and_model(model_str: str) -> tuple[Dict[str, Any], str]:
    """Resolve one route; registered provider identity precedes all rewrites."""
    active, selected = _select_request(model_str)
    if selected is not None:
        return selected.candidate()

    # Preserve single-route configured-model lookup, after original selection.
    requested_clean = _clean_model_id(model_str)
    for provider in active:
        configured = [
            m["id"]
            for m in provider.get("models") or []
            if isinstance(m, dict) and m.get("id")
        ] + list(provider.get("extra_model_ids") or [])
        for native_model in configured:
            if _clean_model_id(f"{provider['id']}:{native_model}") == requested_clean:
                return _model_route(provider, native_model).candidate()

    return _model_route(active[0], model_str).candidate()


def _extra_headers(provider: Dict[str, Any]) -> Dict[str, str]:
    """Fixed headers a provider needs on every call.

    They come from its preset (``anthropic-version`` for Anthropic, the
    Copilot editor headers) and from the provider record, which wins.
    """
    headers: Dict[str, str] = {}
    preset_key = provider.get("preset_key")
    if preset_key:
        from plugboard.direct_providers import PRESETS

        preset = PRESETS.get(str(preset_key)) or {}
        headers.update(
            {str(k): str(v) for k, v in (preset.get("extra_headers") or {}).items()}
        )
    own = provider.get("extra_headers")
    if isinstance(own, dict):
        headers.update({str(k): str(v) for k, v in own.items()})
    return headers


def _format_auth_header(provider: Dict[str, Any], token: str) -> Dict[str, str]:
    """Format the auth header dict from the resolved token + provider config.

    The provider's fixed extra headers are included (see ``_extra_headers``).
    """
    header_name = provider.get("auth_header_name") or "Authorization"
    header_prefix = provider.get("auth_header_prefix")
    if header_prefix is None:
        header_prefix = "Bearer"
    value = f"{header_prefix} {token}" if header_prefix else token
    return {**_extra_headers(provider), header_name: value}


def _build_auth_header(provider: Dict[str, Any]) -> Dict[str, str]:
    """Sync version — uses bearer_token only. Used by discover_models.

    For runtime requests (forward, forward_stream), use _build_auth_header_async
    which supports auth_source=cli:* delegation.
    """
    return _format_auth_header(provider, provider.get("bearer_token", ""))


async def _build_auth_header_async(provider: Dict[str, Any]) -> Dict[str, str]:
    """Async version — supports auth_source=cli:<name> OAuth delegation.

    If auth_source starts with 'cli:', fetches a fresh token from the
    corresponding CLI loader (Codex, Gemini, gh, etc.). Otherwise falls
    back to the stored bearer_token.
    """
    auth_source = provider.get("auth_source", "")
    # Both cli:* (external CLI delegation) and broker:* (plugboard stored OAuth)
    # are resolved through get_loader()
    if auth_source.startswith("cli:") or auth_source.startswith("broker:"):
        from plugboard.cli_auth import get_loader

        # cli:foo → pass "foo"; broker:foo → pass full "broker:foo"
        loader_key = (
            auth_source.removeprefix("cli:")
            if auth_source.startswith("cli:")
            else auth_source
        )
        loader = get_loader(loader_key)
        if loader is None:
            raise RuntimeError(f"Unknown auth_source: {auth_source}")
        bundle = await loader.get_token()
        if bundle is None or not bundle.access_token:
            hint = (
                "Run the corresponding CLI login first"
                if auth_source.startswith("cli:")
                else "Connect via dashboard (Providers → Add Direct)"
            )
            raise RuntimeError(f"Auth not available for {auth_source}. {hint}.")
        return _format_auth_header(provider, bundle.access_token)
    # Fallback: stored bearer_token (Ollama and API key providers).
    return _format_auth_header(provider, provider.get("bearer_token", ""))


_CLEAN_ALIAS_TO_PROVIDER = {
    "Codex:": "chatgpt-via-codex-cli",
    "claude-code:": "claude-via-claude-cli",
    "Gemini:": "google-gemini-via-gemini-cli",
}


def _resolve_all_providers(model_str: str) -> List[tuple[Dict[str, Any], str]]:
    """Use the same selection/admission contract, retaining unqualified fallback."""
    active, selected = _select_request(model_str)
    if selected is not None:
        return [selected.candidate()]
    results = [_model_route(provider, model_str).candidate() for provider in active]
    if len(results) > 1:
        stats = metrics.get_stats()
        results.sort(key=lambda item: _provider_lb_score(item[0]["id"], stats))
    return results


# (provider, real_model) that actually answered the current request, after
# fallback and circuit-breaker rewrites. Billing reads it so the price is the
# one of the provider that served the call. Each request runs in its own
# task, so the value never leaks between requests.
_served_by: ContextVar[Optional[tuple]] = ContextVar(
    "plugboard_served_by", default=None
)


def served_by() -> Optional[tuple]:
    """(provider, real_model) that served the last forward() in this context."""
    return _served_by.get()


async def forward(
    body: Dict[str, Any],
    timeout: float = _TIMEOUT,
    *,
    route_guard: Optional[Callable[[Dict[str, Any], str], None]] = None,
) -> Dict[str, Any]:
    """
    Forward a /v1/chat/completions request to the resolved provider.
    Retries on transient errors per provider, then falls back to next provider.
    """
    from plugboard.circuit_breaker import (
        check_circuit,
        record_failure as cb_failure,
        record_success as cb_success,
    )

    _served_by.set(None)
    model_str = body.get("model", "")
    candidates = _resolve_all_providers(model_str)

    # Circuit breaker check — if model is open, try fallback
    circuit = check_circuit(model_str)
    if not circuit["allowed"] and circuit["fallback"]:
        # Rewrite model to fallback
        fallback_suffix = circuit["fallback"]
        log.warning("Circuit breaker: %s → fallback %s", model_str, fallback_suffix)
        # Try to find the fallback in available models
        for prov, rmodel in candidates:
            if fallback_suffix in rmodel:
                model_str = f"{prov['id']}:{rmodel}"
                body = {**body, "model": model_str}
                candidates = _resolve_all_providers(model_str)
                break

    last_err: Optional[Exception] = None
    # PERF-2: reuse the app-wide pooled session (keepalive + DNS cache)
    # instead of opening a new TCP+TLS connection per forwarded request.
    from plugboard.http_pool import get_session as _get_http_session

    session = await _get_http_session()
    for provider, real_model in candidates:
        if route_guard is not None:
            route_guard(provider, real_model)
        # Direct CLI providers short-circuit HTTP.
        auth_source = provider.get("auth_source", "")
        if auth_source.startswith("subprocess:"):
            try:
                kind = auth_source.removeprefix("subprocess:")
                from plugboard import cli_subprocess

                sub_body = {**body, "model": real_model}
                _t_cli = _time.perf_counter()
                try:
                    result = await cli_subprocess.complete(
                        kind,
                        sub_body,
                        timeout_s=timeout,
                    )
                except Exception:
                    metrics.record(
                        provider["id"], _time.perf_counter() - _t_cli, error=True
                    )
                    raise
                metrics.record(provider["id"], _time.perf_counter() - _t_cli)
                # CLI subprocess returns usage=0,0,0 since the upstream CLIs
                # don't report token counts. Estimate via tiktoken so clients
                # see a consistent usage block (identical to the streaming
                # path in forward_stream()).
                try:
                    usage = result.get("usage") or {}
                    if not usage.get("total_tokens"):
                        from plugboard.tokenizer import (
                            estimate_completion_tokens as _est_c,
                            estimate_prompt_tokens as _est_p,
                        )

                        p_tok = _est_p(body.get("messages", []))
                        c_tok = _est_c(result)
                        result["usage"] = {
                            "prompt_tokens": p_tok,
                            "completion_tokens": c_tok,
                            "total_tokens": p_tok + c_tok,
                        }
                except Exception as _exc:
                    log.debug("subprocess usage estimation failed: %s", _exc)
                try:
                    record_provider_success(provider["id"])
                    cb_success(real_model)
                except Exception:
                    pass
                _served_by.set((provider, real_model))
                return result
            except Exception as exc:
                last_err = exc
                log.warning("Subprocess provider %s failed: %s", provider["id"], exc)
                try:
                    from plugboard.model_quarantine import (
                        is_account_unsupported_error,
                        is_terminal_model_unavailable_error,
                        record_account_unsupported,
                        record_terminal_unavailable,
                    )

                    reason = str(exc)
                    if is_account_unsupported_error(reason):
                        record_account_unsupported(real_model, reason=reason[:200])
                        invalidate_models_cache()
                    elif is_terminal_model_unavailable_error(reason):
                        record_terminal_unavailable(
                            real_model,
                            reason=reason[:200],
                            terminal_reason="model_unavailable",
                        )
                        invalidate_models_cache()
                    cb_failure(real_model)
                    await record_provider_failure(provider["id"])
                except Exception:
                    pass
                continue

        effective_timeout = timeout
        url = openai_url(provider["base_url"], "chat/completions")
        try:
            auth_header = await _build_auth_header_async(provider)
        except RuntimeError as exc:
            last_err = exc
            log.warning("Auth resolution failed for %s: %s", provider["id"], exc)
            continue
        headers = {
            "Content-Type": "application/json",
            **auth_header,
            "X-Request-ID": _request_id_var.get("-"),
            "X-Session-ID": _uuid.uuid4().hex,  # unique session per request
        }
        payload = _adapt_payload(real_model, {**body, "model": real_model})
        log.info("→ %s | model=%s | provider=%s", url, real_model, provider["id"])

        provider_failed = False
        with _track_in_flight(provider["id"]):
            for attempt, delay in enumerate([0] + _DELAYS):
                if delay:
                    log.warning(
                        "Retry %d/%d in %.3gs (provider=%s)",
                        attempt,
                        len(_DELAYS),
                        delay,
                        provider["id"],
                    )
                    await asyncio.sleep(delay)

                try:
                    req_t0 = _time.perf_counter()
                    async with session.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=effective_timeout),
                    ) as resp:
                        if resp.status in _RETRYABLE:
                            text = await resp.text()
                            last_err = RuntimeError(
                                f"[{provider['id']}] HTTP {resp.status}: {text[:200]}"
                            )
                            continue
                        if resp.status != 200:
                            text = await resp.text()
                            try:
                                metrics.record(provider["id"], 0.0, error=True)
                            except Exception:
                                pass
                            last_err = RuntimeError(
                                f"[{provider['id']}] HTTP {resp.status}: {text[:400]}"
                            )
                            provider_failed = True
                            break  # non-retryable error, try next provider
                        result = await resp.json()
                        elapsed = _time.perf_counter() - req_t0
                        try:
                            metrics.record(provider["id"], elapsed)
                        except Exception:
                            pass
                        try:
                            record_provider_success(provider["id"])
                            cb_success(real_model)
                        except Exception:
                            pass
                        _served_by.set((provider, real_model))
                        return result
                except asyncio.TimeoutError:
                    try:
                        metrics.record(provider["id"], 0.0, error=True)
                    except Exception:
                        pass
                    last_err = RuntimeError(
                        f"[{provider['id']}] timeout (>{effective_timeout}s) — model={real_model}"
                    )
                    log.warning(
                        "Timeout on %s (attempt %d/%d)",
                        provider["id"],
                        attempt + 1,
                        len(_DELAYS) + 1,
                    )
                    # Continue retries — don't break on timeout, the backend
                    # may have been temporarily overloaded (agents/sub-agents)
                    continue

                if provider_failed:
                    break

        # If we exhausted retries for this provider, record circuit breaker failure
        cb_failure(real_model)

        # Record error metrics and try next provider
        if not provider_failed:
            try:
                metrics.record(provider["id"], 0.0, error=True)
            except Exception:
                pass

        try:
            await record_provider_failure(provider["id"])
        except Exception:
            pass

        log.warning("Provider %s failed, trying next...", provider["id"])

    raise last_err or RuntimeError("All providers failed")


async def forward_stream(
    body: Dict[str, Any],
    timeout: float = _TIMEOUT,
    *,
    route_guard: Optional[Callable[[Dict[str, Any], str], None]] = None,
) -> AsyncIterator[bytes]:
    """
    Forward a streaming /v1/chat/completions request, yielding raw SSE chunks.
    No retry — cannot retry after streaming has begun.
    """
    model_str = body.get("model", "")
    provider, real_model = resolve_provider_and_model(model_str)
    if route_guard is not None:
        route_guard(provider, real_model)
    effective_timeout = timeout
    _served_by.set((provider, real_model))

    # Subprocess providers don't stream natively (CLIs return the full
    # response once). Emulate SSE by running the completion and chunking
    # the text into pseudo-deltas so the Playground works end-to-end.
    auth_source = provider.get("auth_source", "")
    if auth_source.startswith("subprocess:"):
        import json as _json
        from plugboard import cli_subprocess

        kind = auth_source.removeprefix("subprocess:")
        sub_body = {**body, "model": real_model, "stream": False}
        _t_cli = _time.perf_counter()
        try:
            result = await cli_subprocess.complete(
                kind,
                sub_body,
                timeout_s=timeout,
            )
            metrics.record(provider["id"], _time.perf_counter() - _t_cli)
        except Exception as exc:
            metrics.record(provider["id"], _time.perf_counter() - _t_cli, error=True)
            try:
                from plugboard.model_quarantine import (
                    is_account_unsupported_error,
                    is_terminal_model_unavailable_error,
                    record_account_unsupported,
                    record_terminal_unavailable,
                )

                reason = str(exc)
                if is_account_unsupported_error(reason):
                    record_account_unsupported(real_model, reason=reason[:200])
                    invalidate_models_cache()
                elif is_terminal_model_unavailable_error(reason):
                    record_terminal_unavailable(
                        real_model,
                        reason=reason[:200],
                        terminal_reason="model_unavailable",
                    )
                    invalidate_models_cache()
            except Exception:
                pass
            err_payload: Dict[str, Any] = {
                "error": {"message": str(exc), "type": "subprocess_error"}
            }
            from plugboard.cli_subprocess import CliSubprocessError

            if isinstance(exc, CliSubprocessError):
                err_payload["error"]["exit_code"] = exc.exit_code
                err_payload["error"]["stderr"] = exc.stderr
                err_payload["error"]["pid"] = exc.pid
                err_payload["error"]["provider_name"] = exc.provider_name
                err_payload["error"]["model_id"] = exc.model_id
            err_evt = _json.dumps(err_payload)
            yield f"data: {err_evt}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
            return
        content = result["choices"][0]["message"]["content"]
        model_name = result.get("model", f"{kind}-cli:{real_model}")
        completion_id = result.get("id", "chatcmpl-subproc")
        created = result.get("created", int(_time.time()))

        # Estimate tokens via tiktoken so the Playground can show them,
        # and compute cost from the current pricing grid.
        p_tok = 0
        c_tok = 0
        cost_usd = 0.0
        try:
            from plugboard import billing as _billing_mod
            from plugboard.tokenizer import (
                estimate_completion_tokens as _est_c,
                estimate_prompt_tokens as _est_p,
            )

            p_tok = _est_p(body.get("messages", []))
            c_tok = _est_c(result)
            # Resilient lookup (see billing.resolve_model_price_with_fallback):
            # walks back through previous snapshots if the current month
            # is sentinel or missing the model — avoids 0$ billing on
            # newly-released models the scraper hasn't priced yet.
            price_entry = _billing_mod.resolve_model_price_with_fallback(
                real_model, provider=provider
            )
            cost_usd = _billing_mod.calculate_cost(p_tok, c_tok, price_entry)
        except Exception as _exc:
            log.warning("subprocess stream billing estimation failed: %s", _exc)

        # Emit chunked SSE. For UX we split on whitespace so the Playground
        # feels a bit more alive, though the CLI already returned everything.
        tokens = content.split(" ")
        for i, tok in enumerate(tokens):
            delta = tok + (" " if i < len(tokens) - 1 else "")
            evt = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [
                    {"index": 0, "delta": {"content": delta}, "finish_reason": None}
                ],
            }
            yield f"data: {_json.dumps(evt)}\n\n".encode("utf-8")
        # Final chunk with usage (OpenAI stream_options.include_usage shape)
        final_evt = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": p_tok,
                "completion_tokens": c_tok,
                "total_tokens": p_tok + c_tok,
                "cost_usd": round(cost_usd, 6),
            },
        }
        yield f"data: {_json.dumps(final_evt)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        return

    url = openai_url(provider["base_url"], "chat/completions")
    auth_header = await _build_auth_header_async(provider)
    headers = {
        "Content-Type": "application/json",
        **auth_header,
        "X-Request-ID": _request_id_var.get("-"),
        "X-Session-ID": _uuid.uuid4().hex,  # unique session per request
    }
    payload = _adapt_payload(real_model, {**body, "model": real_model})

    log.info("→ stream %s | model=%s | provider=%s", url, real_model, provider["id"])

    # PERF-2: pooled session for streaming (same TCP/TLS reuse benefit).
    from plugboard.http_pool import get_session as _get_http_session

    session = await _get_http_session()
    async with session.post(
        url,
        headers=headers,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=effective_timeout),
    ) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"[{provider['id']}] HTTP {resp.status}: {text[:400]}")
        async for chunk in resp.content.iter_any():
            yield chunk


_models_cache: Optional[List[Dict[str, Any]]] = None
_models_cache_ts: float = 0
_models_cache_namespace: tuple = ()
_models_alias_cache: Dict[str, str] = {}
_MODELS_CACHE_TTL = 30.0  # seconds
_DISABLED_MODEL_IDS = {"claude-opus-4"}


def _model_denylist() -> set:
    """Static disabled ids + an admin-configurable denylist.

    ``PLUGBOARD_MODEL_DENYLIST`` is a comma-separated list of model SUFFIXES
    (the part after the provider prefix, e.g. ``minimax-m2.5:cloud``). It lets an
    operator keep a model OUT of /v1/models even though the Ollama Cloud
    auto-sync (C2) would otherwise re-surface it from the upstream catalogue —
    i.e. an intentional removal survives the next refresh, unlike quarantine
    (which only catches models that FAIL a probe)."""
    raw = os.environ.get("PLUGBOARD_MODEL_DENYLIST", "") or ""
    extra = {s.strip() for s in raw.split(",") if s.strip()}
    return _DISABLED_MODEL_IDS | extra


# mtime_ns of model_status.json at the last cache fill — used to detect changes.
# Using nanosecond resolution (st_mtime_ns) to catch rapid successive writes.
_model_status_mtime: int = 0


def invalidate_models_cache() -> None:
    """Clear the models cache (useful for tests and after provider changes)."""
    global _models_cache, _models_alias_cache, _models_cache_namespace
    _models_cache = None
    _models_alias_cache = {}
    _models_cache_namespace = ()


def _model_status_mtime_changed() -> bool:
    """Return True when model_status.json has been modified since last cache fill.

    Uses st_mtime_ns (nanosecond resolution) to detect rapid successive writes
    that would be indistinguishable with the 1-second resolution of getmtime().
    """
    import os as _os

    try:
        from plugboard.model_availability import _MODEL_STATUS_PATH

        mtime_ns = _os.stat(_MODEL_STATUS_PATH).st_mtime_ns
        return mtime_ns != _model_status_mtime
    except Exception:
        return False


def _resolve_model_alias(model_str: str) -> str:
    """Rewrite ``model_str`` through the alias map if it matches a promoted
    dated-canonical entry. Returns the original string when no alias hits.

    The alias cache is populated by :func:`list_models`. If the cache is
    cold (server just booted, no one called ``/v1/models`` yet) the alias
    lookup degrades to a no-op and the request uses ``model_str`` as is.
    """
    if not model_str or not _models_alias_cache:
        return model_str
    return _models_alias_cache.get(model_str, model_str)


def _subprocess_extra_models(
    provider: Dict[str, Any], persisted_ids: List[str]
) -> List[str]:
    """Return model IDs that LiteLLM tracks for this subprocess CLI but
    that are not yet in ``persisted_ids``.

    Read-only enrichment used by :func:`list_models` so the dashboard
    surfaces freshly-released models without the admin having to click
    "Discover" on every provider. Returns an empty list for any non
    subprocess provider, on any error, and when the pricing snapshot has
    no extras vs. the persisted list — i.e. it is strictly additive and
    cannot regress an admin's curated list.
    """
    auth_source = provider.get("auth_source", "") or ""
    if not auth_source.startswith("subprocess:"):
        return []
    kind = auth_source.removeprefix("subprocess:")
    if kind in {"codex", "claude", "gemini"}:
        # Subscription-backed CLIs are account-filtered by
        # model_availability.refresh_all_model_catalogs before models are
        # persisted. Do not surface raw pricing extras here, otherwise a model
        # can appear before the current account/subscription accepts it.
        return []
    try:
        from plugboard.model_catalog import discover_subprocess_models_from_pricing

        dynamic = discover_subprocess_models_from_pricing(kind)
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "_subprocess_extra_models: discovery failed for %s (%s)",
            provider.get("id"),
            exc,
        )
        return []
    persisted_set = set(persisted_ids)
    return [m for m in dynamic if m not in persisted_set]


def _claude_latest_n() -> int:
    try:
        return max(1, int(os.environ.get("PLUGBOARD_CLAUDE_LATEST_N", "2")))
    except (TypeError, ValueError):
        return 2


def _strip_date_tail(name: str) -> str:
    """Drop a trailing ``-YYYYMMDD`` snapshot tag (no regex dep)."""
    head, _, tail = name.rpartition("-")
    if head and len(tail) == 8 and tail.isdigit():
        return head
    return name


def _allowed_claude_suffixes(model_names, n: int) -> set:
    """Dynamic Claude whitelist: the N latest ``(major, minor)`` versions per
    family (opus / sonnet / haiku), parsed from the DISCOVERED ids — no hardcoded
    version pins, so a new Opus/Sonnet/Haiku release surfaces with zero code edit
    (C1, 2026-06-01). Dated YYYYMMDD snapshots collapse onto their (major,minor);
    ``dedupe_family_snapshots`` picks the canonical entry downstream."""
    from collections import defaultdict
    from plugboard.model_availability import (  # lazy import: avoid circular dep
        parse_family_and_version,
    )

    fam_versions: Dict[str, set] = defaultdict(set)
    parsed = []
    for name in model_names:
        if not any(k in name for k in ("opus", "sonnet", "haiku")):
            continue
        fam, ver = parse_family_and_version(_strip_date_tail(name))
        # Need a full (major, minor) to rank a family. A bare "claude-opus-4"
        # (e.g. left by stripping the date off "claude-opus-4-20250514") yields
        # an incomplete (4,) — skip it so it can't perturb the per-family sort
        # dedupe_family_snapshots handles such snapshots.
        if len(ver) < 2:
            continue
        mm = tuple(ver[:2])
        fam_versions[fam].add(mm)
        parsed.append((name, fam, mm))
    top = {
        fam: set(sorted(vers, reverse=True)[:n]) for fam, vers in fam_versions.items()
    }
    return {name for (name, fam, mm) in parsed if mm in top.get(fam, set())}


def _model_filter_mode() -> str:
    """``all`` (default): list every discovered model. ``curated``: keep a short
    list per built-in provider (see ``_is_whitelisted_model``).
    Set with PLUGBOARD_MODEL_FILTER."""
    mode = os.environ.get("PLUGBOARD_MODEL_FILTER", "").strip().lower()
    return "curated" if mode == "curated" else "all"


def _is_whitelisted_model(m: Dict[str, Any], claude_allowed=None) -> bool:
    """Curated filter (PLUGBOARD_MODEL_FILTER=curated) for built-in providers.

    Providers not listed here are never filtered.
    """
    mid = m.get("id", "")
    if ":" not in mid:
        return False
    provider_id = m.get("owned_by") or m.get("provider") or mid.split(":", 1)[0]
    model_name = (
        catalogue_native_model(m)
        if m.get("owned_by") or m.get("provider")
        else mid.split(":", 1)[1]
    )

    # Normalize provider ID
    pid_lower = provider_id.lower()

    # If this is not one of the standard providers or their cleaned names, allow it!
    standard_pids = {
        "claude-via-claude-cli",
        "claude-code",
        "anthropic",
        "chatgpt-via-codex-cli",
        "codex",
        "openai",
        "google-gemini-via-gemini-cli",
        "gemini",
        "google",
        "ollama",
    }
    if pid_lower not in standard_pids:
        return True

    # 1. Anthropic / Claude: the N latest (major, minor) per family.
    if pid_lower in {"claude", "anthropic", "claude-via-claude-cli", "claude-code"}:
        if not any(k in model_name for k in ("opus", "sonnet", "haiku")):
            return False
        # C1 (2026-06-01): DYNAMIC "N latest per family" instead of hardcoded
        # version pins. ``claude_allowed`` is precomputed once by list_models
        # from the discovered ids. If absent (direct/standalone call), allow the
        # family so a Claude model is never hidden by accident.
        if claude_allowed is None:
            # Only list_models precomputes the set; a direct/ad-hoc call lands
            # here. Allow (never hide a Claude model by accident) but flag it so
            # a future caller that forgets to pass the set is visible (C1 MAJOR-3).
            log.debug(
                "_is_whitelisted_model: no claude_allowed set — allowing %s", model_name
            )
            return True
        return model_name in claude_allowed

    # 2. Codex / OpenAI: GPT-5 and newer (including mini and codex variants).
    if pid_lower in {"codex", "openai", "chatgpt-via-codex-cli"}:
        return "gpt-5" in model_name or "gpt-6" in model_name or "gpt-7" in model_name

    # 3. Gemini / Google: Gemini 3 and newer.
    if pid_lower in {"gemini", "google", "google-gemini-via-gemini-cli"}:
        return "gemini-3" in model_name

    # 4. Ollama: cloud variants of the families in ollama._CLOUD_WHITELIST_FAMILIES.
    if pid_lower == "ollama":
        is_cloud = "cloud" in model_name or "thinking" in model_name
        if not is_cloud:
            return False

        lower_name = model_name.lower()
        # P3.1 (2026-06-02): SINGLE source of truth for the cloud families —
        # shared with the C2 auto-sync (ollama._CLOUD_WHITELIST_FAMILIES) so the
        # router filter and the discovery list can never silently diverge.
        from plugboard.ollama import _cloud_families  # lazy: avoid circular import

        return any(kw in lower_name for kw in _cloud_families())

    return True


# Vendor names the clean aliases (claude-code:, Codex:, Gemini:, ollama:) stand for.
_LEGACY_ALIAS_PREFIXES = frozenset(
    {
        "anthropic",
        "claude",
        "claude-via-claude-cli",
        "claude-code",
        "openai",
        "codex",
        "chatgpt",
        "chatgpt-via-codex-cli",
        "google",
        "gemini",
        "google-gemini-cli",
        "google-gemini-via-gemini-cli",
        "ollama",
    }
)


def _clean_model_id(mid: str) -> str:
    """Translate canonical provider prefixes to user-requested clean ones."""
    # If it already starts with a desired clean prefix, return it as-is
    for prefix in ["Gemini:", "Codex:", "claude-code:", "ollama:"]:
        if mid.startswith(prefix):
            return mid

    # Map known CLI providers directly
    if mid.startswith("google-gemini-via-gemini-cli:"):
        return mid.replace("google-gemini-via-gemini-cli:", "Gemini:", 1)
    if mid.startswith("chatgpt-via-codex-cli:"):
        return mid.replace("chatgpt-via-codex-cli:", "Codex:", 1)
    if mid.startswith("claude-via-claude-cli:"):
        return mid.replace("claude-via-claude-cli:", "claude-code:", 1)

    stripped = mid
    if ":" in stripped or "--" in stripped:
        pfx = ""
        rem = stripped
        if ":" in stripped:
            pfx, rem = stripped.split(":", 1)
        elif "--" in stripped:
            pfx, rem = stripped.split("--", 1)

        pfx_lower = pfx.lower()
        # A configured provider keeps its own id: guessing the vendor from the
        # model name sent anthropic-direct / agy / reseller models to the
        # claude-code or Codex alias, which then routed to another provider.
        if pfx and pfx_lower not in _LEGACY_ALIAS_PREFIXES:
            return f"{pfx}:{rem}"
        if pfx_lower in {"anthropic", "claude", "claude-via-claude-cli", "claude-code"}:
            return f"claude-code:{rem}"
        elif pfx_lower in {"openai", "codex", "chatgpt", "chatgpt-via-codex-cli"}:
            return f"Codex:{rem}"
        elif pfx_lower in {
            "google",
            "gemini",
            "google-gemini-cli",
            "google-gemini-via-gemini-cli",
        }:
            return f"Gemini:{rem}"
        elif pfx_lower in {"ollama"}:
            return f"ollama:{rem}"

        # Check in actual model identifier part
        rem_lower = rem.lower()
        if "claude" in rem_lower or "anthropic" in rem_lower:
            return f"claude-code:{rem}"
        elif "gpt" in rem_lower or "codex" in rem_lower:
            return f"Codex:{rem}"
        elif "gemini" in rem_lower:
            return f"Gemini:{rem}"
        elif "ollama" in rem_lower or any(
            kw in rem_lower
            for kw in {
                "deepseek",
                "kimi",
                "qwen",
                "glm",
                "gemma",
                "nemotron",
                "minimax",
                "gpt-oss",
            }
        ):
            return f"ollama:{rem}"

        if pfx:
            return f"{pfx}:{rem}"
        return rem

    # Fallback scanning of the entire string
    mid_lower = mid.lower()
    if "claude" in mid_lower or "anthropic" in mid_lower:
        return f"claude-code:{mid}"
    elif "gpt" in mid_lower or "codex" in mid_lower:
        return f"Codex:{mid}"
    elif "gemini" in mid_lower:
        return f"Gemini:{mid}"
    elif "ollama" in mid_lower or any(
        kw in mid_lower
        for kw in {
            "deepseek",
            "kimi",
            "qwen",
            "glm",
            "gemma",
            "nemotron",
            "minimax",
            "gpt-oss",
        }
    ):
        return f"ollama:{mid}"

    return mid


def _apply_exposure(
    models: List[Dict[str, Any]], providers: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Drop the models the exposure policy hides (``PLUGBOARD_MODEL_EXPOSE``).

    Default models and models already used are always listed.
    """
    from plugboard import model_exposure

    if model_exposure.mode() == "all":
        return models
    try:
        from plugboard.usage import get_all_used_models

        used = set(get_all_used_models())
    except Exception:  # noqa: BLE001 - usage store unavailable: protect nothing more
        used = set()
    defaults = {
        f"{p['id']}:{p['default_model']}"
        for p in providers
        if p.get("id") and p.get("default_model")
    }
    by_provider: Dict[str, List[Dict[str, Any]]] = {}
    provider_ids = tuple(p["id"] for p in providers)
    for item in models:
        owner = (
            item.get("owned_by")
            or registered_model_owner(item.get("id", ""), provider_ids)
            or ""
        )
        by_provider.setdefault(owner, []).append(item)
    hidden: set = set()
    for entries in by_provider.values():
        bare = {
            catalogue_native_model(item, provider_ids=provider_ids): item["id"]
            for item in entries
        }
        protected = {
            native
            for native, mid in bare.items()
            if mid in defaults or mid in used or native in used
        }
        for name in model_exposure.hidden_ids(list(bare), protected):
            hidden.add(bare[name])
    return [item for item in models if item.get("id") not in hidden]


async def list_models() -> List[Dict[str, Any]]:
    """Return all models from all active providers as OpenAI /v1/models format.

    MODEL-1/2: every entry is enriched with ``display_name`` / ``family`` /
    ``variant`` so a naive client that splits on ``:`` never loses a
    segment, and dated snapshots are dropped when a ``-latest`` alias
    of the same family already exists.
    """
    from plugboard.decisions import is_decision_model

    global _models_cache, _models_cache_ts, _model_status_mtime, _models_cache_namespace
    registry = load_providers()
    provider_ids = tuple(p["id"] for p in registry)
    namespace = tuple(
        sorted(
            (p["id"], bool(p.get("is_active", True)), is_removed_provider(p))
            for p in registry
        )
    )
    providers = [p for p in registry if not is_removed_provider(p)]
    now = _time.monotonic()
    cache_valid = (
        _models_cache is not None
        and (now - _models_cache_ts) < _MODELS_CACHE_TTL
        and not _model_status_mtime_changed()
    )
    if cache_valid and _models_cache_namespace == namespace:
        return [
            model for model in _models_cache if not is_removed_catalogue_model(model)
        ]

    models = []
    for p in providers:
        if not p.get("is_active", True):
            continue
        # Subprocess providers (claude-cli, codex-cli, gemini-cli) have no
        # HTTP /v1/models endpoint, so their persisted models list can lag
        # behind the real vendor catalog. Augment the persisted list at
        # boot dynamically (MODEL-2).
        persisted_ids = {
            m["id"]
            for m in (p.get("models") or [])
            if isinstance(m, dict) and m.get("id")
        }
        extra_ids = [m for m in p.get("extra_model_ids", []) if m not in persisted_ids]
        for mid in extra_ids:
            models.append(
                {
                    "id": f"{p['id']}:{mid}",
                    "object": "model",
                    "owned_by": p["id"],
                    "provider": p["id"],
                }
            )
        for pm in p.get("models") or []:
            if not isinstance(pm, dict) or not pm.get("id"):
                continue
            mid = pm["id"]
            models.append(
                {
                    "id": f"{p['id']}:{mid}",
                    "object": "model",
                    "owned_by": p["id"],
                    "provider": p["id"],
                    "internet_access": pm.get("internet_access"),
                }
            )
        # If no models list and no dynamic extras, expose default_model
        if not persisted_ids and not extra_ids and p.get("default_model"):
            models.append(
                {
                    "id": f"{p['id']}:{p['default_model']}",
                    "object": "model",
                    "owned_by": p["id"],
                }
            )

    # Enrich every entry with display_name / family / variant, then
    # drop dated snapshots that duplicate a -latest alias.
    from plugboard.model_catalog import (
        build_alias_map,
        dedupe_family_snapshots,
        enrich,
    )

    models = [
        m
        for m in models
        if not is_removed_model(
            m.get("id", ""), provider_ids=(p["id"] for p in providers)
        )
        and not is_decision_model(
            m.get("id", ""), provider_ids=(p["id"] for p in providers)
        )
    ]
    # Inexpressible identities must not suppress safe siblings during dedup or
    # exposure. Keep raw IDs for native metadata, health and promotion helpers;
    # the owner is part of the key because nested owners can share a raw ID.
    publication_ids = {
        (m["owned_by"], m["id"]): published_model_id(
            m["owned_by"], catalogue_native_model(m), provider_ids
        )
        for m in models
    }
    models = [
        m for m in models if publication_ids[(m["owned_by"], m["id"])] is not None
    ]
    models = dedupe_family_snapshots(enrich(models))
    models = _apply_exposure(models, providers)

    # Combine the static disable list with the dynamic quarantine set
    # populated by the nightly smoke test. A model lands here when it
    # has failed identification probes ``QUARANTINE_THRESHOLD`` times in
    # a row — typically a deprecation we missed or a vendor pulling the
    # rug under us. The next successful probe rehabilitates it.
    try:
        from plugboard.model_quarantine import get_quarantined_suffixes

        quarantined = get_quarantined_suffixes()
    except Exception as exc:  # noqa: BLE001
        log.debug("list_models: quarantine read failed (%s)", exc)
        quarantined = set()

    # Load persisted connectivity status from model_status.json.
    # Falls back gracefully to quarantine-only logic if file is absent.
    # When PLUGBOARD_IGNORE_MODEL_STATUS=true the file is never read —
    # this is the "medium" rollback level documented in .env.example.
    try:
        from plugboard.model_availability import (
            _IGNORE_MODEL_STATUS,
            load_model_status,
        )

        if _IGNORE_MODEL_STATUS:
            log.debug(
                "list_models: PLUGBOARD_IGNORE_MODEL_STATUS=true — skipping model_status.json"
            )
            status_models: Dict[str, Any] = {}
        else:
            model_status = load_model_status()
            status_models = model_status.get("models", {})
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "list_models: model_status load failed (%s) — using quarantine only", exc
        )
        status_models = {}

    native_aliases = build_alias_map(models)

    blocked = _model_denylist()  # static + PLUGBOARD_MODEL_DENYLIST
    for m in models:
        raw_id = m.get("id", "")
        real_suffix = catalogue_native_model(m)
        # Prefer probed status over quarantine when available
        entry = status_models.get(raw_id)
        if (
            entry is not None
            and observation_owner(raw_id, entry, provider_ids) == m["owned_by"]
        ):
            m["connected"] = bool(entry.get("connected", False))
            m["last_probed_at"] = entry.get("last_probed_at")
        else:
            m["connected"] = False
            m["last_probed_at"] = None
        if real_suffix in quarantined and entry is None:
            m["reason"] = "quarantined"
        m["id"] = publication_ids[(m["owned_by"], raw_id)]

    models = [
        m
        for m in models
        if m["id"] is not None
        and not is_decision_model(catalogue_native_model(m), effective_name=True)
        and catalogue_native_model(m) not in blocked
    ]
    # C1: precompute the dynamic Claude whitelist (N latest per family) once,
    # from the full discovered Claude id set, then filter with it.
    _claude_names = [
        catalogue_native_model(m)
        for m in models
        if "claude" in m.get("id", "").lower() or "anthropic" in m.get("id", "").lower()
    ]
    from plugboard import model_exposure

    expose_all = model_exposure.mode() == "all"
    if _model_filter_mode() == "curated" and not expose_all:
        _claude_allowed = _allowed_claude_suffixes(_claude_names, _claude_latest_n())
        models = [m for m in models if _is_whitelisted_model(m, _claude_allowed)]

    # Trim each provider group to the top-N best models (default: 10).
    # Scored via benchmarks.Arena_ELO + recency bonus so freshly released
    # flagships (claude-opus-4-7, gpt-5.4) surface ahead of older siblings
    # even before public benchmarks land. Disabled by
    # ``PLUGBOARD_TOP_N_PER_PROVIDER=0``.
    try:
        from plugboard.model_ranker import top_n_per_provider

        if not expose_all:
            models = top_n_per_provider(models)
    except Exception as exc:  # noqa: BLE001
        log.warning("list_models: top-N ranking failed (%s) — surfacing all", exc)

    global _models_alias_cache
    _models_alias_cache = {
        key: native_aliases[key]
        for key in build_alias_map(models)
        if key in native_aliases
    }

    _models_cache = models
    _models_cache_ts = now
    _models_cache_namespace = namespace
    # Capture current mtime_ns so a future write to model_status.json triggers refresh.
    try:
        import os as _os
        from plugboard.model_availability import _MODEL_STATUS_PATH

        _model_status_mtime = _os.stat(_MODEL_STATUS_PATH).st_mtime_ns
    except Exception:
        _model_status_mtime = 0
    return models


# ── Model discovery ──────────────────────────────────────────────────────────

_DISCOVER_PROMPT = (
    "List all available agent/model IDs in this instance. "
    "Return ONLY the IDs, one per line, no explanation, no markdown, no numbering. "
    "Example format:\nanthropic--claude-sonnet-4-6\nopenai--gpt-4o\n"
)


def _is_anthropic_api_provider(provider: Dict[str, Any]) -> bool:
    base = (provider.get("base_url") or "").lower()
    return "api.anthropic.com" in base


def _is_gemini_api_provider(provider: Dict[str, Any]) -> bool:
    base = (provider.get("base_url") or "").lower()
    return "generativelanguage.googleapis.com" in base


def _is_ollama_provider(provider: Dict[str, Any]) -> bool:
    base = (provider.get("base_url") or "").lower()
    name = (provider.get("name") or provider.get("id") or "").lower()
    return provider.get("provider_type") == "sovereign" and (
        "ollama" in name or "localhost:11434" in base or "127.0.0.1:11434" in base
    )


def _model_rows(ids: List[str]) -> List[Dict[str, str]]:
    return [{"id": mid, "name": mid} for mid in ids if mid]


async def _discover_anthropic_api_models(
    provider: Dict[str, Any],
    session: aiohttp.ClientSession,
) -> Optional[List[Dict[str, str]]]:
    """Discover first-party Anthropic API models via GET /v1/models."""
    if not _is_anthropic_api_provider(provider):
        return None
    base = (provider.get("base_url") or "").rstrip("/")
    url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
    headers = {
        **_build_auth_header(provider),
        "Accept": "application/json",
        "anthropic-version": "2023-06-01",
    }
    try:
        async with session.get(
            url,
            headers=headers,
            params={"limit": "1000"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                text = await resp.text()
                log.warning(
                    "Anthropic model discovery failed for %s: HTTP %d %s",
                    provider.get("id"),
                    resp.status,
                    text[:160],
                )
                return []
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Anthropic model discovery error for %s: %s", provider.get("id"), exc
        )
        return []

    ids = [
        item.get("id", "")
        for item in data.get("data", []) or []
        if isinstance(item, dict) and item.get("id")
    ]
    log.info("Discovered %d Anthropic models for %s", len(ids), provider.get("id"))
    return _model_rows(ids)


async def _discover_gemini_api_models(
    provider: Dict[str, Any],
    session: aiohttp.ClientSession,
) -> Optional[List[Dict[str, str]]]:
    """Discover first-party Gemini API models via models.list."""
    if not _is_gemini_api_provider(provider):
        return None
    base = (provider.get("base_url") or "").rstrip("/")
    root = base.split("/v1beta", 1)[0] + "/v1beta" if "/v1beta" in base else base
    url = f"{root.rstrip('/')}/models"
    params: Dict[str, str] = {"pageSize": "1000"}
    if provider.get("bearer_token"):
        params["key"] = provider["bearer_token"]

    ids: List[str] = []
    page_token = ""
    try:
        while True:
            req_params = dict(params)
            if page_token:
                req_params["pageToken"] = page_token
            async with session.get(
                url,
                params=req_params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    text = await resp.text()
                    log.warning(
                        "Gemini model discovery failed for %s: HTTP %d %s",
                        provider.get("id"),
                        resp.status,
                        text[:160],
                    )
                    return []
                data = await resp.json()
            for item in data.get("models", []) or []:
                if not isinstance(item, dict):
                    continue
                actions = (
                    item.get("supportedGenerationMethods")
                    or item.get("supported_actions")
                    or item.get("supportedActions")
                    or []
                )
                if actions and "generateContent" not in actions:
                    continue
                mid = item.get("baseModelId") or item.get("name", "")
                mid = mid.removeprefix("models/")
                if mid:
                    ids.append(mid)
            page_token = data.get("nextPageToken") or ""
            if not page_token:
                break
    except Exception as exc:  # noqa: BLE001
        log.warning("Gemini model discovery error for %s: %s", provider.get("id"), exc)
        return []

    log.info("Discovered %d Gemini models for %s", len(ids), provider.get("id"))
    return _model_rows(sorted(set(ids)))


async def _discover_ollama_models(
    provider: Dict[str, Any],
    session: aiohttp.ClientSession,
) -> Optional[List[Dict[str, str]]]:
    """Discover installed Ollama models via GET /api/tags."""
    if not _is_ollama_provider(provider):
        return None
    base = (provider.get("base_url") or "").rstrip("/")
    try:
        async with session.get(
            f"{base}/api/tags",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                log.warning(
                    "Ollama model discovery failed for %s: HTTP %d %s",
                    provider.get("id"),
                    resp.status,
                    text[:160],
                )
                return []
            data = await resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("Ollama model discovery error for %s: %s", provider.get("id"), exc)
        return []
    ids = []
    for item in data.get("models", []) or []:
        if isinstance(item, dict):
            mid = item.get("name") or item.get("model") or ""
            if mid:
                ids.append(mid)
    log.info("Discovered %d Ollama models for %s", len(ids), provider.get("id"))
    return _model_rows(ids)


# Whether the last discover_models() in this context read an authoritative
# list (GET /models or a native catalog API) rather than a fallback (preset
# list, or a model asked to list itself). Fallback lists may add models but
# must never remove any.
_discovery_trusted: ContextVar[bool] = ContextVar(
    "plugboard_discovery_trusted", default=False
)


def last_discovery_trusted() -> bool:
    return _discovery_trusted.get()


async def discover_models(provider: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Auto-discover models from a provider.

    Strategy:
    0. Subprocess providers: return the preset's known_models list (no HTTP).
    1. Try GET /v1/models (standard OpenAI endpoint)
    2. If that fails (HTML, 404, etc.), ask via chat completion
    """
    _discovery_trusted.set(False)
    require_supported_provider(provider)
    require_supported_model(provider.get("default_model", ""), effective_name=True)
    # Subprocess providers don't have an HTTP /v1/models endpoint. Their
    # baseline catalog is declared statically in the preset, but we enrich
    # it with anything LiteLLM has on file for the same vendor (refreshed
    # daily by PricingScheduler). That gives us a zero-touch "new model
    # auto-discovery" path: the day LiteLLM adds ``claude-opus-4-7`` to
    # its JSON, our 03:00 UTC refresh picks it up and the next call to
    # /v1/models surfaces it — without anyone editing the preset.
    auth_source = provider.get("auth_source", "")
    if auth_source.startswith("subprocess:"):
        from plugboard.direct_providers import PRESETS
        from plugboard.model_catalog import discover_subprocess_models_from_pricing

        kind = auth_source.removeprefix("subprocess:")
        if kind == "agy":
            # `agy models` is account-scoped and authoritative: no pricing
            # union, no stale preset list when the CLI answers.
            from plugboard.cli_subprocess import list_agy_models

            agy_ids = await list_agy_models()
            if agy_ids:
                _discovery_trusted.set(True)
                log.info(
                    "subprocess provider %s: %d models from `agy models`",
                    provider["id"],
                    len(agy_ids),
                )
                return [{"id": m, "name": m} for m in agy_ids]
        if kind == "codex":
            # The codex CLI caches the account's own catalog: a ChatGPT plan
            # rejects API-only models such as the preset's gpt-5.x list.
            from plugboard.cli_subprocess import list_codex_models

            codex_ids = list_codex_models()
            if codex_ids:
                _discovery_trusted.set(True)
                log.info(
                    "subprocess provider %s: %d models from the codex cache",
                    provider["id"],
                    len(codex_ids),
                )
                return [{"id": m, "name": m} for m in codex_ids]
        preset_models: List[str] = []
        for preset in PRESETS.values():
            if preset.get("auth_source") == f"subprocess:{kind}":
                preset_models = list(preset.get("known_models", []) or [])
                break

        # Union with the pricing-derived list. The preset stays the floor
        # (regression-safe: if pricing discovery returns nothing, the user
        # still sees the curated baseline) and dynamic discovery is the
        # ceiling (auto-surface freshly released models).
        try:
            dynamic_models = discover_subprocess_models_from_pricing(kind)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "subprocess provider %s: dynamic discovery failed (%s) — "
                "falling back to preset baseline",
                provider["id"],
                exc,
            )
            dynamic_models = []

        seen: set[str] = set()
        merged: List[str] = []
        # Preserve preset order for the baseline (humans curated it), then
        # append dynamically-discovered extras in lexicographic order.
        for m in preset_models:
            if m and m not in seen:
                seen.add(m)
                merged.append(m)
        for m in dynamic_models:
            if m and m not in seen:
                seen.add(m)
                merged.append(m)

        log.info(
            "subprocess provider %s: %d models (preset=%d, dynamic=%d)",
            provider["id"],
            len(merged),
            len(preset_models),
            len(dynamic_models),
        )
        return [{"id": m, "name": m} for m in merged]

    # Provider-native catalog APIs. These avoid static fallbacks when the
    # upstream gives us an authoritative account-scoped model list.
    from plugboard.http_pool import get_session as _get_http_session

    session = await _get_http_session()
    for discoverer in (
        _discover_ollama_models,
        _discover_anthropic_api_models,
        _discover_gemini_api_models,
    ):
        specific = await discoverer(provider, session)
        if specific is not None:
            _discovery_trusted.set(True)
            return specific

    def _preset_known_models() -> Optional[List[Dict[str, str]]]:
        """Fallback: return known_models from the matching preset, if any."""
        from plugboard.direct_providers import PRESETS

        p_auth = provider.get("auth_source", "")
        p_base = (provider.get("base_url") or "").rstrip("/")
        for preset in PRESETS.values():
            if not preset.get("known_models"):
                continue
            matches_auth = p_auth and preset.get("auth_source") == p_auth
            matches_base = (preset.get("base_url") or "").rstrip("/") == p_base
            if matches_auth or matches_base:
                models = preset["known_models"]
                log.info(
                    "preset %s: %d known_models (fallback)", preset["key"], len(models)
                )
                return [{"id": m, "name": m} for m in models]
        return None

    base = provider["base_url"].rstrip("/")
    headers = {
        **_build_auth_header(provider),
        "Accept": "application/json",
    }

    # Strategy 1: standard /v1/models
    try:
        async with session.get(
            openai_url(base, "models"),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                ct = resp.headers.get("Content-Type", "")
                if "json" in ct:
                    data = await resp.json()
                    if isinstance(data.get("data"), list):
                        log.info(
                            "Discovered %d models via /v1/models for %s",
                            len(data["data"]),
                            provider["id"],
                        )
                        _discovery_trusted.set(True)
                        return [
                            {"id": m.get("id", ""), "name": m.get("id", "")}
                            for m in data["data"]
                            if m.get("id")
                        ]
    except Exception as exc:
        log.debug("Standard /v1/models failed for %s: %s", provider["id"], exc)

    # Strategy 1b: preset known_models fallback (for providers whose HTTP
    # /v1/models endpoint doesn't exist — e.g. GitHub Models catalog lives at
    # /catalog/models on a separate host, not /v1/models).
    fallback = _preset_known_models()
    if fallback:
        return fallback

    # Strategy 2: ask via chat completion
    default_model = provider.get("default_model", "")
    if not default_model:
        log.warning(
            "No default_model for %s — cannot discover via chat", provider["id"]
        )
        return []

    from plugboard.decisions import is_decision_model

    if is_decision_model(
        default_model, provider_ids=(provider["id"],), effective_name=True
    ):
        return []

    try:
        async with session.post(
            openai_url(base, "chat/completions"),
            headers={**headers, "Content-Type": "application/json"},
            json=_adapt_payload(
                default_model,
                {
                    "model": default_model,
                    "messages": [{"role": "user", "content": _DISCOVER_PROMPT}],
                    "max_tokens": 2000,
                    "temperature": 0,
                },
            ),
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                log.error(
                    "Discovery chat failed for %s: HTTP %d",
                    provider["id"],
                    resp.status,
                )
                return []
            data = await resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as exc:
        log.error("Discovery chat error for %s: %s", provider["id"], exc)
        fb = _preset_known_models()
        return fb if fb else []

    # Parse response: one model ID per line
    models = []
    for line in content.strip().splitlines():
        mid = line.strip().strip("`").strip("- ").strip()
        if not mid or mid.startswith("#") or len(mid) < 3:
            continue
        # Skip lines that look like prose
        if " " in mid and "--" not in mid:
            continue
        models.append({"id": mid, "name": mid})

    if not models:
        fb = _preset_known_models()
        if fb:
            return fb

    log.info("Discovered %d models via chat for %s", len(models), provider["id"])
    return models
