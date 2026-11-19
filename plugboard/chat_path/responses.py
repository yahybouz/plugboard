"""Answers to the client and error responses of the chat path.

What a caller may see of a completion, the 429, 503, 400, 404 and 502 answers
built from the provider errors, and the replay of a whole answer as a stream.
Moved verbatim from ``plugboard/app.py`` (PLU-95), behaviour unchanged.
``core`` is the ``plugboard.app`` module that registers
``_provider_disabled_response`` and ``_chat_error_response``: ``get_provider``,
a patch point that stayed there, is read as ``core.get_provider`` when it runs.
"""

from __future__ import annotations

import logging
import secrets
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from plugboard import chat_billing
from plugboard.metrics import record_busy_refusal
from plugboard.cli_auth import AuthError
from plugboard.cli_limits import UNSUPPORTED_ON_CLI
from plugboard.cli_tools import CLI_TOOL_CALL_INVALID, CliToolCallError
from plugboard.cli_subprocess import CliSubprocessError, ModelNotServedError
from plugboard.router import (
    ProviderDisabledError,
    ProviderHTTPError,
    ProviderNetworkError,
    ProvidersBusyError,
    ProviderQuotaError,
    ProviderTimeoutError,
    UnsupportedOnCliError,
    WebUnavailableError,
)
from plugboard.web_gate import _web_unavailable_response

log = logging.getLogger("plugboard.app")


def _for_caller(completion: Dict[str, Any], client_id: Optional[str]) -> Dict[str, Any]:
    """The completion as the caller may see it.

    ``billing.price_key`` names the grid entry that priced the call, which
    reveals provider routing: admins see it (like ``X-Price-Key`` and the
    entries of ``/usage/me``), client keys do not. Applied to every answer,
    including those served from the cache.
    """
    bill = completion.get("billing")
    if client_id == "admin" or not isinstance(bill, dict) or "price_key" not in bill:
        return completion
    return {
        **completion,
        "billing": {k: v for k, v in bill.items() if k != "price_key"},
    }


def _quota_detail(provider: str, retry_after: Optional[int]) -> str:
    """Reason given to a client key: composed here, never the upstream text."""
    when = (
        f"retry after {retry_after} seconds"
        if retry_after is not None
        else "retry later"
    )
    return f"The quota or rate limit of the {provider} provider is exhausted: {when}."


def _retry_after_headers(
    content: Dict[str, Any], retry_after: Optional[int]
) -> Dict[str, str]:
    """Add ``retry_after`` to an error body; return the Retry-After header."""
    if retry_after is None:
        return {}
    content["retry_after"] = retry_after
    return {"Retry-After": str(retry_after)}


def _quota_error_response(
    exc: ProviderQuotaError, client_id: Optional[str]
) -> JSONResponse:
    """429 when the last HTTP provider tried answered 429.

    A non-streamed request has already been offered to the other candidate
    providers (the last one tried decides); a streamed one to an HTTP
    provider has no fallback. The upstream body stays with the admin; a
    client key gets the provider, the reason and the provider's Retry-After
    delay when it gave one.
    """
    log.warning(
        "provider quota exhausted: provider=%s retry_after=%s: %s",
        exc.provider_id,
        exc.retry_after,
        exc,
    )
    if client_id == "admin":
        detail = str(exc)
    else:
        detail = _quota_detail(exc.provider_id, exc.retry_after)
    content: Dict[str, Any] = {"detail": detail, "code": "quota_exhausted"}
    headers = _retry_after_headers(content, exc.retry_after)
    return JSONResponse(status_code=429, content=content, headers=headers)


def _providers_busy_response(
    exc: ProvidersBusyError, client_id: Optional[str]
) -> JSONResponse:
    """503 ``providers_busy`` when every candidate provider stayed at its
    concurrency cap for the whole wait (D-23).

    Nothing was called. Only the admin gets the ids of the busy providers.
    """
    log.warning("providers busy: %s", ", ".join(exc.provider_ids))
    record_busy_refusal((exc.provider_ids or [""])[0], exc.model)  # PLU-16
    detail = (
        "Every provider that serves this model is busy: "
        f"retry in {exc.retry_after} seconds."
    )
    if client_id == "admin":
        detail += f" Busy providers: {', '.join(exc.provider_ids)}."
    content: Dict[str, Any] = {"detail": detail, "code": "providers_busy"}
    headers = _retry_after_headers(content, exc.retry_after)
    return JSONResponse(status_code=503, content=content, headers=headers)


