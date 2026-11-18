"""Admin sign-in, sign-out and second factor (admin).

Routes ``/login``, ``/auth/admin/login``, ``/auth/admin/logout``,
``/auth/admin/logout-all``, ``/admin/mfa/status``, ``/admin/mfa/begin``,
``/admin/mfa/setup``, ``/admin/mfa/verify`` and ``/admin/mfa`` (DELETE), moved
verbatim from ``plugboard/app.py`` (PLU-36). ``core`` is the ``plugboard.app``
module that registers them: the auto-login helpers (``_auto_login_refusal``,
``_local_login_proof``, ``_should_audit_refusal``...), the session and
personal-key helpers (``_create_admin_session_token``, ``_prove_person_code``,
``_person_scope``...), ``_require_admin``, ``_audit``, ``_client_address`` and
``_is_admin_key`` are read as ``core.<name>`` when a handler runs.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
import os
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from plugboard import admin_people
from plugboard import admin_sessions

log = logging.getLogger("plugboard.app")


_LOGIN_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Plugboard — Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,sans-serif;background:#f8fafc;color:#0f172a;
display:flex;align-items:center;justify-content:center;min-height:100vh;min-height:100dvh;padding:1rem}
.box{background:#fff;padding:clamp(1.25rem,6vw,2.5rem);border-radius:18px;width:400px;max-width:100%;margin:auto;flex-shrink:0;
text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.07),0 8px 28px rgba(0,0,0,.08);
border:1px solid rgba(0,0,0,.08)}
.logo{width:52px;height:52px;border-radius:14px;display:inline-grid;place-items:center;
background:linear-gradient(135deg,#1e40af,#2563eb,#3b82f6);color:#fff;font-weight:800;
font-size:1.15rem;margin-bottom:1rem;box-shadow:0 4px 14px rgba(37,99,235,.3)}
h2{font-size:1.35rem;font-weight:700;margin-bottom:.25rem;letter-spacing:-.02em}
.sub{color:#94a3b8;font-size:.85rem;margin-bottom:1.5rem}
input{width:100%;padding:.55rem .85rem;margin:0 0 .5rem;border:1px solid rgba(0,0,0,.12);
border-radius:8px;font-size:.875rem;font-family:inherit;outline:none;
transition:border-color .18s,box-shadow .18s}
input:focus{border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
button{width:100%;padding:.6rem;background:#2563eb;color:#fff;border:none;
border-radius:8px;cursor:pointer;font-size:.9rem;font-weight:600;font-family:inherit;
margin-top:.25rem;transition:background .18s;box-shadow:0 1px 3px rgba(37,99,235,.2)}
button:hover{background:#1d4ed8}
.err{color:#dc2626;font-size:.82rem;min-height:1.2em;margin-top:.25rem;overflow-wrap:anywhere}
@media(max-width:600px),(any-pointer:coarse){input{font-size:1rem}input,button{min-height:44px}}
</style></head><body>
<div class="box">
<div class="logo">E</div>
<h2>Plugboard</h2>
<p class="sub">Enter your admin key or your personal key</p>
<form id="f">
<input type="password" id="k" placeholder="Admin or personal key" autofocus>
<input type="text" id="m" placeholder="MFA code (6 digits)" inputmode="numeric" pattern="[0-9]*" maxlength="6" autocomplete="one-time-code" style="display:none">
<div class="err" id="e"></div>
<button type="submit" id="b">Sign in</button>
</form>
<script nonce="__CSP_NONCE__">
var f=document.getElementById('f'),k=document.getElementById('k'),
    m=document.getElementById('m'),e=document.getElementById('e'),
    b=document.getElementById('b');
var mfaNeeded=false;
f.onsubmit=async ev=>{
  ev.preventDefault();
  e.textContent='';
  var body={key:k.value};
  if(mfaNeeded) body.totp_code=m.value.trim();
  const r=await fetch('/auth/admin/login',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)
  });
  if(r.ok){
    var raw=new URLSearchParams(location.search).get('next')||'';
    var safe=(raw && raw.charAt(0)==='/' && raw.charAt(1)!=='/') ? raw : '/dashboard';
    location.href=safe;
    return;
  }
  // Parse JSON error for the MFA-required marker; fall back to a
  // generic message if the body is empty or malformed.
  var payload={};
  try{ payload=await r.json(); }catch(_){}
  if(payload && payload.mfa_required){
    mfaNeeded=true;
    m.style.display='block';
    m.focus();
    b.textContent='Verify MFA';
    e.textContent='Enter the 6-digit code from your authenticator.';
    return;
  }
  e.textContent=(payload && payload.detail) || 'Invalid key or code';
};
</script></div></body></html>"""


