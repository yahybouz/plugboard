"""Web access on the chat path (PLU-158): the gate, its refusals, the slot.

Written for ``plugboard/app.py`` on ``feat/web-access`` and moved here when
4.15.0 merged it with the chat path split (PLU-95), behaviour unchanged, plus
one rule of 4.15.0: web and tools in the same request are refused
(``web_with_tools_unsupported``, ADR-040).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from plugboard import apikeys as _apikeys
from plugboard import cli_tools as _cli_tools
from plugboard import web_access as _web_access
from plugboard.router import WebUnavailableError, web_refusal

log = logging.getLogger("plugboard.app")

# 400: the request asks for the web and declares tools (ADR-040).
WEB_WITH_TOOLS = "web_with_tools_unsupported"


def _web_refused(status_code: int, code: str, detail: str, **extra: Any):
    content: Dict[str, Any] = {"detail": detail, "code": code, **extra}
    headers = {}
    if status_code == 429:
        headers["Retry-After"] = str(_web_access.RETRY_AFTER_S)
        content["retry_after"] = _web_access.RETRY_AFTER_S
    return JSONResponse(status_code=status_code, content=content, headers=headers)


def _web_unavailable_response(exc: WebUnavailableError) -> JSONResponse:
    """400 ``web_unavailable``: no claude CLI route serves the model."""
    _web_access.count("refused_unavailable")
    log.info("web refused: unavailable for provider=%s", exc.provider_id)
    return _web_refused(
        400,
        _web_access.WEB_UNAVAILABLE,
        f"{exc} (web is not available for this provider). Nothing was called.",
        hint="Use a claude-code:... model, or send the request without web.",
    )


def _web_gate(
    request: Request,
    body: Dict[str, Any],
    client_id: Optional[str],
    key_record: Optional[Dict[str, Any]],
) -> Optional[JSONResponse]:
    """Decide the request's web access (PLU-158) and pop its extension.

    No web asked: None, nothing changes. Asked: 400 for a malformed field,
    403 ``web_not_allowed`` when the key does not hold the right (the admin
    key never does), 400 ``web_with_tools_unsupported`` when the request
    also declares tools (ADR-040: the web run gives the CLI only its two web
    tools, so tools emulated as text cannot be honoured with it), 400
    ``web_unavailable`` when no claude CLI route serves the model, 429
    ``web_busy`` when the web slots are all taken; otherwise the slot is held
    until the response is sent (``_inflight_chat_dep``) and the request's
    policy is set for the router, the CLI and billing.
    """
    try:
        asked = _web_access.requested(body, request.headers)
    except _web_access.WebRequestError as exc:
        return _web_refused(400, _web_access.WEB_BAD_REQUEST, str(exc))
    if not asked:
        # A context copied from an earlier web request never leaks into this one.
        _web_access.activate(None)
        return None
    if not _apikeys.allows_web(key_record):
        _web_access.count("refused_not_allowed")
        log.info("web refused: key not allowed (client=%s)", client_id or "-")
        return _web_refused(
            403,
            _web_access.WEB_NOT_ALLOWED,
            "This API key is not allowed to use the web: ask the administrator "
            "to turn web_access on for it. Nothing was called.",
        )
    if _cli_tools.engaged(body):
        _web_access.count("refused_with_tools")
        log.info("web refused: tools declared (client=%s)", client_id or "-")
        return _web_refused(
            400,
            WEB_WITH_TOOLS,
            "Web access and tools cannot be used in the same request: send "
            "the request without tools, or without web. Nothing was called.",
        )
    refusal = web_refusal(body.get("model", ""))
    if refusal is not None:
        return _web_unavailable_response(refusal)
    slot = _web_access.try_acquire()
    if slot is None:
        log.info("web refused: busy (client=%s)", client_id or "-")
        return _web_refused(
            429,
            _web_access.WEB_BUSY,
            f"Too many web requests in progress (limit "
            f"{_web_access.MAX_INFLIGHT}): retry in "
            f"{_web_access.RETRY_AFTER_S} s.",
        )
    request.state.web_slot = slot
    request.state.web_t0 = time.perf_counter()
    policy = _web_access.WebPolicy(max_uses=_apikeys.web_max_uses(key_record))
    request.state.web_policy = policy
    _web_access.activate(policy)
    return None


def _web_finish(request: Request) -> None:
    """Give the web slot back and log the call, without its content."""
    slot = getattr(request.state, "web_slot", None)
    if slot is None:
        return
    request.state.web_slot = None
    policy = getattr(request.state, "web_policy", None)
    t0 = getattr(request.state, "web_t0", None)
    slot.release()
    log.info(
        "web call done: client=%s key=%s uses=%s max_uses=%s elapsed=%.1fs",
        getattr(request.state, "client_id", None) or "-",
        getattr(request.state, "key_id", "") or "-",
        policy.uses if policy else "?",
        policy.max_uses if policy else "?",
        (time.perf_counter() - t0) if t0 else -1.0,
    )