def _unsupported_on_cli_response(exc: UnsupportedOnCliError) -> JSONResponse:
    """400 ``unsupported_on_cli``: every route of the model is a CLI
    subscription, and a CLI cannot serve the fields ``exc.unsupported``
    names (tools, a structured response format, an image...). Nothing was
    called. The body holds no secret: a client key and the admin get it alike.
    """
    log.info(
        "unsupported on cli: provider=%s fields=%s",
        exc.provider_id,
        ",".join(exc.unsupported),
    )
    return JSONResponse(
        status_code=400,
        content={
            "detail": f"Model route '{exc.provider_id}' is a CLI subscription: "
            f"it cannot serve {', '.join(exc.unsupported)}. Nothing was called.",
            "code": UNSUPPORTED_ON_CLI,
            "unsupported": list(exc.unsupported),
            "hint": "Send plain text messages, or pick a model served by an API "
            "or Ollama provider from GET /v1/models.",
        },
    )


def _cli_tool_call_response(exc: CliToolCallError) -> JSONResponse:
    """502 ``cli_tool_call_invalid``: the CLI answered a request with tools
    emulated as text (ADR-039) off the protocol. Not retried, not billed, not
    counted against the route; ``x-should-retry: false`` tells the SDKs that
    read it. The detail is written here: the reason and, when there is one,
    the tool name (shortened, printable), never the model's text. The log
    gets the reason only.
    """
    ref = secrets.token_hex(4)
    log.warning("cli tool call invalid ref=%s reason=%s", ref, exc.reason)
    detail = f"The CLI subscription route answered off the tool protocol ({exc.reason}"
    if exc.tool:
        shown = "".join(c if c.isprintable() else "?" for c in exc.tool[:64])
        detail += f", tool '{shown}'"
    detail += (
        "); nothing was billed and the request was not retried "
        f"(reference {ref} in the gateway log)."
    )
    return JSONResponse(
        status_code=502,
        content={"detail": detail, "code": CLI_TOOL_CALL_INVALID, "error_ref": ref},
        headers={"x-should-retry": "false"},
    )


# A whole answer asked as a stream is replayed by chat_billing (PLU-28).
_replay_completion_as_sse = chat_billing.replay_as_sse


def _provider_http_error_response(
    exc: ProviderHTTPError | ProviderNetworkError, client_id: Optional[str]
) -> JSONResponse:
    """400 when an HTTP provider rejected the request, 502 for its other errors.

    The provider rejected the request when its answer says so (413, an error
    saying the context is too long in any format, or an error type naming the
    request: ``exc.request_rejected``); the status alone does not, a provider
    also answers 400 for its own key. A
    provider that could not be reached gives a 502. The provider's body can
    name its account or organisation, and a network error its host and port:
    only the admin gets them. A client key gets a reason written here and a
    reference to the server log entry, never the upstream text.
    """
    ref = secrets.token_hex(4)
    log.warning(
        "http provider error ref=%s provider=%s status=%s: %s",
        ref,
        exc.provider_id,
        getattr(exc, "status", "network"),
        exc,
    )
    rejected = getattr(exc, "request_rejected", False)
    if client_id == "admin":
        detail = str(exc)
    elif rejected:
        detail = (
            f"The {exc.provider_id} provider rejected the request: check the "
            "length of the messages against the model's context and the "
            f"parameters (reference {ref} in the gateway log)."
        )
    else:
        detail = (
            f"The {exc.provider_id} provider failed "
            f"(reference {ref} in the gateway log)."
        )
    return JSONResponse(
        status_code=400 if rejected else 502,
        content={"detail": detail, "error_ref": ref},
    )


