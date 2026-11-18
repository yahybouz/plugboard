"""Ollama (admin): settings, switch and models.

Routes ``/admin/ollama/settings``, ``/admin/ollama/switch``,
``/admin/ollama/cloud-catalog``, ``/admin/ollama/status``,
``/admin/ollama/models``, ``/admin/ollama/models/pull`` and
``/admin/ollama/models/{name:path}`` (DELETE, then GET), moved verbatim from
``plugboard/app.py`` (PLU-36). ``core`` is the ``plugboard.app`` module that
registers them: ``_require_admin``, ``_audit`` and ``get_provider`` are read
as ``core.<name>`` when a handler runs.
"""

from __future__ import annotations

import json
import logging
from types import ModuleType, SimpleNamespace

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("plugboard.app")


class OllamaPullRequest(BaseModel):
    name: str = Field(max_length=256)  # e.g. "llama3:8b", "mistral"


class OllamaSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    local_downloads_enabled: bool


class OllamaSwitchUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    @app.get("/admin/ollama/settings")
    async def ollama_get_settings(_: None = Depends(core._require_admin)):
        """Local downloads switch (and its source), disk guard margin, free disk."""
        from plugboard import ollama_admin

        return ollama_admin.settings_payload()

    @app.put("/admin/ollama/settings")
    async def ollama_put_settings(
        body: OllamaSettingsUpdate, _: None = Depends(core._require_admin)
    ):
        """Turn local model downloads on or off (persisted, wins over the env)."""
        from plugboard import ollama_admin

        ollama_admin.set_local_downloads(body.local_downloads_enabled)
        core._audit(
            "ollama.settings",
            target_type="setting",
            target_id="local_downloads_enabled",
            details=f"value={body.local_downloads_enabled}",
        )
        return ollama_admin.settings_payload()

    @app.get("/admin/ollama/switch")
    async def ollama_get_switch(_: None = Depends(core._require_admin)):
        """Whether Ollama serves the API, the MCP server and the Playground (PLU-12)."""
        from plugboard import ollama

        return ollama.switch_state()

    @app.put("/admin/ollama/switch")
    async def ollama_put_switch(
        body: OllamaSwitchUpdate, _: None = Depends(core._require_admin)
    ):
        """Turn Ollama on or off for every surface, at once and without a restart.

        Off, its models leave the catalogue and every request for one gets 503
        provider_disabled without any call; requests already running go to
        their end. Persisted in providers.json (``is_active``), audited.
        """
        from plugboard import ollama

        try:
            state = ollama.set_enabled(body.enabled)
        except LookupError:
            raise HTTPException(
                404,
                "Ollama provider is not registered (see OLLAMA_URL and OLLAMA_ENABLED)",
            ) from None
        before = state.pop("before")
        # Audited as soon as it is written, before the model sync (up to
        # OLLAMA_TIMEOUT): concurrent switches are logged in the order they land.
        core._audit(
            "ollama.switch",
            target_type="provider",
            target_id=state["provider_id"],
            details=json.dumps(
                {
                    "from": before,
                    "to": body.enabled,
                    "in_flight": state["in_flight"],
                    "waiting": state["waiting"],
                }
            ),
        )
        if body.enabled and not before:
            await ollama.resync_models()
            state = ollama.switch_state()
        return state

    @app.get("/admin/ollama/cloud-catalog")
    async def ollama_cloud_catalog(_: None = Depends(core._require_admin)):
        """Public Ollama Cloud catalogue as cloud ids, flagged when installed.

        Read-only: no inference probe. Some models need a paid Ollama plan.
        """
        from plugboard import ollama_admin
        from plugboard.ollama import _to_cloud_id, fetch_cloud_catalog
        from plugboard.ollama import list_models as _ollama_list

        names = await fetch_cloud_catalog()
        try:
            local = await _ollama_list()
            local_available = True
        except Exception as exc:  # noqa: BLE001 (catalogue still useful offline)
            log.warning("ollama cloud catalog: local list unavailable: %s", exc)
            local, local_available = [], False
        installed = {
            str(m.get("name") or m.get("model") or "")
            for m in local
            if isinstance(m, dict)
        }
        seen = set()
        models = []
        for base in names:
            # Untrusted upstream: keep only names the pull route would accept
            # as cloud ids (official library, valid characters).
            if not isinstance(base, str):
                continue
            try:
                cloud_id = ollama_admin.validate_model_name(_to_cloud_id(base))
            except ValueError:
                continue
            if not ollama_admin.is_cloud_id(cloud_id) or cloud_id in seen:
                continue
            seen.add(cloud_id)
            models.append(
                {"name": base, "id": cloud_id, "installed": cloud_id in installed}
            )
        return {
            "models": models,
            "count": len(models),
            "local_list_available": local_available,
            "note": "Some cloud models need a paid Ollama plan; the free plan "
            "answers with an error when they are used.",
        }

    @app.get("/admin/ollama/status")
    async def ollama_status(_: None = Depends(core._require_admin)):
        """Check Ollama availability and provider registration."""
        from plugboard.ollama import check_available, get_ollama_url

        available = await check_available()
        provider = core.get_provider("ollama")
        return {
            "available": available,
            "url": get_ollama_url(),
            "provider_id": provider["id"] if provider else None,
            "provider_type": provider.get("provider_type", "cloud")
            if provider
            else None,
            "is_active": provider.get("is_active", False) if provider else False,
        }

    @app.get("/admin/ollama/models")
    async def ollama_list_models(_: None = Depends(core._require_admin)):
        """List installed Ollama models."""
        from plugboard.ollama import list_models as _ollama_list

        try:
            models = await _ollama_list()
            return {"models": models, "count": len(models)}
        except Exception as exc:
            raise HTTPException(502, f"Ollama error: {exc}")

    @app.post("/admin/ollama/models/pull")
    async def ollama_pull_model(
        body: OllamaPullRequest, _: None = Depends(core._require_admin)
    ):
        """Pull (download) an Ollama model. Returns streaming NDJSON progress.

        Refused before any streaming when local downloads are off and the name is
        not an Ollama Cloud id (403), or when the registry size does not fit on
        disk with the margin (507). An unknown size streams under a disk guard.
        """
        import json as _json
        from plugboard import ollama_admin
        from plugboard.ollama import delete_model, pull_model

        try:
            name = ollama_admin.validate_model_name(body.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        try:
            guard = await ollama_admin.precheck_pull(name)
        except ollama_admin.PullRefused as exc:
            core._audit(
                "ollama.pull_refused",
                target_type="model",
                target_id=name,
                details=f"status={exc.status}",
            )
            raise HTTPException(exc.status, exc.detail)

        async def _stream():
            try:
                if guard["cloud"]:
                    progress_iter = ollama_admin.guarded_pull(
                        name,
                        free_at_start=None,
                        margin=0,
                        pull=pull_model,
                        delete=delete_model,
                        max_total=ollama_admin.CLOUD_MAX_BYTES,
                    )
                else:
                    progress_iter = ollama_admin.guarded_pull(
                        name,
                        free_at_start=guard["free"],
                        margin=guard["margin"],
                        pull=pull_model,
                        delete=delete_model,
                    )
                async for progress in progress_iter:
                    yield _json.dumps(progress) + "\n"
                # Sync models after pull
                provider = core.get_provider("ollama")
                if provider:
                    from plugboard.ollama import _sync_models

                    await _sync_models(provider)
                yield _json.dumps({"status": "complete", "model": name}) + "\n"
            except ollama_admin.PullAborted:
                return
            except Exception as exc:
                yield _json.dumps({"status": "error", "error": str(exc)}) + "\n"

        core._audit("ollama.pull", target_type="model", target_id=name)
        return StreamingResponse(_stream(), media_type="application/x-ndjson")

    @app.delete("/admin/ollama/models/{name:path}", status_code=204)
    async def ollama_delete_model(name: str, _: None = Depends(core._require_admin)):
        """Delete an Ollama model."""
        from plugboard.ollama import delete_model

        try:
            deleted = await delete_model(name)
        except Exception as exc:
            raise HTTPException(502, f"Ollama error: {exc}")
        if not deleted:
            raise HTTPException(404, f"Model '{name}' not found in Ollama")
        # Sync provider models list
        provider = core.get_provider("ollama")
        if provider:
            from plugboard.ollama import _sync_models

            await _sync_models(provider)
        core._audit("ollama.delete", target_type="model", target_id=name)

    @app.get("/admin/ollama/models/{name:path}")
    async def ollama_model_info(name: str, _: None = Depends(core._require_admin)):
        """Get detailed info for an Ollama model."""
        from plugboard.ollama import show_model

        try:
            info = await show_model(name)
            return info
        except RuntimeError as exc:
            if "not found" in str(exc).lower():
                raise HTTPException(404, str(exc))
            raise HTTPException(502, str(exc))

    return SimpleNamespace()
