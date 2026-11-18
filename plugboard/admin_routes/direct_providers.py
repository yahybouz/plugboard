"""Direct providers (admin): presets for Anthropic, OpenAI, Gemini, GitHub Models.

Routes ``/admin/direct-providers/presets``, ``/admin/direct-providers/test``,
``/admin/direct-providers/{preset_key}/check`` and
``/admin/direct-providers/{preset_key}``, moved from ``plugboard/app.py``
(PLU-36), behaviour unchanged. ``core`` is the ``plugboard.app`` module that
registers them: the names the tests patch there, the outbound-address guard
that stays there (``_require_parsable_base_url``, shared with ``/providers``,
and ``_require_public_host``) and ``_background_tasks`` are read as
``core.<name>`` when a handler runs.
"""

from __future__ import annotations

import asyncio as _asyncio
import contextvars
import logging
import os
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from plugboard import netguard
from plugboard.decisions import is_decision_model
from plugboard.legacy_connectors import is_removed_model
from plugboard.webhooks import emit_fire_and_forget as _webhook

log = logging.getLogger("plugboard.app")


class DirectProviderCreate(BaseModel):
    api_key: str = ""  # empty for OAuth presets (cli:* auth_source)
    is_active: bool = True  # "Save anyway" after a failed test creates it inactive


class DirectProviderTest(BaseModel):
    preset_key: str = ""
    base_url: str = ""  # custom OpenAI-compatible endpoint when no preset
    api_key: str = ""
    send_test_token: bool = False  # one billed token, opt-in


_TEST_DOCS = "docs/guide/providers-and-subscriptions.md#test-the-connection"


