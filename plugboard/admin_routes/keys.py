"""Client keys and organisations (admin).

Routes ``/admin/keys``, ``/admin/keys/client/{client_id}``,
``/admin/keys/{key_id}/revoke``, ``/admin/keys/{key_id}`` (DELETE),
``/admin/keys/{key_id}/rotate``, ``/admin/orgs``, ``/admin/orgs/{org_id}/keys``
and ``/admin/orgs/{org_id}/usage/{month}``, moved from ``plugboard/app.py``
(PLU-36), behaviour unchanged. ``core`` is the ``plugboard.app`` module that
registers them: ``_require_admin``, ``_audit``, ``_month_param``, ``_usage``
and ``load_providers`` are read as ``core.<name>`` when a handler runs.
"""

from __future__ import annotations

import logging
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from plugboard import envcompat
from plugboard.apikeys import (
    KeyRevoked,
    create_key,
    delete_key,
    get_keys_by_client,
    get_keys_by_org,
    get_orgs,
    load_keys,
    mask_key,
    refused_client_reason,
    revoke_key,
    rotate_key,
)
from plugboard.legacy_connectors import is_removed_provider
from plugboard.router import is_unknown_model
from plugboard.webhooks import emit_fire_and_forget as _webhook

log = logging.getLogger("plugboard.app")


