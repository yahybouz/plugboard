"""Provider management (admin).

Routes ``/providers`` and ``/providers/{provider_id}`` (update, delete, health,
discover), moved from ``plugboard/app.py`` (PLU-36), behaviour unchanged.
``core`` is the ``plugboard.app`` module that registers them: the names the
tests patch there, and the outbound-address guard that stays there
(``_require_parsable_base_url``, ``_check_guard_on_edit``,
``_require_guard_allows``, shared with the direct providers), are read as
``core.<name>`` when a handler runs.
"""

from __future__ import annotations

import json
from types import ModuleType, SimpleNamespace
from typing import Annotated, Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from plugboard import config, netguard
from plugboard import live_requests as _live
from plugboard.config import (
    create_provider,
    delete_provider,
    mask_token,
)
from plugboard.legacy_connectors import (
    REMOVED_CONNECTOR_ERROR,
    is_removed_provider,
    require_supported_provider,
)
from plugboard.webhooks import emit_fire_and_forget as _webhook


class ProviderCreate(BaseModel):
    name: str
    base_url: str
    bearer_token: str
    default_model: str = ""
    is_active: bool = True
    timeout: float = 60.0
    provider_type: str = "cloud"
    auth_header_name: str = "Authorization"
    auth_header_prefix: str = "Bearer"
    # Public addresses only (D-18 c): unticked by default for a manual provider.
    egress_guard: bool = False


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    bearer_token: Optional[str] = None
    default_model: Optional[str] = None
    is_active: Optional[bool] = None
    timeout: Optional[float] = None
    provider_type: Optional[str] = None
    auth_header_name: Optional[str] = None
    auth_header_prefix: Optional[str] = None
    models: Optional[List[Dict[str, Any]]] = None
    # Refresh this provider's model list automatically (GET /models).
    auto_discover: Optional[bool] = None
    # Models never removed by an automatic refresh.
    pinned_models: Optional[
        List[Annotated[str, Field(min_length=1, max_length=200)]]
    ] = Field(default=None, max_length=500)
    egress_guard: Optional[bool] = None
    # Requests the provider runs at once; 0 = no cap (D-22).
    max_concurrency: Optional[int] = Field(default=None, ge=0, le=1000)


def _mask(p: Dict[str, Any]) -> Dict[str, Any]:
    masked = {
        **p,
        "bearer_token": mask_token(p.get("bearer_token", "")),
        # The effective protection, not the stored field: catalogue and
        # built-in cloud presets are always guarded (egress_guard_forced).
        "egress_guard": netguard.is_protected(p),
        "egress_guard_forced": netguard.guard_is_forced(p),
    }
    if is_removed_provider(p):
        masked.update(
            is_active=False, migration_required=True, error=REMOVED_CONNECTOR_ERROR
        )
    return masked


def _admin_actor(request: Request) -> str:
    """Audit actor of an admin call: ``mcp`` from the MCP server, else ``admin``.

    The surface header is declarative (any admin can send it): it tells the
    MCP server's calls apart, it proves nothing (PB-MCP-02).
    """
    surface = _live.normalize_surface(request.headers.get(_live.SURFACE_HEADER))
    return "mcp" if surface == "mcp" else "admin"


def _refuse_unreadable_secret(provider: Dict[str, Any]) -> None:
    """409 for a provider turned off because its key cannot be decrypted.

    Ping and Discover called it with an empty token, and the admin read a
    connection or discovery error instead of why it is off (BUG-79).
    """
    if provider.get("secret_unreadable"):
        raise HTTPException(
            409,
            f"Provider '{provider.get('id', '')}' is turned off: its stored key "
            "cannot be decrypted with the current ENCRYPTION_KEY. Restore that "
            "key, or enter the provider's key again; nothing was called.",
        )