def _docs_link(path: str) -> str:
    """A ``docs/...`` path of the repository, for the dashboard.

    With ``PLUGBOARD_DOCS_BASE_URL`` (where the repository's files are
    published, such as its GitLab ``-/blob/main`` page) the path becomes an
    absolute URL under it, which the dashboard shows as a link. Only an
    http(s) URL with a host is used: anything else is logged and ignored.
    """
    from urllib.parse import urlsplit

    base = os.environ.get("PLUGBOARD_DOCS_BASE_URL", "").strip()
    if not base:
        return path
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        log.warning(
            "PLUGBOARD_DOCS_BASE_URL ignored: an http(s) URL is expected, "
            "such as https://gitlab.com/<group>/plugboard/-/blob/main"
        )
        return path
    return base.rstrip("/") + "/" + path


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    async def _test_openai_endpoint(
        base_url: str,
        api_key: str,
        send_test_token: bool,
        pinned: Optional[list] = None,
        preset: Optional[Dict[str, Any]] = None,
        *,
        public_only: bool = False,
    ) -> Dict[str, Any]:
        """GET /models (and optionally a 1-token completion) against an endpoint.

        A preset's own auth header and extra headers are used (Anthropic takes
        ``x-api-key`` plus ``anthropic-version``, not a bearer token).

        ``public_only`` (a protected preset): the egress guard's session, which
        refuses a non-public address and ignores ``HTTPS_PROXY``; no redirect is
        followed, so the key never reaches a host the check did not see.
        """
        from urllib.parse import urlsplit

        import aiohttp

        from plugboard.endpoints import openai_url

        host = urlsplit(base_url).hostname or base_url
        headers = {"Accept": "application/json"}
        preset = preset or {}
        for name, value in (preset.get("extra_headers") or {}).items():
            headers[str(name)] = str(value)
        if api_key:
            header_name = preset.get("auth_header_name") or "Authorization"
            prefix = preset.get("auth_header_prefix")
            prefix = "Bearer" if prefix is None else prefix
            headers[header_name] = f"{prefix} {api_key}".strip() if prefix else api_key
        timeout = aiohttp.ClientTimeout(total=15)
        # Fakes of aiohttp.ClientSession take no allow_redirects: the unguarded
        # path passes nothing new.
        no_redirect: Dict[str, Any] = {"allow_redirects": False} if public_only else {}
        try:
            if public_only:
                from plugboard.llm_pricing import _PinnedResolver

                session_cm = netguard.client_session(
                    True,
                    timeout=timeout,
                    connector=netguard.public_connector(
                        resolver=_PinnedResolver(host, pinned) if pinned else None
                    ),
                )
            else:
                connector = None
                if pinned:
                    from plugboard.llm_pricing import _PinnedResolver

                    connector = aiohttp.TCPConnector(
                        resolver=_PinnedResolver(host, pinned), use_dns_cache=False
                    )
                session_cm = aiohttp.ClientSession(timeout=timeout, connector=connector)
            async with session_cm as session:
                async with session.get(
                    openai_url(base_url, "models"), headers=headers, **no_redirect
                ) as resp:
                    if resp.status in (401, 403):
                        return {
                            "status": "invalid_key",
                            "message": f"{resp.status} invalid key: check that this key belongs to {host} "
                            "(some providers issue separate keys per region or project).",
                            "docs": _docs_link(_TEST_DOCS),
                        }
                    body = None
                    if resp.status == 200:
                        try:
                            body = await resp.json(content_type=None)
                        except ValueError:  # HTML page, plain text...
                            body = None
                    if (
                        resp.status != 200
                        or not isinstance(body, dict)
                        or not isinstance(body.get("data"), list)
                    ):
                        return {
                            "status": "not_openai_compatible",
                            "message": f"Not a standard chat API: {host} did not answer GET /models in the "
                            f"standard format (HTTP {resp.status}). Add it as a custom endpoint or check its base URL.",
                            "docs": _docs_link(_TEST_DOCS),
                        }
                models = [
                    m.get("id")
                    for m in body["data"]
                    if isinstance(m, dict) and m.get("id")
                ]
                result: Dict[str, Any] = {
                    "status": "ok" if models else "ok_no_models",
                    "models_count": len(models),
                    "models": models[:50],
                    "message": f"{len(models)} models found"
                    if models
                    else "Key valid, 0 models",
                }
                if send_test_token and models:
                    chat_model = next(
                        (
                            mid
                            for mid in models
                            if not is_decision_model(mid, effective_name=True)
                            and not is_removed_model(mid, effective_name=True)
                        ),
                        None,
                    )
                    if chat_model is None:
                        result["completion_skipped"] = (
                            "removed_legacy_connector"
                            if any(
                                is_removed_model(mid, effective_name=True)
                                for mid in models
                            )
                            else "decision_model"
                        )
                        return result
                    async with session.post(
                        openai_url(base_url, "chat/completions"),
                        headers={**headers, "Content-Type": "application/json"},
                        json={
                            "model": chat_model,
                            "messages": [{"role": "user", "content": "ping"}],
                            "max_tokens": 1,
                        },
                        **no_redirect,
                    ) as resp:
                        result["completion_ok"] = resp.status == 200
                        result["completion_status"] = resp.status
                return result
        except (aiohttp.ClientError, _asyncio.TimeoutError) as exc:
            if public_only and netguard.is_egress_refusal(exc):
                return {
                    "status": netguard.EGRESS_REFUSED,
                    "message": f"Refused: {host} resolves to a non-public address.",
                    "docs": _docs_link(_TEST_DOCS),
                }
            return {
                "status": "unreachable",
                "message": f"Unreachable: {host} did not answer ({type(exc).__name__}). "
                "Check the network, a proxy or the base URL.",
                "docs": _docs_link(_TEST_DOCS),
            }

    @app.get("/admin/direct-providers/presets")
    async def list_direct_presets(_: None = Depends(core._require_admin)):
        """List available direct-provider presets (no secrets)."""
        from plugboard.direct_providers import list_presets

        return {"presets": list_presets()}

    @app.post("/admin/direct-providers/test")
    async def test_direct_provider(
        body: DirectProviderTest, _: None = Depends(core._require_admin)
    ):
        """Check a key against a preset or custom endpoint without creating anything."""
        from plugboard.config import (
            ProviderDomainNotAllowed,
            check_provider_domain_allowed,
        )
        from plugboard.direct_providers import get_preset

        base_url = body.base_url.strip()
        pinned = None
        preset = None
        if body.preset_key:
            preset = get_preset(body.preset_key)
            if preset is None:
                raise HTTPException(404, f"Unknown preset: {body.preset_key}")
            base_url = preset.get("base_url", "")
            if preset.get("catalog"):
                pinned = await core._require_public_host(base_url)
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(422, "Give a preset_key or an http(s) base_url")
        core._require_parsable_base_url(base_url, "base_url")
        try:
            check_provider_domain_allowed(base_url)
        except ProviderDomainNotAllowed as exc:
            raise HTTPException(422, str(exc))
        # Catalogue and built-in HTTP presets are tested as they will be used:
        # through the egress guard. A custom endpoint is not protected.
        public_only = preset is not None and netguard.guard_is_forced(
            {"preset_key": body.preset_key}
        )
        return await core._test_openai_endpoint(
            base_url,
            body.api_key.strip(),
            body.send_test_token,
            pinned=pinned,
            preset=preset,
            public_only=public_only,
        )

    @app.get("/admin/direct-providers/{preset_key}/check")
    async def check_direct_preset(
        preset_key: str, _: None = Depends(core._require_admin)
    ):
        """Check if the CLI for an OAuth/subprocess preset is installed and logged in."""
        from plugboard.direct_providers import (
            get_preset,
            is_oauth_preset,
            is_subprocess_preset,
        )

        preset = get_preset(preset_key)
        if preset is None:
            raise HTTPException(404, f"Unknown preset: {preset_key}")
        if not is_oauth_preset(preset):
            return {
                "ok": True,
                "auth_type": "api_key",
                "hint": "This preset uses an API key, not OAuth.",
            }
        # Subprocess presets: verify the CLI binary is on PATH and runnable.
        if is_subprocess_preset(preset):
            from plugboard.cli_subprocess import (
                check_cli_installed,
                probe_cli_version,
            )

            kind = preset["auth_source"].removeprefix("subprocess:")
            installed = check_cli_installed(kind)
            if not installed["ok"]:
                return {
                    "ok": False,
                    "auth_type": "subprocess",
                    "kind": kind,
                    "error": installed.get("error"),
                }
            probe = await probe_cli_version(kind)
            return {
                "ok": probe["ok"],
                "auth_type": "subprocess",
                "kind": kind,
                "binary": installed.get("binary"),
                "version": probe.get("version", ""),
                "error": probe.get("error"),
            }
        # CLI-delegation presets (cli:*) — existing loader path
        from plugboard.cli_auth import get_loader

        source = preset["auth_source"].removeprefix("cli:")
        loader = get_loader(source)
        if loader is None:
            raise HTTPException(500, f"No loader for source: {source}")
        status = loader.available()
        status["auth_type"] = "oauth"
        status["source"] = source
        return status

    @app.post("/admin/direct-providers/{preset_key}", status_code=201)
    async def create_direct_provider(
        preset_key: str,
        body: DirectProviderCreate,
        _: None = Depends(core._require_admin),
    ):
        """Create a provider from a preset. For OAuth presets, api_key is ignored."""
        from plugboard.direct_providers import create_from_preset, get_preset

        preset = get_preset(preset_key)
        if preset is not None and preset.get("catalog"):
            await core._require_public_host(preset.get("base_url", ""))

        try:
            record = create_from_preset(
                preset_key, body.api_key, is_active=body.is_active
            )
        except ValueError as exc:
            detail = str(exc)
            if "Unknown preset" in detail:
                raise HTTPException(404, detail)
            raise HTTPException(422, detail)
        core.invalidate_models_cache()
        core._audit(
            "provider.create",
            target_type="provider",
            target_id=record["id"],
            details=f"direct:{preset_key}",
        )
        await _webhook(
            "provider.created", {"provider_id": record["id"], "name": record["name"]}
        )
        if record.get("auto_discover") and record.get("is_active", True):
            core._schedule_initial_discovery(record["id"])
        return record

    def _schedule_initial_discovery(provider_id: str) -> None:
        """Replace a new provider's catalog model list with what its key can use."""

        async def _run() -> None:
            try:
                provider = core.get_provider(provider_id)
                if provider is None:
                    return
                models = await core.discover_models(provider)
                if models:
                    core.update_provider(provider_id, {"models": models})
                    core.invalidate_models_cache()
            except Exception as exc:  # noqa: BLE001  (background: never crash the app)
                log.warning("Initial discovery for %s failed: %s", provider_id, exc)

        # A fresh context: the task outlives the admin request and must not hold
        # its principal (PB-AUTH-01).
        task = _asyncio.get_running_loop().create_task(
            _run(), context=contextvars.Context()
        )
        core._background_tasks.add(task)
        task.add_done_callback(core._background_tasks.discard)

    return SimpleNamespace(
        _test_openai_endpoint=_test_openai_endpoint,
        _schedule_initial_discovery=_schedule_initial_discovery,
    )
