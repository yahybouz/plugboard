"""POST /v1/chat/completions: request checks, the session turn and the route.

The chat body checks (output caps, messages and roles), the session's turn
taken and recorded, the /v1/inflight note, and the chat route with its
streamed and whole answers. Moved verbatim from ``plugboard/app.py``
(PLU-95), behaviour unchanged. ``core`` is the ``plugboard.app`` module that
registers the route: the patch points (``forward``, ``forward_stream``,
``cli_refusal``, ``load_providers``, ``resolve_provider_and_model``,
``_get_request_key_record``, ``_check_price_known``, ``_hold_budget``,
``_model_route_guard``, ``_bill_streaming``, ``_send_spending_alert``,
``_record_session_turn``) and the names that stay in ``app.py`` are read as
``core.<name>`` when they run.
"""

from __future__ import annotations

import asyncio as _asyncio
import functools
import logging
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from plugboard import chat_billing
from plugboard import cli_tools as _cli_tools
from plugboard import live_requests as _live
from plugboard import request_wait as _request_wait
from plugboard.api_contract import CHAT_ROLES, MAX_OUTPUT_TOKENS_CAP
from plugboard.apikeys import allows_cli_tools
from plugboard.chat_path.billing import _keep_hold
from plugboard.chat_path.limits import _check_client_rpm
from plugboard.chat_path.responses import (
    _for_caller,
    _model_not_found_response,
    _model_not_served_response,
    _replay_completion_as_sse,
    _unsupported_on_cli_response,
)
from plugboard.chat_path.streaming import (
    _ClientGone,
    _client_gone_response,
    _complete_events,
    _done_event_at,
    _stream_cut_event,
    _unless_client_gone,
)
from plugboard.decisions import is_decision_model
from plugboard.legacy_connectors import (
    REMOVED_CONNECTOR_ERROR,
    require_supported_model,
    require_supported_provider,
)
from plugboard.cli_limits import unsupported_on_cli
from plugboard.router import (
    UnsupportedOnCliError,
    disabled_route,
    is_unknown_model,
    is_unserved_cli_model,
    usage_reported as _usage_reported,
)
from plugboard.storage import to_thread_to_the_end as _to_thread_to_the_end

log = logging.getLogger("plugboard.app")


def _shown_role(role: str) -> str:
    """A refused role as the 400 quotes it: 40 characters at most, anything
    unprintable (a line break, a control character) as ?."""
    shown = "".join(c if c.isprintable() else "?" for c in role[:40])
    return shown + ("..." if len(role) > 40 else "")


