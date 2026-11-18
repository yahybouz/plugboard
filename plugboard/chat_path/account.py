"""The client account routes.

GET /v1/inflight, GET /admin/inflight, GET /usage/me, GET /v1/me and
GET /v1/me/keys. Moved verbatim from ``plugboard/app.py`` (PLU-95), behaviour
unchanged. ``core`` is the ``plugboard.app`` module that registers the routes:
the dependencies (``_resolve_client``, ``_require_admin``) and the names left
in ``app.py`` (``_get_request_key_record``, ``_usage``) are read as
``core.<name>`` when they run.
"""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request

from plugboard import billing as _billing
from plugboard import live_requests as _live
from plugboard.apikeys import get_keys_by_client, load_keys, mask_key

# The usage entry fields /usage/me answers (contract 3.0.1), named one by one:
# a column added to usage_entries stays out until it is named here. Left out
# on purpose: price_key and price_source (they reveal provider routing) and
# request_id and trace_id (PLU-106, not in the client contract).
_CLIENT_USAGE_FIELDS = (
    "id",
    "ts",
    "month",
    "client_id",
    "key_id",
    "model",
    "provider_id",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "pricing_month",
    "cost_usd",
)


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    @app.get("/v1/inflight")
    async def inflight_me(client_id: Optional[str] = Depends(core._resolve_client)):
        """Chat calls in flight and the last results (PLU-13).

        The admin sees every call with its provider; a client key sees its own
        calls only, without routing details.
        """
        if client_id is None:
            raise HTTPException(401, "Authentication required")
        return _live.snapshot(client_id=client_id, admin=client_id == "admin")

    @app.get("/admin/inflight", dependencies=[Depends(core._require_admin)])
    async def inflight_admin():
        """Every chat call in flight and the last results, with routing details."""
        return _live.snapshot(admin=True)

    @app.get("/usage/me")
    async def usage_me(
        month: Optional[str] = None,
        client_id: Optional[str] = Depends(core._resolve_client),
    ):
        if client_id is None:
            return {"entries": [], "summary": {}}
        m = month or _billing.current_pricing_month()
        entries = [
            {k: e[k] for k in _CLIENT_USAGE_FIELDS if k in e}
            for e in core._usage.get_usage_by_client(m, client_id)
        ]
        return {
            "month": m,
            "client_id": client_id,
            "entries": entries,
            "summary": core._usage.summarize_usage(entries),
        }

    @app.get("/v1/me")
    async def client_info(
        request: Request, client_id: Optional[str] = Depends(core._resolve_client)
    ):
        if not client_id:
            return {"client_id": None}
        info: Dict[str, Any] = {"client_id": client_id}
        key_record = core._get_request_key_record(request)
        if key_record:
            info["key_id"] = key_record.get("key_id", "")
            info["monthly_limit_usd"] = key_record.get("monthly_limit_usd", 0)
            info["allowed_models"] = key_record.get("allowed_models", [])
            info["rpm_limit"] = key_record.get("rpm_limit", 0)
            info["org_id"] = key_record.get("org_id", "")
        else:
            for k in load_keys():
                if k.get("client_id") == client_id:
                    info["monthly_limit_usd"] = k.get("monthly_limit_usd", 0)
                    info["allowed_models"] = k.get("allowed_models", [])
                    info["rpm_limit"] = k.get("rpm_limit", 0)
                    info["org_id"] = k.get("org_id", "")
                    break

        month = _billing.current_pricing_month()
        key_id = info.get("key_id", "")
        if key_id:
            summary = core._usage.summarize_usage_by_key(month, key_id)
        else:
            summary = core._usage.summarize_usage_by_client(month, client_id)
        info["current_month"] = month
        info["usage"] = summary
        limit = info.get("monthly_limit_usd", 0)
        info["remaining_usd"] = (
            max(0, limit - summary["total_cost_usd"]) if limit and limit > 0 else None
        )
        return info

    @app.get("/v1/me/keys")
    async def client_keys(client_id: Optional[str] = Depends(core._resolve_client)):
        """List the calling client's own keys (masked)."""
        if not client_id:
            return []
        keys = get_keys_by_client(client_id)
        return [
            {
                "key_id": k["key_id"],
                "key_display": mask_key(k.get("key_prefix", "")),
                "label": k.get("label", ""),
                "is_active": k.get("is_active", True),
                "created_at": k.get("created_at", ""),
            }
            for k in keys
        ]

    return SimpleNamespace()
