"""OAuth broker (admin): in-dashboard OAuth to Google, GitHub and OpenAI.

Routes ``/admin/oauth/start``, ``/oauth/callback/{source}`` (public, guarded by
the state token), ``/admin/oauth/device/poll/{state}``,
``/admin/oauth/connections`` and ``/admin/oauth/{source}``, moved from
``plugboard/app.py`` (PLU-36), behaviour unchanged. ``core`` is the
``plugboard.app`` module that registers them: the names the tests patch there
are read as ``core.<name>`` when a handler runs.
"""

from __future__ import annotations

import logging
from types import ModuleType, SimpleNamespace
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

log = logging.getLogger("plugboard.app")


class OAuthStartRequest(BaseModel):
    preset: str  # preset key from direct_providers (e.g. "google-broker")


def _slugify_for_broker(name: str) -> str:
    import re

    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    @app.post("/admin/oauth/start")
    async def oauth_start(
        body: OAuthStartRequest, _: None = Depends(core._require_admin)
    ):
        """Start an OAuth flow for a broker preset. Returns auth_url or device code."""
        from plugboard.direct_providers import get_preset, is_broker_preset
        from plugboard.oauth_manager import start_flow

        preset = get_preset(body.preset)
        if preset is None:
            raise HTTPException(404, f"Unknown preset: {body.preset}")
        if not is_broker_preset(preset):
            raise HTTPException(422, f"Preset '{body.preset}' is not a broker preset")
        source = preset.get("oauth_source", "")
        try:
            result = await start_flow(source)
        except Exception as exc:
            raise HTTPException(502, f"Failed to start OAuth flow: {exc}")
        core._audit(
            "oauth.start",
            target_type="oauth",
            target_id=source,
            details=f"preset:{body.preset}",
        )
        return {
            "flow": result.flow,
            "state": result.state,
            "auth_url": result.auth_url or None,
            "user_code": result.user_code or None,
            "verification_uri": result.verification_uri or None,
            "interval": result.interval,
            "expires_in": result.expires_in,
            "preset": body.preset,
        }

    @app.get("/oauth/callback/{source}", include_in_schema=False)
    async def oauth_callback(
        request: Request,
        source: str,
        code: str = "",
        state: str = "",
        error: str = "",
    ):
        """OAuth redirect callback (Authorization Code flow).

        Public endpoint (not admin-protected) — CSRF-protected via state token.
        Exchanges the code for tokens and returns an HTML page that closes
        the popup and notifies the parent via postMessage.

        Security: every interpolated value is html-escaped for the body and
        json-encoded for the postMessage payload. The target origin is pinned
        to the current server origin instead of "*" so a hostile parent page
        that opened the popup cannot intercept the message.
        """
        import html as _html
        import json as _json

        nonce = request.state.csp_nonce

        def _render(
            title: str, body_html: str, payload: Dict[str, Any], close_delay: int
        ):
            # Build the JSON payload via json.dumps so control chars and
            # quotes from untrusted inputs are escaped safely. We then
            # escape `<` → `\u003c` so an attacker-controlled value like
            # `"</script><script>alert(1)</script>"` cannot break out of
            # the surrounding <script> block (JSON is a subset of JS but
            # the parser is permissive about literal `</script>`).
            payload_js = (
                _json.dumps(payload)
                .replace("<", "\\u003c")
                .replace(">", "\\u003e")
                .replace("&", "\\u0026")
            )
            # Pin target origin to the server itself. Anything else lets a
            # cross-origin opener intercept tokens/account info.
            return (
                "<!DOCTYPE html>\n"
                '<html><head><meta charset="utf-8">'
                f"<title>OAuth — {_html.escape(title)}</title>\n"
                "<style>body{font-family:-apple-system,sans-serif;padding:2rem;"
                "text-align:center;background:#f9fafb;}"
                ".box{background:white;border-radius:12px;padding:2rem;"
                "max-width:420px;margin:0 auto;box-shadow:0 2px 8px rgba(0,0,0,0.08);}"
                ".ok{color:#059669;font-size:1.1rem;} .err{color:#dc2626;font-size:1rem;}"
                "code{background:#f3f4f6;padding:0.2rem 0.4rem;border-radius:4px;"
                "font-size:0.85rem;}</style></head><body>"
                f'<div class="box">{body_html}</div>\n'
                f'<script nonce="{nonce}">\n'
                "try {\n"
                f"  var payload = {payload_js};\n"
                "  if (window.opener) {\n"
                "    window.opener.postMessage(payload, window.location.origin);\n"
                "  }\n"
                "} catch(e) {}\n"
                f"setTimeout(function(){{ window.close(); }}, {int(close_delay)});\n"
                "</script></body></html>"
            )

        safe_source = _html.escape(source)

        if error:
            safe_error = _html.escape(error)
            body_html = (
                f'<div class="err">❌ OAuth error</div><p><code>{safe_error}</code></p>'
            )
            payload = {"type": "oauth-error", "source": source, "error": error}
            return Response(
                content=_render("Error", body_html, payload, close_delay=5000),
                media_type="text/html",
            )

        if not code or not state:
            body_html = '<div class="err">❌ Missing code or state</div>'
            payload = {"type": "oauth-error", "source": source, "error": "missing_code"}
            return Response(
                content=_render("Error", body_html, payload, close_delay=5000),
                media_type="text/html",
            )

        from plugboard.oauth_manager import complete_callback

        try:
            cred = await complete_callback(source, state, code)
        except Exception as exc:
            safe_exc = _html.escape(str(exc))
            body_html = f'<div class="err">❌ Token exchange failed</div><p><code>{safe_exc}</code></p>'
            payload = {
                "type": "oauth-error",
                "source": source,
                "error": "exchange_failed",
            }
            return Response(
                content=_render("Failed", body_html, payload, close_delay=8000),
                media_type="text/html",
            )

        # Auto-create the provider record if not exists
        _ensure_broker_provider(source)

        safe_account = _html.escape(cred.account)
        body_html = (
            f'<div class="ok">✓ Connected as<br><strong>{safe_account}</strong></div>'
            f'<p style="color:#6b7280;font-size:0.85rem;margin-top:1rem;">'
            f"Provider <code>{safe_source}-broker</code> created. "
            f"You can close this window.</p>"
        )
        payload = {"type": "oauth-success", "source": source, "account": cred.account}
        return Response(
            content=_render("Connected", body_html, payload, close_delay=2000),
            media_type="text/html",
        )

    @app.get("/admin/oauth/device/poll/{state}")
    async def oauth_device_poll(state: str, _: None = Depends(core._require_admin)):
        """Poll a Device Flow (GitHub). Returns status + account when done."""
        from plugboard.oauth_manager import poll_device

        try:
            result = await poll_device(state)
        except Exception as exc:
            raise HTTPException(502, f"Poll failed: {exc}")
        if result.get("status") == "success":
            source = result.get("source", "")
            if source:
                _ensure_broker_provider(source)
        return result

    @app.get("/admin/oauth/connections")
    async def oauth_connections(_: None = Depends(core._require_admin)):
        """List all OAuth connections (no secrets)."""
        from plugboard.oauth_store import list_credentials

        return {"connections": list_credentials()}

    @app.delete("/admin/oauth/{source}", status_code=204)
    async def oauth_disconnect(source: str, _: None = Depends(core._require_admin)):
        """Revoke an OAuth connection.

        The cached tokens of that source (and of ``broker:github+copilot`` for
        ``github``) are dropped at once. Related broker provider records are kept:
        their requests fail with "Auth not available" until the source is
        connected again.
        """
        from plugboard.oauth_store import delete_credential

        deleted = delete_credential(source)
        if not deleted:
            raise HTTPException(404, f"No connection for source: {source}")
        core._audit("oauth.disconnect", target_type="oauth", target_id=source)
        core.invalidate_models_cache()

    def _ensure_broker_provider(oauth_source: str) -> None:
        """Create a Provider record for a broker-based preset after successful OAuth.

        Finds all broker presets bound to this oauth_source and creates providers
        that don't already exist.
        """
        from plugboard.direct_providers import PRESETS

        matching = [
            p
            for p in PRESETS.values()
            if p.get("oauth_source") == oauth_source
            and p.get("auth_source", "").startswith("broker:")
        ]
        for preset in matching:
            existing = core.get_provider(_slugify_for_broker(preset["name"]))
            if existing is not None:
                continue
            from plugboard.direct_providers import create_from_preset

            try:
                create_from_preset(preset["key"])
            except Exception as exc:
                log.warning(
                    "Failed to auto-create provider for %s: %s", preset["key"], exc
                )
        core.invalidate_models_cache()

    return SimpleNamespace(oauth_disconnect=oauth_disconnect)