def _cli_tools_right(
    client_id: Optional[str], key_record: Optional[Dict[str, Any]]
) -> bool:
    """Whether the call may have tools emulated on CLI routes (ADR-039): the
    instance option is on and the calling key holds ``allow_cli_tools``. The
    admin, who has no key record, never may."""
    if not client_id or client_id == "admin":
        return False
    return _cli_tools.emulation_enabled() and allows_cli_tools(key_record)


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    async def _take_session_turn(
        request: Request, client_id: str, session_id: str
    ) -> None:
        """Wait for the previous turn of this session to be answered and recorded.

        The release is stored as soon as the turn is obtained, so a hang-up that
        comes later (``_ClientGone``) cannot lose it. The wait takes what is left
        of the request's wait budget (PLU-15): then 503 ``session_busy`` with
        Retry-After (PLU-45), out of the queue. A free turn is taken at once,
        even with no budget left. While the turn runs elsewhere, the request
        gives its global slot back (BUG-77).
        """
        from plugboard import session_turns

        if not session_turns.busy(client_id, session_id):
            request.state.session_turn_release = await session_turns.acquire(
                client_id, session_id
            )
            return
        # BUG-77: no global slot held while the turn runs elsewhere; it is taken
        # back right before a provider is called (PLU-51).
        _request_wait.release_slot()
        try:
            with _request_wait.waiting() as timeout:
                request.state.session_turn_release = await session_turns.acquire(
                    client_id, session_id, timeout=timeout
                )
        except session_turns.QueueFull:
            log.info(
                "session turn busy: %d turns already wait", session_turns.MAX_WAITING
            )
            raise core._SessionBusy() from None
        except TimeoutError:
            log.info("session turn busy: the request's wait ended")
            raise core._SessionBusy() from None

    def _record_session_turn(
        client_id: Optional[str],
        session_id: Optional[str],
        messages: List[dict],
        reply: str,
    ) -> None:
        """PERF-1: record the exchange so the next call with the same session_id
        sees the updated transcript. Non-blocking: never bubbles errors."""
        if not (session_id and client_id):
            return
        try:
            from plugboard.session_store import record_exchange

            record_exchange(client_id, session_id, messages, reply)
        except Exception as exc:
            log.warning("session record error (non-fatal): %s", exc)

    def _require_output_caps(body: Dict[str, Any]) -> None:
        """400 for a max_tokens / max_completion_tokens no provider can honour
        (BUG-121): absent or null passes; otherwise a whole number (5 or 5.0, not
        a boolean nor text) from 1 to MAX_OUTPUT_TOKENS_CAP. A negative cap used
        to reach the provider, a huge one to stop at the budget hold as a 429."""
        for name in ("max_tokens", "max_completion_tokens"):
            value = body.get(name)
            if value is None:
                continue
            whole = isinstance(value, int) or (
                isinstance(value, float) and value.is_integer()
            )
            if (
                isinstance(value, bool)
                or not whole
                or not 1 <= value <= MAX_OUTPUT_TOKENS_CAP
            ):
                raise core._InvalidChatRequest(
                    f"{name} must be an integer from 1 to {MAX_OUTPUT_TOKENS_CAP}"
                )

    def _require_chat_messages(
        body: Dict[str, Any], *, history_may_follow: bool = False
    ) -> None:
        """400 ``invalid_request`` for a messages field, or an output cap, no
        provider can answer.

        Without it, an empty or malformed list reached the provider and came back
        as a 502 carrying the upstream parser error (or the CLI's "Empty prompt");
        an unknown role was answered as a user turn (BUG-121).
        With ``history_may_follow`` (a session turn, before its history is merged)
        an absent or empty list passes: the stored history may fill it, and the
        full check runs again after the merge. The output caps are checked first,
        in both cases.
        """
        _require_output_caps(body)
        if history_may_follow and body.get("messages", []) == []:
            return
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise core._InvalidChatRequest("messages must be a non-empty array")
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                raise core._InvalidChatRequest(f"messages[{i}] must be an object")
            role = msg.get("role")
            if not isinstance(role, str) or not role.strip():
                raise core._InvalidChatRequest(
                    f"messages[{i}].role must be a non-empty string"
                )
            if role not in CHAT_ROLES:
                raise core._InvalidChatRequest(
                    f"messages[{i}].role '{_shown_role(role)}' is not supported: use "
                    "system, developer, user, assistant, tool or function"
                )
            content = msg.get("content")
            if content is not None and not isinstance(content, (str, list)):
                raise core._InvalidChatRequest(
                    f"messages[{i}].content must be a string or an array of parts"
                )

    def _live_note(
        model: str, client_id: Optional[str], disabled: Optional[tuple] = None
    ) -> None:
        """Tell /v1/inflight which model, provider and caller this request is.

        ``disabled`` is the request's router.disabled_route answer, read once by
        the caller: a model of a disabled provider is shown as its owner's.
        """
        provider_id = None
        try:
            route = disabled or core.resolve_provider_and_model(model)
            provider_id = route[0].get("id")
        except Exception:  # unknown model: the admission checks answer it
            pass
        _live.update(
            client_id=client_id, model=model[:120] or None, provider=provider_id
        )

    @app.post(
        "/v1/chat/completions",
        # The key first (PLU-15): see _inflight_chat_dep.
        dependencies=[
            Depends(core._resolve_client),
            Depends(core._refuse_anonymous_chat),
            Depends(core._inflight_chat_dep),
            Depends(core._session_turn_dep),
        ],
    )
    async def chat_completions(
        request: Request,
        client_id: Optional[str] = Depends(core._resolve_client),
    ):
        # SEC-001 (v3.0.0): strict tenant mode requires a valid client_id for any
        # cache-scoped endpoint. Anonymous requests would crash the cache layer with
        # ValueError and leak as a bare 500. Gate early so callers get 401/403.
        if not client_id and core._strict_tenant_mode():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required: missing client_id.",
            )

        # ARCH-DEBT-026 closed (opt-in): track request start so a cache HIT can
        # be artificially padded to the configured floor. Eliminates the timing
        # side-channel where 5ms HIT vs 500ms MISS leaks "this prompt is
        # cached" to a co-located attacker. Off by default (floor=0) since the
        # leak requires LAN-level adversary; opt-in for high-sensitivity
        # deployments via PLUGBOARD_CACHE_RESPONSE_FLOOR_MS.
        _request_t0 = time.perf_counter()
        try:
            body = await request.json()
        except (ValueError, RecursionError):
            # BUG-48: a body that is not JSON (or not UTF-8, or nested past the
            # parser's depth) is the client's error, not a 500.
            raise HTTPException(400, "Request body must be valid JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be a JSON object")
        if not isinstance(body.get("model", ""), str):
            raise HTTPException(400, "model must be a string")
        # Read once per request (PLU-12): /v1/inflight and the admission share it.
        disabled = disabled_route(body.get("model", ""))
        _live_note(body.get("model", ""), client_id, disabled)
        providers = core.load_providers()
        try:
            require_supported_model(
                body.get("model", ""), provider_ids=(p["id"] for p in providers)
            )
            for provider in sorted(providers, key=lambda p: len(p["id"]), reverse=True):
                if body.get("model", "").startswith(provider["id"] + ":"):
                    require_supported_provider(provider)
                    break
            # Validate the effective default/dated alias before a cached answer can
            # bypass routing. Other admission errors keep their existing handling.
            try:
                core.resolve_provider_and_model(body.get("model", ""))
            except ValueError as exc:
                if str(exc) == REMOVED_CONNECTOR_ERROR:
                    raise
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if is_decision_model(
            body.get("model", ""), provider_ids=(p["id"] for p in core.load_providers())
        ):
            raise HTTPException(400, "Use /v1/decisions for decision models")
        # Pre-resolve once: downstream spending/model/rpm checks all receive
        # the same record and skip their own load_keys() fallback scans.
        key_record = core._get_request_key_record(request, client_id=client_id)
        spending_total = 0.0

        # PERF-1: opt-in server-side session continuity.
        # Client passes a session id via header X-LLM-Session-ID or body.session_id;
        # we prepend previously stored history so they only need to send the new
        # turn. Anonymous clients can't use sessions (no owner → no isolation).
        session_id: Optional[str] = (
            request.headers.get("X-LLM-Session-ID")
            or request.headers.get("x-llm-session-id")
            or body.pop("session_id", None)
        )
        if isinstance(session_id, (list, dict)):
            # BUG-48: an unhashable session id made the session turn fail (500).
            raise HTTPException(400, "session_id must be a string")
        in_session = bool(session_id and client_id)
        _require_chat_messages(body, history_may_follow=in_session)

        # Issue #5: check spending limit before processing request
        if client_id and client_id != "admin":
            spending_total = await _asyncio.to_thread(
                core._check_spending_limit, client_id, key_record=key_record
            )

        # Check model access (per-key allowed_models). A model of a disabled
        # provider is checked as its owner's, then refused (PLU-12).
        route_guard = core._model_route_guard(client_id, key_record)
        route_options = {}
        if route_guard is not None:
            route_guard(
                *(disabled or core.resolve_provider_and_model(body.get("model", "")))
            )
            route_options["route_guard"] = route_guard

        # After the key's 403, before the 404 and the 402; before the cache too,
        # so no stored answer comes out while the provider is off.
        if disabled is not None:
            return core._provider_disabled_response(disabled[0]["id"], disabled[0])

        # After the key's 403, so a limited key cannot probe which IDs exist;
        # before the 402, which would blame a price for a model that is not there.
        if is_unknown_model(body.get("model", "")):
            return _model_not_found_response(body["model"])
        # BUG-120: a CLI route is never given a name outside its catalogue, nor
        # one that reads as an option, whoever calls (the admin key included).
        # Same place as the 404 above: after the key's 403 and the 503 of a
        # turned-off provider, before the 402, the rpm count, the session turn,
        # the cache and any provider; each of those refuses too, nothing starts.
        if is_unserved_cli_model(body.get("model", "")):
            return _model_not_served_response()

        # After the 404, before the 402 and the rpm count, the session turn and
        # the cache: a request only CLI routes serve, asking for what a CLI cannot
        # (tools, structured output, images), is refused with nothing counted.
        # ADR-039: with the option and the key right, tools on CLI routes only
        # are emulated as text; without both, the refusal is unchanged.
        # The routes are resolved once: the refusal names the fields left once
        # the tools are emulated.
        refusal = core.cli_refusal(body)
        emulated = False
        if (
            refusal is not None
            and _cli_tools.engaged(body)
            and _cli_tools_right(client_id, key_record)
        ):
            left = unsupported_on_cli(body, cli_tools=True)
            if left:
                refusal = UnsupportedOnCliError(refusal.provider_id, left)
            else:
                refusal, emulated = None, True
        if refusal is not None:
            return _unsupported_on_cli_response(refusal)
        forward_options = dict(route_options)
        if emulated:
            # Checked here, before anything is counted: a session cannot
            # replay tool calls (v1), and the bounds hold before any call.
            if in_session:
                raise core._InvalidChatRequest(
                    "session_id cannot be used with tools on a CLI subscription route"
                )
            try:
                _cli_tools.to_text_body(body)
            except _cli_tools.CliToolsRequestError as exc:
                raise core._InvalidChatRequest(str(exc)) from None
            forward_options["cli_tools"] = True

        def billed() -> Dict[str, Any]:
            """The body billing estimates on when the answer has no counts:
            with tools emulated, the text the CLI got (ADR-039)."""
            return _cli_tools.to_text_body(body) if emulated else body

        # A budget cannot hold a call whose price is unknown (402, nothing called)
        if client_id and client_id != "admin":
            core._check_price_known(
                client_id, body.get("model", ""), key_record=key_record
            )

        # Per-client rate limit (rpm_limit on key record)
        if client_id and client_id != "admin":
            _check_client_rpm(client_id, key_record=key_record)

        if in_session:
            from plugboard.session_store import merge_history_into_body

            # D-24: the previous turn of this session is answered and recorded
            # first, so this one sees it; the turn comes back after the response.
            # Taken after every refusal above: a request that is refused does not
            # wait for the turn its session is running.
            try:
                await _unless_client_gone(
                    request, _take_session_turn(request, client_id, session_id)
                )
            except _ClientGone:
                return _client_gone_response(body.get("model", "?"))
            body = merge_history_into_body(client_id, session_id, body)
            _require_chat_messages(body)

        # Response cache — disabled only when caller sets cache=false.
        # For streaming requests we still check the cache; on HIT we replay the
        # cached response as SSE chunks so the Playground still "streams" but
        # returns instantly. Only the WRITE-side of streaming is skipped (we
        # don't cache streamed responses yet — they're replayed from the
        # non-stream cache when the same prompt was asked non-stream first).
        _cache_enabled = body.pop("cache", True) is not False
        _is_streaming = body.get("stream") is True
        # Historical cache entries do not bind the effective serving identity.
        # Restricted keys must not reuse responses from another route or policy.
        _use_cache = _cache_enabled and route_guard is None
        _cache_key = None
        if _use_cache:
            from plugboard.cache import compute_cache_key, get_cached, key_params

            _cache_key = compute_cache_key(
                body.get("model", ""),
                body.get("messages", []),
                client_id=client_id or "",
                **key_params(body),
            )
            cached = await _asyncio.to_thread(
                get_cached, _cache_key, client_id=client_id or ""
            )
            if not cached and not _is_streaming:
                # Exact miss → try semantic (paraphrase) match. Off by default;
                # enable via PLUGBOARD_SEMANTIC_CACHE=1. Phase 2 (embedding-
                # based cosine fallback) requires PLUGBOARD_SEMANTIC_EMBED=1
                # plus EMBED_PROVIDER_ID + EMBED_MODEL. Streams not supported
                # in this phase (would need SSE replay like the subprocess path).
                try:
                    from plugboard import semantic_cache as _sem

                    if _sem.is_enabled():
                        sem_cached = await _sem.lookup_similar_async(
                            body.get("model", ""),
                            body.get("messages", []),
                            client_id=client_id or "",
                            params=key_params(body),
                        )
                        if sem_cached:
                            meta = sem_cached.get("_cache") or {}
                            sim = meta.get("similarity")
                            layer = "EMBED" if meta.get("embedding") else "MINHASH"
                            log.info(
                                "Semantic Cache HIT | layer=%s | model=%s | sim=%s | client_id=%s",
                                layer,
                                body.get("model", "?"),
                                sim,
                                client_id or "-",
                            )
                            resp = JSONResponse(
                                content=_for_caller(sem_cached, client_id)
                            )
                            resp.headers["X-Cache"] = f"SEMANTIC-HIT-{layer}"
                            if sim is not None:
                                resp.headers["X-Cache-Similarity"] = str(sim)
                            await core._pad_cache_hit_response(
                                _request_t0
                            )  # ARCH-DEBT-026
                            return resp
                except Exception as exc:
                    log.warning("Semantic cache lookup error (non-fatal): %s", exc)
            if cached:
                log.info(
                    "Cache HIT | model=%s | stream=%s | client_id=%s",
                    body.get("model", "?"),
                    _is_streaming,
                    client_id or "-",
                )
                if not _is_streaming:
                    resp = JSONResponse(content=_for_caller(cached, client_id))
                    resp.headers["X-Cache"] = "HIT"
                    await core._pad_cache_hit_response(_request_t0)  # ARCH-DEBT-026
                    return resp
                # Stream replay from cache ONLY for subprocess providers — HTTP
                # providers are expected to stream live tokens from their backend.
                try:
                    _replay_provider, _ = core.resolve_provider_and_model(
                        body.get("model", "")
                    )
                    _replay_allowed = str(
                        _replay_provider.get("auth_source", "")
                    ).startswith("subprocess:")
                except Exception:
                    _replay_allowed = False
                if not _replay_allowed:
                    # Skip the cache on stream requests for non-subprocess providers —
                    # fall through to the live streaming path below.
                    _cache_key = None
                else:
                    # Replay the cached non-stream response as SSE chunks.
                    pass  # fall into the replay block
            if cached and _cache_key is not None and _is_streaming:
                # (subprocess-only replay path)
                return StreamingResponse(
                    _replay_completion_as_sse(
                        cached,
                        completion_id=cached.get("id", "chatcmpl-cache"),
                        created=cached.get("created", int(time.time())),
                        model=cached.get("model", body.get("model", "")),
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "X-Cache": "HIT",
                    },
                )

        # BUG-52: a budgeted key holds this call's estimated cost before any
        # provider is called (429 when it does not fit, 503 when usage cannot be
        # written); the hold is settled when the call is billed, else released
        # once the response is sent (_session_turn_dep).
        if emulated:
            await core._hold_budget(
                request, client_id, key_record, body, cli_tools=True
            )
        else:
            await core._hold_budget(request, client_id, key_record, body)

        if body.get("stream") is True:
            # Fast path: subprocess providers execute synchronously (CLI returns
            # the full text), so we can call forward() to populate the non-stream
            # cache AND replay the result as SSE. Future identical requests will
            # hit the cache instantly (see the "Cache HIT + stream=true" branch
            # above) — saving ~10s per repeat query.
            try:
                _provider_for_stream, _ = core.resolve_provider_and_model(
                    body.get("model", "")
                )
                _stream_auth = str(_provider_for_stream.get("auth_source", ""))
                # A CLI that writes its answer as it goes (claude, agy) is streamed
                # live by forward_stream, the first word at once (INC-2): the
                # answer is not cached by this path. A CLI that cannot is run whole
                # here so repeats hit the cache.
                from plugboard import cli_subprocess as _cli

                _stream_bypass = _stream_auth.startswith("subprocess:") and not (
                    _cli.can_stream(_stream_auth.removeprefix("subprocess:"))
                )
            except Exception:
                _stream_bypass = False
            # ADR-039: tools emulated on a CLI route are read from the whole
            # answer, so even a CLI that streams (claude, agy) is run whole.
            _stream_bypass = _stream_bypass or emulated

            if _stream_bypass:
                try:
                    result = await _unless_client_gone(
                        request,
                        core.forward({**body, "stream": False}, **forward_options),
                    )
                except _ClientGone:
                    return _client_gone_response(body.get("model", "?"))
                except HTTPException:
                    raise
                except Exception as exc:
                    return core._chat_error_response(
                        exc, client_id, "subprocess stream"
                    )

                # Billed as a plain chat; the replay ends with what was billed.
                reported_usage = dict(result.get("usage") or {})
                _billing_for_cache = await core._charge_answer(
                    request, client_id, key_record, spending_total, billed(), result
                )
                if _billing_for_cache:
                    result["billing"] = _billing_for_cache
                    result["usage"] = chat_billing.usage_block(
                        _billing_for_cache,
                        reported_usage if _usage_reported() else None,
                    )

                # Write to cache (same cache the non-stream branch uses, so future
                # requests — stream or not — hit instantly).
                if _use_cache and _cache_key:
                    try:
                        from plugboard.cache import set_cached

                        _cost = _billing_for_cache.get("cost_usd", 0)
                        await _asyncio.to_thread(
                            set_cached,
                            _cache_key,
                            body.get("model", ""),
                            _cache_key[:16],
                            result,
                            cost_usd=_cost,
                            client_id=client_id or "",
                        )
                        try:
                            from plugboard import semantic_cache as _sem
                            from plugboard.cache import key_params as _cache_key_params

                            await _sem.store_async(
                                body.get("model", ""),
                                body.get("messages", []),
                                result,
                                cost_usd=_cost,
                                client_id=client_id or "",
                                params=_cache_key_params(body),
                            )
                        except Exception as exc:
                            log.warning(
                                "Semantic cache write error (non-fatal): %s", exc
                            )
                    except Exception as exc:
                        log.warning("Cache write error (non-fatal): %s", exc)

                # Replay as SSE: the answer may come from the provider that served
                # the request in the CLI's place, tool calls included.
                content = (result.get("choices") or [{}])[0].get("message", {}).get(
                    "content", ""
                ) or ""
                core._record_session_turn(
                    client_id, session_id, list(body.get("messages") or []), content
                )
                return StreamingResponse(
                    _replay_completion_as_sse(
                        result,
                        completion_id=result.get("id", "chatcmpl-subproc"),
                        created=result.get("created", int(time.time())),
                        model=result.get("model", body.get("model", "")),
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "X-Cache": "MISS",
                    },
                )

            try:
                gen = core.forward_stream(body, **route_options)
                first = await _unless_client_gone(request, gen.__anext__())
            except _ClientGone:
                await core._aclose_quietly(gen)
                return _client_gone_response(body.get("model", "?"))
            except StopAsyncIteration:
                return StreamingResponse(
                    iter([]),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            except HTTPException:
                raise
            except Exception as exc:
                return core._chat_error_response(exc, client_id, "stream")

            _key_id = getattr(request.state, "key_id", "")
            _hold = getattr(request.state, "budget_hold", None)

            async def _stream_and_bill(first_chunk: bytes, rest, cid, req_body, kid):
                """Yield chunks in real-time and append a final usage chunk.

                OpenAI's `stream_options.include_usage` is honoured by OpenAI
                and GitHub Models but NOT by Ollama or most other providers.
                Without this injection, the Playground
                never sees tokens/cost for those upstreams.

                We must preserve the real-time streaming UX — so each complete
                event goes out as soon as it is in; only an event cut by a read,
                and `[DONE]` with what follows, are held back (``tail_buf``), so
                the usage chunk can be spliced in before `[DONE]` (OpenAI
                clients stop reading there) and a cut stream ends between two
                events (PLU-26).
                """
                import json as _json_mod

                accumulated = bytearray(first_chunk)
                # The first chunk is already consumed by the caller's probe; a
                # short answer can arrive whole in this one read.
                tail_buf = bytearray(first_chunk)
                ready = _complete_events(tail_buf)
                if ready:
                    yield ready
                completed = False
                cut: Optional[Exception] = None
                try:
                    async for chunk in rest:
                        accumulated.extend(chunk)
                        tail_buf.extend(chunk)
                        ready = _complete_events(tail_buf)
                        if ready:
                            yield ready
                    completed = True
                except Exception as exc:
                    if _done_event_at(tail_buf) != -1:
                        # The provider had finished: a late read error is moot.
                        completed = True
                    else:
                        # The provider broke off after the first chunk (PLU-26):
                        # no other provider can take over a stream already begun.
                        cut = exc
                        log.warning("Stream cut by the provider: %s", exc)
                finally:
                    if not completed:
                        # Client gone (cancellation or generator close) or upstream
                        # read error: the provider has already generated these
                        # tokens, so record them before letting the error through.
                        if cid:
                            try:
                                # On the loop: a cancelled request cannot await
                                # here (the rare path; PLU-27 moves the others).
                                core._bill_streaming(
                                    cid,
                                    req_body,
                                    bytes(accumulated),
                                    key_id=kid,
                                    hold=_hold,
                                    # Counts only when the usage event came before
                                    # the cut; without it this changes nothing.
                                    reported=_usage_reported(),
                                )
                                log.info(
                                    "Stream interrupted: billed the partial answer "
                                    "(%d bytes)",
                                    len(accumulated),
                                )
                            except Exception as exc:
                                log.warning("Stream billing error (non-fatal): %s", exc)
                                _keep_hold(_hold)
                        await core._aclose_quietly(rest)
                if cut is not None:
                    # What came, in whole events, then a readable end: an error
                    # event and [DONE], never the provider's own text (it may name
                    # its address). An event the cut broke in two is dropped.
                    yield _stream_cut_event()
                    yield b"data: [DONE]\n\n"
                    return
                # At this point `tail_buf` holds what was held back (an event cut
                # by a read, `[DONE]` and what follows) and has NOT been yielded.

                bill = None
                cancelled: Optional[BaseException] = None
                if cid:
                    if _hold is not None:
                        _hold.settling = True  # never given back while it is billed
                    try:
                        # Off the loop, waited for to the end (PLU-27): a client
                        # that hangs up now cannot leave the hold given back and
                        # the session's turn unrecorded before the usage is written.
                        bill, cancelled = await _to_thread_to_the_end(
                            functools.partial(
                                core._bill_streaming,
                                cid,
                                req_body,
                                bytes(accumulated),
                                key_id=kid,
                                hold=_hold,
                                # Read here, in the task that relayed the stream:
                                # the route set it when the CLI's answer ended.
                                reported=_usage_reported(),
                            )
                        )
                    except Exception as exc:
                        log.warning("Stream billing error (non-fatal): %s", exc)
                        _keep_hold(_hold)
                if bill is not None and cid and cid != "admin":
                    # As a whole answer is: the spending alert follows its billing
                    # (a CLI stream used to be billed through forward() and had it).
                    try:
                        core._send_spending_alert(
                            cid,
                            bill["pricing_month"],
                            key_record=key_record,
                            current_total_usd=spending_total + bill["cost_usd"],
                        )
                    except Exception as exc:
                        log.warning("Spending alert error (non-fatal): %s", exc)
                if bill is not None:
                    # Complete stream only: an interrupted one left above.
                    core._record_session_turn(
                        cid,
                        session_id,
                        list(req_body.get("messages") or []),
                        bill["text"],
                    )
                if cancelled is not None:
                    raise cancelled

                usage_needed = bill is not None and not bill["upstream_usage_present"]
                if not usage_needed:
                    if tail_buf:
                        yield bytes(tail_buf)
                    return

                usage_evt = {
                    "id": "chatcmpl-injected",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": bill["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": bill["prompt_tokens"],
                        "completion_tokens": bill["completion_tokens"],
                        "total_tokens": bill["total_tokens"],
                        "cost_usd": bill["cost_usd"],
                    },
                }
                usage_sse = f"data: {_json_mod.dumps(usage_evt)}\n\n".encode("utf-8")

                done_idx = _done_event_at(tail_buf)
                if done_idx == -1:
                    if tail_buf:
                        yield bytes(tail_buf)
                    yield usage_sse
                else:
                    yield bytes(tail_buf[:done_idx])
                    yield usage_sse
                    yield bytes(tail_buf[done_idx:])

            return StreamingResponse(
                _stream_and_bill(first, gen, client_id, body, _key_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        t0 = time.perf_counter()
        try:
            result = await _unless_client_gone(
                request, core.forward(body, **forward_options)
            )
        except _ClientGone:
            return _client_gone_response(body.get("model", "?"))
        except HTTPException:
            raise
        except Exception as exc:
            return core._chat_error_response(exc, client_id, "forward")
        elapsed = time.perf_counter() - t0
        log.info("<- OK | model=%s | %.2fs", body.get("model", "?"), elapsed)

        # -- Billing --------------------------------------------------------------
        billing_info = await core._charge_answer(
            request, client_id, key_record, spending_total, billed(), result
        )

        # Inject billing into response + headers
        if billing_info:
            result["billing"] = billing_info

        if session_id and client_id:
            assistant_text = ""
            try:
                choice = (result.get("choices") or [{}])[0]
                assistant_text = (choice.get("message") or {}).get("content") or ""
            except (AttributeError, IndexError, TypeError):
                pass
            core._record_session_turn(
                client_id,
                session_id,
                list(body.get("messages", []) or []),
                assistant_text,
            )

        # Cache the response
        if _use_cache and _cache_key:
            try:
                from plugboard.cache import set_cached

                cost = billing_info.get("cost_usd", 0) if billing_info else 0
                await _asyncio.to_thread(
                    set_cached,
                    _cache_key,
                    body.get("model", ""),
                    _cache_key[:16],
                    result,
                    cost_usd=cost,
                    client_id=client_id or "",
                )
                try:
                    from plugboard import semantic_cache as _sem
                    from plugboard.cache import key_params as _cache_key_params

                    await _sem.store_async(
                        body.get("model", ""),
                        body.get("messages", []),
                        result,
                        cost_usd=cost,
                        client_id=client_id or "",
                        params=_cache_key_params(body),
                    )
                except Exception as exc:
                    log.warning("Semantic cache write error (non-fatal): %s", exc)
            except Exception as exc:
                log.warning("Cache write error (non-fatal): %s", exc)

        resp = JSONResponse(content=_for_caller(result, client_id))
        resp.headers["X-Cache"] = "MISS"
        if billing_info:
            resp.headers["X-Cost-USD"] = str(billing_info["cost_usd"])
            resp.headers["X-Prompt-Tokens"] = str(billing_info["prompt_tokens"])
            resp.headers["X-Completion-Tokens"] = str(billing_info["completion_tokens"])
            # Which grid entry priced the call: useful to admins, not shown to
            # client keys (it reveals provider routing).
            if client_id == "admin" and billing_info.get("price_key"):
                resp.headers["X-Price-Key"] = billing_info["price_key"]
        return resp

    return SimpleNamespace(
        _record_session_turn=_record_session_turn,
        chat_completions=chat_completions,
    )
