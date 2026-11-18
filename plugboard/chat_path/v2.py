"""The /v2 aliases of the chat and model list routes.

POST /v2/chat/completions, GET /v2/models and GET /v2/models/benchmarks answer
as their /v1 routes, marked with the API version. Moved verbatim from
``plugboard/app.py`` (PLU-95), behaviour unchanged. ``core`` is the
``plugboard.app`` module that registers the routes: the dependencies
(``_resolve_client``, ``_refuse_anonymous_chat``, ``_inflight_chat_dep``,
``_session_turn_dep``) and the /v1 handlers (``chat_completions``,
``_models_for_key``) re-exported by ``app.py`` are read as
``core.<name>`` when they run.
"""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Optional

from fastapi import Depends, FastAPI, Request

# -- /v2/ aliases (API versioning) -------------------------------------------
# v2 endpoints are identical to v1 for now. The version header allows
# clients to opt-in to future breaking changes.


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    @app.post(
        "/v2/chat/completions",
        # The key first (PLU-15): see _inflight_chat_dep.
        dependencies=[
            Depends(core._resolve_client),
            Depends(core._refuse_anonymous_chat),
            Depends(core._inflight_chat_dep),
            Depends(core._session_turn_dep),
        ],
    )
    async def chat_completions_v2(
        request: Request, client_id: Optional[str] = Depends(core._resolve_client)
    ):
        resp = await core.chat_completions(request, client_id)
        resp.headers["X-API-Version"] = "2"
        return resp

    @app.get("/v2/models")
    async def models_v2(
        request: Request, client_id: Optional[str] = Depends(core._resolve_client)
    ):
        items = await core._models_for_key(request, client_id)
        return {"object": "list", "data": items, "api_version": "2"}

    @app.get("/v2/models/benchmarks")
    async def models_benchmarks_v2(
        request: Request, client_id: Optional[str] = Depends(core._resolve_client)
    ):
        from plugboard.benchmarks import enrich_models_list

        items = await core._models_for_key(request, client_id)
        return {"object": "list", "data": enrich_models_list(items), "api_version": "2"}

    return SimpleNamespace(models_v2=models_v2)
