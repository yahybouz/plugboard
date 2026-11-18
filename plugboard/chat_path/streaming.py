"""Client hang-up and SSE events of the chat path.

Watching for the caller hanging up while the upstream works (BUG-30), cutting a
provider's stream into whole SSE events, the event that ends a stream broken
off by the provider (PLU-26), and closing an upstream stream quietly. Moved
verbatim from ``plugboard/app.py`` (PLU-95), behaviour unchanged. ``core`` is
the ``plugboard.app`` module that registers ``_aclose_quietly``:
``_background_tasks``, which stayed there, is read as ``core._background_tasks``
when it runs.
"""

from __future__ import annotations

import asyncio as _asyncio
import json
import logging
from types import ModuleType, SimpleNamespace
from typing import Any, Awaitable, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import Response

log = logging.getLogger("plugboard.app")


# ── Client hang-up while the upstream works (BUG-30) ────────────────────────
#
# The inflight slot is given back only once the response is sent, and nothing
# else listens to the client before the response starts: an abandoned call
# kept its slot, its CLI and its HTTP request until the upstream answered (up
# to 900 s for a CLI), then was billed and cached for nobody.

_CLIENT_CLOSED_REQUEST = 499  # nginx's code for "client closed request"
_T = TypeVar("_T")


class _ClientGone(Exception):
    """The caller hung up before the upstream answered."""


async def _unless_client_gone(request: Request, awaitable: Awaitable[_T]) -> _T:
    """Await upstream work, and cancel it as soon as the caller hangs up.

    The work runs in the calling task on purpose: ``forward()`` sets the
    router's context variables that billing reads afterwards. A watcher task
    cancels this task on ``http.disconnect``; the cancellation kills a CLI's
    process group (cli_subprocess) and closes an HTTP request, then becomes
    ``_ClientGone``. Only valid once the request body has been read.
    """
    task = _asyncio.current_task()
    if task is None:  # pragma: no cover - always called from a request task
        return await awaitable
    hung_up = False
    finished = False

    async def _watch() -> None:
        nonlocal hung_up
        try:
            message = await request.receive()
        except Exception as exc:  # the server cannot tell: keep the old way
            log.debug("Client watch stopped: %s", exc)
            return
        # After the body, an ASGI server only ever sends http.disconnect.
        # Once the work is over, a hang-up changes nothing: BaseHTTPMiddleware's
        # receive (an anyio task group) can swallow watcher.cancel() and still
        # return the disconnect, which must not cancel the finished request.
        if message.get("type") == "http.disconnect" and not finished:
            hung_up = True
            task.cancel()

    watcher = _asyncio.ensure_future(_watch())
    try:
        result = await awaitable
    except (_asyncio.CancelledError, Exception):
        # Withdraw our own cancellation request (the work may also have turned
        # it into an error); any other cancellation keeps propagating.
        if hung_up and task.uncancel() == 0:
            raise _ClientGone() from None
        raise
    finally:
        finished = True  # before the first await: see _watch
        watcher.cancel()
        await _asyncio.wait([watcher])
    if hung_up:
        # The work swallowed the cancellation: the caller is gone all the same.
        task.uncancel()
        raise _ClientGone()
    return result


_EVENT_ENDS = (b"\n\n", b"\r\n\r\n")


_DONE = b"data: [DONE]"
# Held without an event end past this, a provider's stream goes out as it
# comes (all but its last bytes): a stream that never ends its events must
# not be buffered whole.
_MAX_HELD = 1 << 16


def _done_event_at(buf: bytearray) -> int:
    """Where the `data: [DONE]` event starts in ``buf``, -1 when there is
    none. Only a whole event counts: at the start of ``buf`` (which starts
    at an event's start) or right after an event end, followed by a line
    end or nothing yet; `data: [DONE]` quoted inside an event's JSON does
    not."""
    end = len(buf)
    while True:
        at = buf.rfind(_DONE, 0, end)
        if at == -1:
            return -1
        after = buf[at + len(_DONE) : at + len(_DONE) + 1]
        starts_event = at == 0 or any(buf.endswith(sep, 0, at) for sep in _EVENT_ENDS)
        if starts_event and after in (b"", b"\n", b"\r"):
            return at
        end = at


def _complete_events(buf: bytearray) -> bytes:
    """Take from ``buf`` the complete SSE events that may go out now: up to
    the last event end, but never the `data: [DONE]` event nor what follows
    it. What is left stays in ``buf``."""
    done = _done_event_at(buf)
    limit = len(buf) if done == -1 else done
    end = 0
    for sep in _EVENT_ENDS:
        at = buf.rfind(sep, 0, limit)
        if at != -1:
            end = max(end, at + len(sep))
    if not end and done == -1 and len(buf) > _MAX_HELD:
        end = len(buf) - 64  # no event end in sight: let it flow
    if not end:
        return b""
    out = bytes(buf[:end])
    del buf[:end]
    return out


def _stream_cut_event() -> bytes:
    """The SSE event that ends a stream the provider broke off after its
    first chunk (PLU-26), in OpenAI's streamed error shape."""
    event = {
        "error": {
            "message": (
                "The provider stopped the answer before its end: what came "
                "before this event is all there is."
            ),
            "type": "server_error",
            "code": "stream_interrupted",
        }
    }
    return f"data: {json.dumps(event)}\n\n".encode("utf-8")


def _client_gone_response(model: str) -> Response:
    """Answer nobody reads: it only lets the inflight slot go at once."""
    log.info(
        "Client hung up before the answer: upstream stopped, nothing billed | model=%s",
        model,
    )
    return Response(status_code=_CLIENT_CLOSED_REQUEST)


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Define this block's closures, which read ``core`` when they run."""

    async def _aclose_quietly(agen: Any) -> None:
        """Close an upstream async generator, even inside a cancelled task.

        Starlette cancels the response task when the client disconnects, and
        keeps cancelling it on every await. The close runs in its own task, so
        the upstream generator still releases its HTTP connection; if we are
        cancelled meanwhile, the cancellation propagates and the close finishes
        on its own.
        """
        aclose = getattr(agen, "aclose", None)
        if aclose is None:
            return
        task = _asyncio.ensure_future(aclose())
        core._background_tasks.add(task)
        task.add_done_callback(core._background_tasks.discard)
        # Retrieve the result so an error in a close nobody awaits is not
        # reported as "exception never retrieved".
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        try:
            await _asyncio.shield(task)
        except _asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("Upstream stream close error (ignored): %s", exc)

    return SimpleNamespace(_aclose_quietly=_aclose_quietly)