class AdminLogin(BaseModel):
    key: str
    # Optional six-digit TOTP code. Required when admin_mfa.is_required().
    totp_code: Optional[str] = None


class AdminMfaSetupRequest(BaseModel):
    """Confirm an enrolment by proving the admin controls the secret."""

    secret: str
    code: str
    # Code from the secret being replaced; required once one is enrolled.
    current_code: Optional[str] = None


class AdminMfaVerify(BaseModel):
    code: str


def _safe_next(raw: str) -> str:
    """A local path to go back to after sign-in, else ``/dashboard``.

    Same rule as the login page script: one leading slash only, so
    ``//host`` and ``/\\host`` (read as other origins) are refused.
    """
    if (
        raw.startswith("/")
        and not raw.startswith(("//", "/\\"))
        and len(raw) <= 512
        and raw.isprintable()
    ):
        return raw
    return "/dashboard"


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    """Declare this block's routes on ``app``, in their original order."""

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        """Public login page. Auto-login only for a local install opened locally
        by a browser holding the operator's secret (login token or remembered)."""
        refusal = core._auto_login_refusal(request)
        proof = ""
        if not refusal:
            proof, refusal = core._local_login_proof(request)
        peer = request.client.host if request.client else ""
        host = core._host_header_name(request)
        if refusal:
            log.info("auth.auto_login refused reason=%s peer=%s", refusal, peer)
            if (
                refusal in core._SUSPICIOUS_REFUSALS
                and host in core._LOOPBACK_HOSTS
                and core._should_audit_refusal(peer, refusal)
            ):
                # Unauthenticated path: keep the SQLite write off the event loop.
                await _asyncio.to_thread(
                    core._audit,
                    "auth.auto_login_refused",
                    details=f"reason={refusal} host={host}",
                    ip=peer,
                )
        else:
            session_token = core._create_admin_session_token()
            if session_token:
                core._audit(
                    "auth.auto_login",
                    details=f"localhost auto-login host={host} via={proof}",
                    ip=peer,
                    principal=core._ADMIN_KEY_PRINCIPAL,
                )
                resp = RedirectResponse(
                    url=_safe_next(request.query_params.get("next", "")),
                    status_code=302,
                )
                # Auto-login only serves plain-http localhost (refused otherwise),
                # so the cookie cannot carry the Secure flag.
                resp.set_cookie(  # NOSONAR
                    core._ADMIN_COOKIE_NAME,
                    session_token,
                    max_age=core._ADMIN_SESSION_MAX_AGE,
                    httponly=True,
                    samesite="strict",
                    secure=False,
                    path="/",
                )
                if proof == "token":
                    from plugboard import local_login

                    # Remember this browser so the next visits need no token.
                    resp.set_cookie(  # NOSONAR
                        local_login.DEVICE_COOKIE,
                        local_login.remember_device(),
                        max_age=local_login.DEVICE_MAX_AGE,
                        httponly=True,
                        samesite="strict",
                        secure=False,
                        path="/login",
                    )
                return resp
        nonce = request.state.csp_nonce
        html = _LOGIN_HTML_TEMPLATE.replace("__CSP_NONCE__", nonce)
        return Response(content=html, media_type="text/html")

    # Every path returns its own Response (204 on success, 401/429 otherwise).
    @app.post("/auth/admin/login", include_in_schema=False)
    async def admin_login(body: AdminLogin, request: Request):
        from plugboard.ratelimit import (
            check_auth_rate_limit,
            get_lockout_remaining,
            record_auth_failure,
        )

        client_ip = core._client_address(request)
        address = core._address_scope(request)
        token = body.key.strip()
        is_admin_key = core._is_admin_key(token)
        # A live personal key (PB-AUTH-01); without ADMIN_KEY it opens nothing
        # and counts as a wrong key.
        personal = (
            not is_admin_key
            and token.startswith(admin_people.KEY_PREFIX)
            and bool(core._effective_admin_key())
        )
        person = admin_people.find_by_key(token) if personal else None
        known = is_admin_key or person is not None

        # Brute-force protection (BUG-119): wrong keys lock their address, but
        # the address lockout never refuses a valid key (the admin key or a live
        # personal key); behind a proxy anyone could otherwise lock them out.
        # Checked before the store refusal below, so a locked address gets no
        # free guess while the people store is unreadable (PB-AUTH-01).
        if not known and not check_auth_rate_limit(address):
            remaining = get_lockout_remaining(address)
            core._audit(
                "auth.login_blocked",
                details=f"IP locked out: {client_ip}",
                ip=client_ip,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many failed attempts. Try again later."},
                headers={"Retry-After": str(remaining)},
            )

        if personal and person is None and not admin_people.is_readable():
            # A fault of the store, not a wrong key: refuse and count nothing,
            # as on the TOTP header.
            log.error("auth rejected reason=person_store_unavailable")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
            )

        if not known:
            locked = record_auth_failure(address)
            core._audit(
                "auth.login_failed",
                details=f"IP: {client_ip}, locked={locked}",
                ip=client_ip,
            )
            log.warning(
                "Failed admin login attempt from %s (locked=%s)", client_ip, locked
            )
            if locked:
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many failed attempts. Try again later."},
                    headers={"Retry-After": "900"},
                )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
            )

        if person is not None:
            return _person_login(request, body, person)

        # TM-001: enforce second factor when MFA is required.
        # is_required() is only true when the operator set the env flag AND
        # a secret is already enrolled, so a fresh deploy can't lock itself out.
        from plugboard import admin_mfa as _mfa

        mfa_checked = _mfa.is_required()
        if mfa_checked:
            code = (body.totp_code or "").strip()
            if not code:
                # Distinct 401 shape so the login UI knows to prompt for the
                # second factor rather than telling the user their key is bad.
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "MFA code required", "mfa_required": True},
                )
            # Wrong codes need the admin key: they lock the admin key, from any
            # address (BUG-119).
            if not check_auth_rate_limit(core._ADMIN_SCOPE):
                remaining = get_lockout_remaining(core._ADMIN_SCOPE)
                core._audit(
                    "auth.login_blocked",
                    details=f"admin key locked out (MFA), IP: {client_ip}",
                    ip=client_ip,
                    principal=core._ADMIN_KEY_PRINCIPAL,
                )
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many failed attempts. Try again later."},
                    headers={"Retry-After": str(remaining)},
                )
            if not _mfa.verify(code):
                locked = record_auth_failure(core._ADMIN_SCOPE)
                core._audit(
                    "auth.mfa_failed",
                    details=f"IP: {client_ip}, locked={locked}",
                    ip=client_ip,
                    principal=core._ADMIN_KEY_PRINCIPAL,
                )
                log.warning("Failed admin MFA from %s (locked=%s)", client_ip, locked)
                if locked:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": "Too many failed attempts. Try again later."
                        },
                        headers={"Retry-After": "900"},
                    )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid MFA code",
                )
            core._audit(
                "auth.mfa_success",
                details=f"IP: {client_ip}",
                ip=client_ip,
                principal=core._ADMIN_KEY_PRINCIPAL,
            )

        session_token = core._create_admin_session_token(mfa=mfa_checked)
        if not session_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
            )

        core._set_principal(request, core._ADMIN_KEY_PRINCIPAL)
        core._audit("auth.login_success", details=f"IP: {client_ip}", ip=client_ip)
        return _session_response(request, session_token)

    def _person_login(
        request: Request, body: AdminLogin, person: Dict[str, Any]
    ) -> Response:
        """Open a session with a live personal key and its own code (PB-AUTH-01).

        The code is always required, and checked like on the TOTP header
        (``_prove_person_code``): wrong codes lock that person only. Every entry
        names the person.
        """
        from plugboard.ratelimit import get_lockout_remaining

        client_ip = core._client_address(request)
        generation = person.get("generation")
        sound = core._person_record_is_sound(person)
        if sound and not (isinstance(generation, str) and generation):
            # A session without a generation could never be checked again.
            log.error("auth rejected reason=person_record_malformed")
            sound = False
        if not sound:  # a hand-edited record opens nothing; nothing is counted
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
            )
        person_id = person["id"]
        principal = core._person_principal(person)
        name = str(person.get("name", ""))
        code = (body.totp_code or "").strip()
        if not code:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "MFA code required", "mfa_required": True},
            )
        outcome = core._prove_person_code(request, person, code, via="login")
        if outcome == "locked":
            remaining = get_lockout_remaining(core._person_scope(person_id))
            core._audit(
                "auth.login_blocked",
                target_type="admin_person",
                target_id=person_id,
                details=f"personal key locked out (MFA), IP: {client_ip}",
                ip=client_ip,
                principal=principal,
                principal_name=name,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many failed attempts. Try again later."},
                headers={"Retry-After": str(remaining)},
            )
        if outcome == "refused_locked":
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many failed attempts. Try again later."},
                headers={"Retry-After": "900"},
            )
        if outcome == "refused":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid MFA code"
            )
        if outcome != "ok":  # the store failed: refuse, count nothing
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
            )
        session_token = core._create_admin_session_token(person=person)
        if not session_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin key required"
            )
        core._set_principal(request, principal, name)
        core._audit(
            "auth.login_success",
            details=f"IP: {client_ip}",
            ip=client_ip,
            principal=principal,
            principal_name=name,
        )
        return _session_response(request, session_token)

    def _session_response(request: Request, session_token: str) -> Response:
        """204 that sets the admin session cookie (and drops the legacy one)."""
        resp = Response(status_code=204)
        resp.set_cookie(
            core._ADMIN_COOKIE_NAME,
            session_token,
            max_age=core._ADMIN_SESSION_MAX_AGE,
            httponly=True,
            samesite="strict",
            secure=core._should_use_secure_cookie(request),
            path="/",
        )
        resp.delete_cookie("llm_admin_key", path="/")
        return resp

    def _session_record_author(nonce: str) -> Tuple[str, str]:
        """``(principal, name)`` of a session no longer valid, from its record.

        A logout still names who opened the session once that person is revoked
        (or the people store is unreadable: no name then), or once an MFA change
        refused an ``ADMIN_KEY`` session. ``("", "")`` without a record or with a
        hand-edited ``person_id``.
        """
        record = admin_sessions.lookup(nonce) if nonce else None
        if record is None:
            return ("", "")
        if "person_id" not in record:
            return (core._ADMIN_KEY_PRINCIPAL, "")
        person_id = record.get("person_id")
        if not isinstance(person_id, str) or not admin_people.ID_PATTERN.fullmatch(
            person_id
        ):
            return ("", "")
        person = admin_people.get(person_id)
        return ("person:" + person_id, str(person.get("name", "")) if person else "")

    @app.post("/auth/admin/logout", include_in_schema=False, status_code=204)
    async def admin_logout(request: Request):
        """Revoke this browser's session server-side, then clear its cookie."""
        session = request.cookies.get(core._ADMIN_COOKIE_NAME, "")
        nonce = core._admin_session_nonce(session)
        # The author is read before the revocation: the entry names who left.
        found = core._admin_session_principal(session) if nonce else None
        principal, name = found or _session_record_author(nonce)
        if nonce and admin_sessions.revoke(nonce):
            core._audit(
                "auth.logout",
                details="admin session revoked",
                principal=principal,
                principal_name=name,
            )
        resp = Response(status_code=204)
        resp.delete_cookie(core._ADMIN_COOKIE_NAME, path="/")
        resp.delete_cookie("llm_admin_key", path="/")
        return resp

    @app.post("/auth/admin/logout-all", include_in_schema=False, status_code=204)
    async def admin_logout_all(
        request: Request, _: None = Depends(core._require_admin)
    ):
        """Revoke every admin session (this one included), then clear its cookie.

        Browsers remembered for local auto-login are forgotten first: otherwise
        their next visit to /login would open a new session at once.
        """
        from plugboard import local_login

        try:
            local_login.forget_devices()
            admin_sessions.revoke_all()
        except OSError as exc:
            log.error("Admin sessions not revoked: %s", exc)
            raise HTTPException(
                status_code=503,
                detail="Sessions could not be revoked: the session store is not writable.",
            ) from exc
        core._audit("auth.logout_all", details="every admin session revoked")
        resp = Response(status_code=204)
        resp.delete_cookie(core._ADMIN_COOKIE_NAME, path="/")
        resp.delete_cookie("llm_admin_key", path="/")
        return resp

    # ── Admin MFA (TM-001) ──────────────────────────────────────────────────────

    def _refuse_person_principal(request: Request) -> None:
        """The second factor of ADMIN_KEY is managed with ADMIN_KEY only.

        A person (personal key or person-bound session) is refused before any
        code is read, so a person's codes never count on ``key:admin`` nor
        reach the audit of ADMIN_KEY's factor (PB-AUTH-01). The refusal is
        audited once, under the person's own principal and name.
        """
        principal = getattr(request.state, "admin_principal", core._ADMIN_KEY_PRINCIPAL)
        if principal != core._ADMIN_KEY_PRINCIPAL:
            core._audit(
                "admin.mfa.person_refused",
                target_type="admin",
                target_id="admin",
                details=f"{request.method} {request.url.path}",
                ip=core._client_address(request),
                principal=principal,
                principal_name=getattr(request.state, "admin_principal_name", ""),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The second factor of ADMIN_KEY is managed with ADMIN_KEY",
            )

    @app.get("/admin/mfa/status", include_in_schema=False)
    async def admin_mfa_status(_: None = Depends(core._require_admin)):
        """Expose MFA configuration so the dashboard can render the right UI."""
        from plugboard import admin_mfa as _mfa

        return {
            "enrolled": _mfa.is_enrolled(),
            "enforced": _mfa.is_required(),
            "flag_set": os.environ.get("PLUGBOARD_ADMIN_MFA_REQUIRED", "").strip()
            == "1",
        }

    @app.post("/admin/mfa/begin", include_in_schema=False)
    async def admin_mfa_begin(request: Request, _: None = Depends(core._require_admin)):
        """Step 1 of enrolment: server generates a fresh TOTP secret and the
        otpauth:// provisioning URI. Nothing is persisted yet — the caller must
        confirm with /admin/mfa/setup and a valid code from their authenticator.
        """
        from plugboard import admin_mfa as _mfa

        _refuse_person_principal(request)

        secret = _mfa.generate_secret()
        return {
            "secret": secret,
            "otpauth_uri": _mfa.provisioning_uri(secret, account="admin"),
        }

    @app.post("/admin/mfa/setup", include_in_schema=False, status_code=204)
    async def admin_mfa_setup(
        body: AdminMfaSetupRequest,
        request: Request,
        _: None = Depends(core._require_admin),
    ):
        """Step 2 of enrolment: the admin submits the secret from /begin plus a
        code read from their authenticator app. On success the secret is
        Fernet-encrypted and written to disk. Subsequent logins will demand
        TOTP codes once PLUGBOARD_ADMIN_MFA_REQUIRED=1. Replacing an enrolled
        secret also needs a current code from it (``current_code``)."""
        from plugboard import admin_mfa as _mfa

        _refuse_person_principal(request)
        core._require_mfa_step_up(request, body.current_code)
        # Validate the code matches the *submitted* secret (not any previously
        # stored one — we might be rotating). We don't call _mfa.verify() here
        # because that reads from disk. The code is then recorded as used.
        step = _mfa.setup_code_step(body.secret, (body.code or "").strip())
        if step is None:
            raise HTTPException(400, "TOTP code does not match the secret")
        _mfa.enroll(body.secret, used_step=step)
        core._audit(
            "admin.mfa.enrolled",
            target_type="admin",
            target_id="admin",
            ip=request.client.host if request.client else "",
        )

    @app.post("/admin/mfa/verify", include_in_schema=False, status_code=204)
    async def admin_mfa_verify(
        body: AdminMfaVerify, request: Request, _: None = Depends(core._require_admin)
    ):
        """Validate a code against the currently enrolled secret (debug helper)."""
        from plugboard import admin_mfa as _mfa

        _refuse_person_principal(request)

        if not _mfa.verify((body.code or "").strip()):
            raise HTTPException(400, "Invalid TOTP code")

    @app.delete("/admin/mfa", include_in_schema=False, status_code=204)
    async def admin_mfa_disable(
        request: Request, _: None = Depends(core._require_admin)
    ):
        """Remove the stored TOTP secret. Useful for rotation or to disable MFA.

        Needs a current code in the X-Plugboard-Admin-TOTP header while a secret
        is enrolled (lost authenticator: scripts/admin_mfa_setup.py --rotate on
        the host)."""
        from plugboard import admin_mfa as _mfa

        _refuse_person_principal(request)
        core._require_mfa_step_up(request)
        _mfa.disable()
        core._audit(
            "admin.mfa.disabled",
            target_type="admin",
            target_id="admin",
            ip=request.client.host if request.client else "",
        )

    return SimpleNamespace(_LOGIN_HTML_TEMPLATE=_LOGIN_HTML_TEMPLATE)