def _no_models_detail(
    provider_id: str,
    report: Optional[Dict[str, Any]],
    *,
    signed_out: str = "",
    probes_off_kind: str = "",
) -> str:
    """502 detail when a discovery kept no model: say why (admin endpoint)."""
    base = f"Could not discover models from '{provider_id}'"
    if signed_out:
        # The root cause: nothing can be listed or tested without an account.
        return f"{base}: {signed_out}"
    if report is None:
        return base
    candidates = int(report.get("candidate_count") or 0)
    if not candidates:
        if probes_off_kind:
            return (
                f"{base}: the {probes_off_kind} CLI cannot list its models and model "
                f"probes are off for it (add {probes_off_kind} to "
                "PLUGBOARD_MODEL_REFRESH_PROBE_KINDS)."
            )
        return f"{base}: no candidate model to test."
    tested = int(report.get("verified_count") or 0)
    reasons: List[str] = []
    for item in report.get("dropped") or []:
        text = str(item.get("detail") or item.get("reason") or "").strip()
        if text and text not in reasons:
            reasons.append(text)
    detail = f"{base}: {tested} of {candidates} models tested, none answered."
    if reasons:
        detail += " " + " | ".join(reasons[:2])
    return detail


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Declare this block's routes on ``app``, in their original order."""

    @app.get("/providers")
    async def get_providers(_: None = Depends(core._require_admin)):
        return [_mask(p) for p in core.load_providers()]

    @app.post("/providers", status_code=201)
    async def add_provider(
        body: ProviderCreate, request: Request, _: None = Depends(core._require_admin)
    ):
        actor = _admin_actor(request)
        if body.egress_guard:
            await core._require_guard_allows("", body.base_url, actor=actor)
        try:
            # The domain allowlist first, so its refusals stay audited (TM-004);
            # then an address that cannot be parsed, guard or not (BUG-66).
            config.check_provider_domain_allowed(body.base_url)
            core._require_parsable_base_url(body.base_url, "Provider base_url")
            record = create_provider(
                name=body.name,
                base_url=body.base_url,
                bearer_token=body.bearer_token,
                default_model=body.default_model,
                is_active=body.is_active,
                timeout=body.timeout,
                provider_type=body.provider_type,
                auth_header_name=body.auth_header_name,
                auth_header_prefix=body.auth_header_prefix,
                egress_guard=body.egress_guard,
            )
        except config.ProviderDomainNotAllowed as exc:
            core._audit(
                "provider.domain.rejected",
                actor=actor,
                target_type="provider",
                target_id="",  # provider was never created
                details=json.dumps(
                    {
                        "name": body.name,
                        "base_url": body.base_url,
                        "reason": str(exc),
                    }
                ),
            )
            raise HTTPException(400, str(exc))
        except ValueError as exc:
            if actor == "mcp":
                core._audit(
                    "provider.create.refused",
                    actor=actor,
                    target_type="provider",
                    details=json.dumps(
                        {
                            "name": body.name,
                            "base_url": body.base_url,
                            "status": 400,
                            "reason": str(exc),
                        }
                    ),
                )
            raise HTTPException(400, str(exc)) from None
        core.invalidate_models_cache()
        core._audit(
            "provider.create",
            actor=actor,
            target_type="provider",
            target_id=record["id"],
            details=body.name,
        )
        await _webhook(
            "provider.created", {"provider_id": record["id"], "name": body.name}
        )
        return _mask(record)

    @app.put("/providers/{provider_id}")
    async def patch_provider(
        provider_id: str, body: ProviderUpdate, _: None = Depends(core._require_admin)
    ):
        updates = body.model_dump(exclude_none=True)
        before = core.get_provider(provider_id)
        if before is not None:
            await core._check_guard_on_edit(provider_id, before, updates)
        try:
            if before is not None and "base_url" in updates:
                # As on creation: the audited allowlist first (TM-004), then BUG-66.
                config.check_provider_domain_allowed(updates["base_url"])
                core._require_parsable_base_url(
                    updates["base_url"], "Provider base_url"
                )
            record = core.update_provider(provider_id, updates)
        except config.ProviderDomainNotAllowed as exc:
            core._audit(
                "provider.domain.rejected",
                target_type="provider",
                target_id=provider_id,
                details=json.dumps(
                    {"base_url": updates.get("base_url", ""), "reason": str(exc)}
                ),
            )
            raise HTTPException(400, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if record is None:
            raise HTTPException(404, f"Provider '{provider_id}' not found")
        core.invalidate_models_cache()
        audit_details: Dict[str, Any] = {"fields": sorted(updates.keys())}
        if "is_active" in updates:
            # The switch's value, not only its name (PLU-12).
            audit_details["is_active"] = updates["is_active"]
        core._audit(
            "provider.update",
            target_type="provider",
            target_id=provider_id,
            details=json.dumps(audit_details),
        )
        if (
            provider_id == "ollama"
            and before is not None
            and not before.get("is_active", True)
            and updates.get("is_active") is True
        ):
            # The Ollama switch's own field (PLU-12): turned back on here, the
            # installed model list is synced once, as from the Ollama page.
            from plugboard import ollama

            await ollama.resync_models()
            synced = core.get_provider(provider_id) or {}
            record = {**record, "models": synced.get("models", record.get("models"))}
        if before is not None and "base_url" in updates:
            old_url = (before.get("base_url") or "").rstrip("/")
            new_url = (updates["base_url"] or "").rstrip("/")
            if old_url != new_url:
                core._audit(
                    "provider.base_url.change",
                    target_type="provider",
                    target_id=provider_id,
                    details=json.dumps({"old": old_url, "new": new_url}),
                )
        return _mask(record)

    @app.delete("/providers/{provider_id}", status_code=204)
    async def remove_provider(provider_id: str, _: None = Depends(core._require_admin)):
        if not delete_provider(provider_id):
            raise HTTPException(404, f"Provider '{provider_id}' not found")
        core.invalidate_models_cache()
        core._audit("provider.delete", target_type="provider", target_id=provider_id)
        await _webhook("provider.deleted", {"provider_id": provider_id})

    @app.get("/providers/{provider_id}/health")
    async def provider_health(provider_id: str, _: None = Depends(core._require_admin)):
        provider = core.get_provider(provider_id)
        if provider is None:
            raise HTTPException(404, f"Provider '{provider_id}' not found")
        _refuse_unreadable_secret(provider)
        try:
            require_supported_provider(provider)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        from plugboard.health_probe import probe_provider

        result = await probe_provider(provider)
        return {"provider": provider_id, **result}

    @app.post("/providers/{provider_id}/discover")
    async def discover_provider_models(
        provider_id: str,
        request: Request,
        dry_run: bool = False,
        _: None = Depends(core._require_admin),
    ):
        """Discover a provider's models and write them; removals apply at once.

        ``dry_run=true`` returns what would be added and removed without writing,
        so the dashboard can ask for confirmation first. A call from the MCP
        server is audited with its refusals and previews (PB-MCP-02).
        """
        actor = _admin_actor(request)
        try:
            return await _discover_provider(provider_id, dry_run, actor)
        except HTTPException as exc:
            if actor == "mcp":
                core._audit(
                    "provider.discover.refused",
                    actor=actor,
                    target_type="provider",
                    target_id=provider_id,
                    details=json.dumps(
                        {"status": exc.status_code, "reason": exc.detail}, default=str
                    ),
                )
            raise

    async def _discover_provider(
        provider_id: str, dry_run: bool, actor: str
    ) -> Dict[str, Any]:
        from plugboard import change_events, model_diff

        provider = core.get_provider(provider_id)
        if provider is None:
            raise HTTPException(404, f"Provider '{provider_id}' not found")
        _refuse_unreadable_secret(provider)
        try:
            require_supported_provider(provider)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        extra: Dict[str, Any] = {}
        default_model = None
        report: Optional[Dict[str, Any]] = None
        if (provider.get("auth_source", "") or "").startswith("subprocess:"):
            from plugboard.model_availability import refresh_subprocess_provider_models

            report = await refresh_subprocess_provider_models(provider)
            models = report.get("models") or []
            default_model = report.get("default_model") or None
            extra = {
                "verified": report.get("verified_count", 0),
                "dropped": report.get("dropped", []),
            }
            trusted = True  # each model was probed
        else:
            from plugboard.router import last_discovery_trusted

            models = await core.discover_models(provider)
            trusted = last_discovery_trusted()
        if not models:
            signed_out = probes_off_kind = ""
            if report is not None:
                from plugboard.cli_subprocess import probe_cli_signed_in
                from plugboard.model_availability import _probe_kinds_from_env

                kind = provider["auth_source"].split(":", 1)[1]
                account = await probe_cli_signed_in(kind)
                if account.get("signed_in") is False:
                    signed_out = account.get("error") or f"{kind} is not signed in."
                elif kind not in _probe_kinds_from_env():
                    probes_off_kind = kind
            raise HTTPException(
                502,
                _no_models_detail(
                    provider_id,
                    report,
                    signed_out=signed_out,
                    probes_off_kind=probes_off_kind,
                ),
            )
        diff = model_diff.apply(
            provider_id,
            models,
            "manual",
            default_model=default_model,
            dry_run=dry_run,
            allow_removals=trusted,
        )
        if diff.error:
            raise HTTPException(502, diff.error)
        if not dry_run:
            core.invalidate_models_cache()
            if diff.changed and not diff.first_population:
                batch = change_events.ChangeBatch()
                batch.add(
                    "models.changed",
                    {
                        "provider": provider_id,
                        "added": diff.added,
                        "removed": diff.removed,
                    },
                )
                batch.flush_in_background()
            core._audit(
                "provider.discover",
                actor=actor,
                target_type="provider",
                target_id=provider_id,
                details=f"added={len(diff.added)} removed={len(diff.removed)}",
            )
        elif actor == "mcp":
            core._audit(
                "provider.discover.preview",
                actor=actor,
                target_type="provider",
                target_id=provider_id,
                details=f"added={len(diff.added)} removed={len(diff.removed)}",
            )
        return {
            "provider": provider_id,
            "discovered": len(models),
            "models": [m["id"] for m in models],
            "added": diff.added,
            "removed": diff.removed,
            "removals_allowed": trusted,
            "dry_run": dry_run,
            # The provider had no model yet: added/removed are empty by design.
            "first_population": diff.first_population,
            **extra,
        }

    return SimpleNamespace(get_providers=get_providers)