def _cli_error_response(
    exc: CliSubprocessError, client_id: Optional[str]
) -> JSONResponse:
    """502 for a failed CLI provider, 429 when it failed on its quota, 400
    (``context_length_exceeded``) when it found the prompt too long, 400
    (``model_not_supported_by_subscription``) when its subscription does not
    serve the model (BUG-78), 503 (``provider_sandbox_unavailable``) when
    agy's cage could not start, so agy was not run (BUG-86).

    ``exc.quota`` and ``exc.request_too_long`` were decided by the CLI wrapper
    from the CLI's own error and stderr, never from the model's output. The
    CLI's message, stderr, pid and exit code can carry local paths, account
    names or tokens: only the admin (dashboard Playground, admin key) gets
    them. A client key gets a reference to the server log entry.
    """
    quota = exc.quota
    too_long = exc.request_too_long and not quota
    unsupported = getattr(exc, "model_unsupported", False) and not quota
    timed_out = getattr(exc, "timed_out", False) and not quota
    sandbox = getattr(exc, "sandbox_failed", False) and not quota
    ref = secrets.token_hex(4)
    log.warning(
        "cli provider error ref=%s provider=%s model=%s exit=%s pid=%s: %s | stderr=%s",
        ref,
        exc.provider_name,
        exc.model_id,
        exc.exit_code,
        exc.pid,
        exc,
        exc.stderr,
    )
    if client_id == "admin":
        content = {
            "detail": str(exc),
            "exit_code": exc.exit_code,
            "stderr": exc.stderr,
            "pid": exc.pid,
            "provider_name": exc.provider_name,
            "model_id": exc.model_id,
            "error_ref": ref,
        }
    else:
        name = exc.provider_name or "CLI"
        if sandbox:
            detail = (
                f"The {name} provider is unavailable: its sandbox (bubblewrap) "
                f"could not start, so {name} was not run "
                f"(reference {ref} in the gateway log)."
            )
        elif quota:
            detail = _quota_detail(name, exc.retry_after)
        elif too_long:
            detail = (
                f"The {name} provider rejected the request: the messages are too "
                f"long for the model's context (reference {ref} in the gateway log)."
            )
        elif timed_out:
            detail = (
                f"The {name} provider did not answer in time "
                f"(reference {ref} in the gateway log)."
            )
        elif unsupported:
            detail = (
                f"The {name} provider's subscription does not support the model "
                f"{exc.model_id!r}: pick another model from GET /v1/models "
                f"(reference {ref} in the gateway log)."
            )
        elif getattr(exc, "web_exhausted", False):
            detail = str(exc)  # written by cli_subprocess: no secret
        else:
            detail = f"The {name} provider failed (reference {ref} in the gateway log)."
        content = {
            "detail": detail,
            "error_ref": ref,
            "provider_name": exc.provider_name,
            "model_id": exc.model_id,
        }
    if getattr(exc, "web_exhausted", False) and not quota:
        content["code"] = "web_budget_exhausted"
        return JSONResponse(status_code=502, content=content)
    if sandbox:
        # No Retry-After: a missing or refused sandbox does not come back by
        # itself.
        content["code"] = "provider_sandbox_unavailable"
        return JSONResponse(status_code=503, content=content)
    if too_long:
        content["code"] = "context_length_exceeded"
        return JSONResponse(status_code=400, content=content)
    if unsupported:
        content["code"] = "model_not_supported_by_subscription"
        content["hint"] = MODEL_NOT_SUPPORTED_HINT
        return JSONResponse(status_code=400, content=content)
    if timed_out:
        content["code"] = "provider_timeout"
        content["retry_after"] = 10
        return JSONResponse(
            status_code=503, content=content, headers={"Retry-After": "10"}
        )
    if not quota:
        return JSONResponse(status_code=502, content=content)
    content["code"] = "quota_exhausted"
    headers = _retry_after_headers(content, exc.retry_after)
    return JSONResponse(status_code=429, content=content, headers=headers)


MODEL_NOT_SUPPORTED_HINT = (
    "This route's subscription (a CLI signed in with an account plan) does not "
    "serve this model: GET /v1/models lists the ones it does, or use a route "
    "with an API key."
)

MODEL_NOT_FOUND_HINT = (
    "GET /v1/models lists the model IDs this gateway serves: "
    "send one as written there, <provider-id>:<model>."
)


def _model_not_found_response(model: str) -> JSONResponse:
    """404 for a model ID no provider knows (router.is_unknown_model).

    ``code`` is stable for programs, ``hint`` says what to do. The ID is
    echoed, shortened, so a typo or a stray space shows.
    """
    shown = model if len(model) <= 100 else model[:100] + "..."
    return JSONResponse(
        status_code=404,
        content={
            "detail": f"Model '{shown}' not found: no provider ID prefixes it "
            "and no provider lists it.",
            "code": "model_not_found",
            "hint": MODEL_NOT_FOUND_HINT,
        },
    )