class KeyCreate(BaseModel):
    client_id: str
    label: str = ""
    # None = let the server apply the production-safe env default
    # (PLUGBOARD_DEFAULT_MONTHLY_LIMIT_USD / PLUGBOARD_DEFAULT_RPM_LIMIT).
    # Explicit 0 / 0.0 means "opt out — truly unlimited".
    monthly_limit_usd: Optional[float] = None
    allowed_models: Optional[List[str]] = None
    allowed_decision_models: Optional[
        List[Literal["jev", "laya", "laya-multilingual"]]
    ] = None
    rpm_limit: Optional[int] = None  # calls to a model per minute (BUG-124)
    org_id: str = ""  # organisation (empty = no org)

    @field_validator("client_id")
    @classmethod
    def _not_the_admin(cls, value: str) -> str:
        # A key resolves to its client_id, and "admin" is the admin's identity
        # (every in-flight call, no model restriction): never issue it (BUG-60).
        if value.strip().lower() == "admin":
            raise ValueError("client_id 'admin' is reserved for the administrator")
        return value

    @field_validator("client_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # The chat routes read an empty client_id as "no caller": no budget,
        # rpm_limit or allowed_models, nothing billed (BUG-64).
        if not value.strip():
            raise ValueError("client_id must not be blank")
        return value

    @field_validator("allowed_models")
    @classmethod
    def _models_named(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        # An entry that names no model grants nothing ("", "Codex:",
        # "<provider>:"), and one with surrounding spaces matches no ID
        # (BUG-59): a typo would leave the key without the model it needs.
        for entry in value or []:
            if not entry.strip() or entry.endswith(":"):
                raise ValueError(f"allowed_models entry {entry!r} names no model")
            if entry != entry.strip():
                raise ValueError(
                    f"allowed_models entry {entry!r} has surrounding spaces"
                )
        return value


def _default_rpm_limit() -> int:
    """Production-safe default applied when a key is created without an
    explicit rpm_limit. Operators can raise/lower via env without code change."""
    return max(0, envcompat.get_int("PLUGBOARD_DEFAULT_RPM_LIMIT", default=60))


def _default_monthly_limit_usd() -> float:
    """Production-safe default applied when a key is created without an
    explicit monthly_limit_usd. 0 disables the default entirely."""
    return max(
        0.0, envcompat.get_float("PLUGBOARD_DEFAULT_MONTHLY_LIMIT_USD", default=10.0)
    )


def _admin_key_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """A key record as the admin API shows it: never its hash (BUG-59)."""
    view = {k: v for k, v in record.items() if k != "key_hash"}
    if "key" not in record:
        view["key_display"] = mask_key(record.get("key_prefix", ""))
    return view


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    def _allowed_model_refusal(entry: str) -> Optional[str]:
        """Why an allowed_models entry would grant nothing, or None (BUG-59).

        Refused: an entry no provider knows (chat's own 404 model_not_found
        rule), and one a provider knows only under another spelling, since the
        guard compares exact IDs: an undated family name that chat promotes to a
        listed dated ID, or another case. A ``<provider-id>:<model>`` the
        provider does not list (yet) is accepted, as chat routes it there.
        """
        from plugboard.model_identity import _DISPLAY_OWNERS, registered_model_owner
        from plugboard.router import _family_aliases, _listed_models

        if is_unknown_model(entry):
            return f"no provider knows {entry!r}; use an ID GET /v1/models lists"
        registry = [p for p in core.load_providers() if not is_removed_provider(p)]
        ids = [p["id"] for p in registry]
        owner = registered_model_owner(entry, ids)
        if owner is None and ":" in entry:
            alias = entry.split(":", 1)[0]
            owner = next((o for o, a in _DISPLAY_OWNERS.items() if a == alias), None)
        native = (
            entry[len(owner) + 1 :]
            if owner and entry.startswith(owner + ":")
            else entry
        )
        if owner and native == entry:  # a display prefix (Codex:...)
            native = entry.split(":", 1)[1]
        candidates = [p for p in registry if owner is None or p["id"] == owner]
        for provider in candidates:
            names = _listed_models(provider) + [provider.get("default_model") or ""]
            if native in names:
                return None
        for provider in candidates:
            names = [n for n in _listed_models(provider) if n]
            if native in _family_aliases(provider) and native not in names:
                return (
                    f"{entry!r} is served as a dated ID of {provider['id']}: "
                    "list that ID (GET /v1/models)"
                )
            exact = next((n for n in names if n.casefold() == native.casefold()), None)
            if exact is not None:
                return f"{entry!r} is spelled {exact!r} by {provider['id']}"
        return None

    @app.get("/admin/keys")
    async def list_api_keys(_: None = Depends(core._require_admin)):
        return [_admin_key_view(k) for k in load_keys()]

    @app.post("/admin/keys", status_code=201)
    async def create_api_key(body: KeyCreate, _: None = Depends(core._require_admin)):
        """Creates a new client key. The key is shown in plaintext ONLY in this response."""
        refusals = [
            r for r in map(_allowed_model_refusal, body.allowed_models or []) if r
        ]
        if refusals:
            raise HTTPException(422, "allowed_models: " + "; ".join(refusals))
        rpm = body.rpm_limit if body.rpm_limit is not None else _default_rpm_limit()
        budget = (
            body.monthly_limit_usd
            if body.monthly_limit_usd is not None
            else _default_monthly_limit_usd()
        )
        rec = create_key(
            client_id=body.client_id,
            label=body.label,
            monthly_limit_usd=budget,
            allowed_models=body.allowed_models,
            rpm_limit=rpm,
            org_id=body.org_id,
            allowed_decision_models=body.allowed_decision_models,
        )
        core._audit(
            "key.create",
            target_type="key",
            target_id=rec.get("key_id", ""),
            details=body.client_id,
        )
        await _webhook(
            "key.created", {"key_id": rec.get("key_id"), "client_id": body.client_id}
        )
        return _admin_key_view(rec)

    @app.get("/admin/keys/client/{client_id}")
    async def list_keys_by_client(
        client_id: str, _: None = Depends(core._require_admin)
    ):
        """List all keys for a specific client_id."""
        keys = get_keys_by_client(client_id)
        return [_admin_key_view(k) for k in keys]

    @app.post("/admin/keys/{key_id}/revoke", status_code=200)
    async def revoke_api_key(key_id: str, _: None = Depends(core._require_admin)):
        if not revoke_key(key_id):
            raise HTTPException(404, f"Key '{key_id}' not found")
        core._audit("key.revoke", target_type="key", target_id=key_id)
        await _webhook("key.revoked", {"key_id": key_id})
        return {"key_id": key_id, "is_active": False}

    @app.delete("/admin/keys/{key_id}", status_code=204)
    async def delete_api_key(key_id: str, _: None = Depends(core._require_admin)):
        if not delete_key(key_id):
            raise HTTPException(404, f"Key '{key_id}' not found")
        core._audit("key.delete", target_type="key", target_id=key_id)

    @app.post("/admin/keys/{key_id}/rotate", status_code=201)
    async def rotate_api_key(key_id: str, _: None = Depends(core._require_admin)):
        """Rotate a key: revoke the old one, create a new one with same settings."""
        old = next((k for k in load_keys() if k.get("key_id") == key_id), None)
        if old is not None and refused_client_reason(old):
            # The new key would carry the same blank or "admin" client_id
            # (BUG-64, BUG-60, D-40).
            raise HTTPException(
                422,
                "This key's client ID is empty or reserved and the key is refused "
                "when used: revoke it and create a new key with a valid client ID",
            )
        try:
            result = rotate_key(key_id)
        except KeyRevoked:
            raise HTTPException(
                409,
                f"Key '{key_id}' is already revoked: rotate its replacement, or "
                "create a new key",
            ) from None
        except ValueError as exc:
            # The new key cannot be written (a retired decision model): the old
            # key is untouched (BUG-59).
            raise HTTPException(
                422,
                f"This key cannot be rotated ({exc}): create a new key, then revoke this one",
            ) from None
        if result is None:
            raise HTTPException(404, f"Key '{key_id}' not found")
        core._audit("key.rotate", target_type="key", target_id=key_id)
        await _webhook("key.rotated", {"key_id": key_id})
        return _admin_key_view(result)

    # -- Admin: Organisations ----------------------------------------------------

    @app.get("/admin/orgs")
    async def list_orgs(_: None = Depends(core._require_admin)):
        """List all organisations with key counts."""
        return get_orgs()

    @app.get("/admin/orgs/{org_id}/keys")
    async def list_org_keys(org_id: str, _: None = Depends(core._require_admin)):
        """List all keys for an organisation."""
        keys = get_keys_by_org(org_id)
        return [_admin_key_view(k) for k in keys]

    @app.get("/admin/orgs/{org_id}/usage/{month}")
    async def org_usage(
        org_id: str, month: str, _: None = Depends(core._require_admin)
    ):
        """Aggregate usage for all clients in an organisation."""
        month = core._month_param(month)
        keys = get_keys_by_org(org_id)
        client_ids = {k.get("client_id") for k in keys}
        all_entries = []
        for cid in client_ids:
            all_entries.extend(core._usage.get_usage_by_client(month, cid))
        return {
            "org_id": org_id,
            "month": month,
            "clients": sorted(client_ids),
            "entries": all_entries,
            "summary": core._usage.summarize_usage(all_entries),
        }

    return SimpleNamespace(
        _default_rpm_limit=_default_rpm_limit,
        _default_monthly_limit_usd=_default_monthly_limit_usd,
    )