def _model_not_served_response() -> JSONResponse:
    """404 for a model ID a CLI route does not serve (BUG-120).

    The same ``code`` and ``hint`` as ``_model_not_found_response``. The
    detail is generic: it never quotes the ID (it may hold line breaks or
    options), nor a CLI, its output or a pid, since nothing was started.
    """
    return JSONResponse(
        status_code=404,
        content={
            "detail": "Model not found: the provider this model ID selects "
            "does not serve it; nothing was called.",
            "code": "model_not_found",
            "hint": MODEL_NOT_FOUND_HINT,
        },
    )


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Define this block's closures, which read ``core`` when they run."""

    def _provider_disabled_response(
        provider_id: str, provider: Optional[Dict[str, Any]] = None
    ) -> JSONResponse:
        """503 ``provider_disabled``: the model belongs to a provider the admin
        turned off (PLU-12). Nothing was called, no other provider stands in,
        and no ``Retry-After``: nobody knows when it comes back.

        A provider turned off because its stored key cannot be decrypted
        (BUG-57) keeps the same code, with a detail and hint that say so.
        """
        log.info("provider disabled: %s", provider_id)
        if provider is None:
            provider = core.get_provider(provider_id) or {}
            if not provider:
                from plugboard import ollama

                if provider_id == ollama.PROVIDER_ID and ollama.disabled_by_config():
                    provider = {"disabled_by_config": True}  # never registered
        if provider.get("disabled_by_config"):
            detail = (
                f"Provider '{provider_id}' is turned off by the server "
                "configuration (OLLAMA_ENABLED=false); nothing was called."
            )
            hint = (
                "Pick another model from GET /v1/models, or ask the administrator "
                "to set OLLAMA_ENABLED to true or auto and restart the gateway."
            )
        elif provider.get("secret_unreadable"):
            detail = (
                f"Provider '{provider_id}' is turned off: its stored key cannot be "
                "decrypted with the current ENCRYPTION_KEY; nothing was called."
            )
            hint = (
                "Pick another model from GET /v1/models, or ask the administrator "
                "to restore the ENCRYPTION_KEY that encrypted it, or to enter the "
                "provider's key again."
            )
        else:
            detail = (
                f"Provider '{provider_id}' is turned off by the "
                "administrator; nothing was called."
            )
            hint = (
                "Pick another model from GET /v1/models, or ask the "
                "administrator to turn the provider back on."
            )
        return JSONResponse(
            status_code=503,
            content={"detail": detail, "code": "provider_disabled", "hint": hint},
        )

    def _chat_error_response(
        exc: Exception, client_id: Optional[str], phase: str
    ) -> JSONResponse:
        """The answer to a chat whose provider call raised ``exc`` (every chat
        path: plain, stream, a CLI route asked to stream).

        Typed errors keep their own answers. Any other text can name a host, a
        port, a path, an account or a CLI hint (PLU-28): only the admin gets it.
        A client key gets a sentence written here and an ``error_ref`` that
        finds the text in the gateway log. A timeout is a 503 with Retry-After
        10, a failed provider authentication keeps its status, the rest is 502.
        """
        if isinstance(exc, ProvidersBusyError):
            return _providers_busy_response(exc, client_id)
        if isinstance(exc, ProviderDisabledError):
            return _provider_disabled_response(exc.provider_id)
        if isinstance(exc, UnsupportedOnCliError):
            return _unsupported_on_cli_response(exc)
        if isinstance(exc, WebUnavailableError):
            return _web_unavailable_response(exc)
        if isinstance(exc, ModelNotServedError):
            return _model_not_served_response()  # nothing was started (BUG-120)
        if isinstance(exc, CliToolCallError):
            return _cli_tool_call_response(exc)  # ADR-039, never retried
        # Before the timeout test: a CLI message quotes its stderr.
        if isinstance(exc, CliSubprocessError):
            return _cli_error_response(exc, client_id)
        if isinstance(exc, ProviderQuotaError):
            return _quota_error_response(exc, client_id)
        # Before the timeout test: the provider's body can say "timeout".
        if isinstance(exc, (ProviderHTTPError, ProviderNetworkError)):
            return _provider_http_error_response(exc, client_id)
        ref = secrets.token_hex(4)
        err_str = str(exc)
        log.error("%s error ref=%s (%s): %s", phase, ref, type(exc).__name__, err_str)
        headers: Dict[str, str] = {}
        content: Dict[str, Any] = {}
        if isinstance(exc, ProviderTimeoutError) or "timeout" in err_str.lower():
            status_code = 503
            who = getattr(exc, "provider_id", "")
            detail = f"The {who} provider" if who else "The provider"
            detail += " did not answer in time"
            content["retry_after"] = 10
            content["code"] = "provider_timeout"
            headers["Retry-After"] = "10"
        elif isinstance(exc, AuthError):
            status_code = exc.status_code
            detail = "The provider's credentials could not be used"
        else:
            status_code = 502
            detail = "The provider call failed"
        if client_id == "admin":
            content["detail"] = err_str
        else:
            content["detail"] = f"{detail} (reference {ref} in the gateway log)."
        content["error_ref"] = ref
        return JSONResponse(status_code=status_code, content=content, headers=headers)

    return SimpleNamespace(
        _provider_disabled_response=_provider_disabled_response,
        _chat_error_response=_chat_error_response,
    )
