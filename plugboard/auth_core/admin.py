"""Admin authentication of a request, a person's own codes and the MFA step-up.

The admin authentication of a request (admin session, admin key with its TOTP
header, personal key with that person's own code), the check of a code of a
person's own secret with its lockout counter and audit, and the current code
asked before the second factor is changed. Moved verbatim from
``plugboard/app.py`` (PLU-97), behaviour unchanged. ``core`` is the
``plugboard.app`` module that registers them: any name defined or re-exported
by ``app.py`` is read as ``core.<name>`` when it runs, even a function of this
module (R13).
"""

from __future__ import annotations

import logging
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, status

from plugboard import admin_people
from plugboard.auth_core.scopes import _ADMIN_KEY_PRINCIPAL, _ADMIN_SCOPE

log = logging.getLogger("plugboard.app")

_ADMIN_TOTP_HEADER = "X-Plugboard-Admin-TOTP"


def _person_record_is_sound(person: Dict[str, Any]) -> bool:
    """The record's id has the store's shape (a hand-edited one is refused)."""
    person_id = person.get("id")
    if isinstance(person_id, str) and admin_people.ID_PATTERN.fullmatch(person_id):
        return True
    log.error("auth rejected reason=person_record_malformed")
    return False


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    def _admin_request_auth(request: Request) -> str:
        """Admin authentication of a request: "ok", "denied", "mfa_required"
        (admin key without a valid TOTP header while MFA is required) or
        "locked" (too many wrong codes for the admin key, from any address:
        a code is only checked with the admin key; BUG-119)."""
        session = core._admin_session_principal(
            request.cookies.get(core._ADMIN_COOKIE_NAME, "")
        )
        if session is not None:
            core._set_principal(request, *session)
            return "ok"
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token.startswith(admin_people.KEY_PREFIX) and not core._is_admin_key(token):
            return core._person_request_auth(request, token)
        if not core._is_admin_key(token):
            return "denied"
        from plugboard import admin_mfa as _mfa

        # The principal is set only once the request is proven: a refused one
        # names nobody (its own audit entries name the admin key explicitly).
        if not _mfa.is_required():
            core._set_principal(request, _ADMIN_KEY_PRINCIPAL)
            return "ok"
        code = request.headers.get(_ADMIN_TOTP_HEADER, "").strip()
        if not code:
            return "mfa_required"
        from plugboard.ratelimit import check_auth_rate_limit, record_auth_failure

        client_ip = core._client_address(request)
        if not check_auth_rate_limit(_ADMIN_SCOPE):
            return "locked"
        if _mfa.verify(code):
            # The code is single-use: remember it was proven in this request, so
            # an MFA change made with it (step-up) does not ask for it again.
            request.state.admin_mfa_verified = True
            core._set_principal(request, _ADMIN_KEY_PRINCIPAL)
            return "ok"
        locked = record_auth_failure(_ADMIN_SCOPE)
        core._audit(
            "auth.mfa_failed",
            details=f"header IP: {client_ip}, locked={locked}",
            ip=client_ip,
            principal=_ADMIN_KEY_PRINCIPAL,
        )
        return "mfa_required"

    def _person_request_auth(request: Request, token: str) -> str:
        """Admin authentication by a personal key (``pba_``), with the same
        outcomes as ``_admin_request_auth``. A code of that person's own secret
        is always required, even where global admin MFA is not; wrong codes lock
        that person only (``key:admin:<id>``). An unknown or revoked key, or an
        unreadable people store, is "denied" and counts nothing. The admin key's MFA proof
        (``admin_mfa_verified``) is never set here. Without ``ADMIN_KEY`` a
        personal key opens nothing; a fault of the store (unreadable, not
        writable, malformed record) refuses without counting a wrong code."""
        if not core._effective_admin_key():
            return "denied"
        person = admin_people.find_by_key(token)
        if person is None or not core._person_record_is_sound(person):
            return "denied"
        code = request.headers.get(_ADMIN_TOTP_HEADER, "").strip()
        if not code:
            return "mfa_required"
        outcome = core._prove_person_code(request, person, code, via="header")
        if outcome == "locked":
            return "locked"
        if outcome == "unavailable":
            return "denied"
        if outcome != "ok":
            return "mfa_required"
        core._set_principal(
            request, core._person_principal(person), str(person.get("name", ""))
        )
        return "ok"

    def _prove_person_code(
        request: Request, person: Dict[str, Any], code: str, *, via: str
    ) -> str:
        """Check a code of that person's own secret, on the TOTP header
        (``via="header"``) or at login (``via="login"``).

        "ok" when accepted (the first accepted code confirms the key, audited);
        "locked" when that person's scope (``key:admin:<id>``) is locked, nothing
        checked; "refused" for a wrong code, counted on that scope only and
        audited, or "refused_locked" when that count locks the scope;
        "unavailable" for a fault of the store, nothing counted. Every entry
        names the person explicitly: it may be written before the request is
        proven.
        """
        from plugboard.ratelimit import check_auth_rate_limit, record_auth_failure

        person_id = person["id"]
        principal = core._person_principal(person)
        name = str(person.get("name", ""))
        scope = core._person_scope(person_id)
        client_ip = core._client_address(request)
        if not check_auth_rate_limit(scope):
            return "locked"
        outcome = admin_people.check_code(person_id, code)
        if outcome == "unavailable":
            log.error("auth rejected reason=person_store_unavailable")
            return "unavailable"
        if outcome == "refused":
            locked = record_auth_failure(scope)
            where = "header IP" if via == "header" else "IP"
            core._audit(
                "auth.person.mfa_failed",
                target_type="admin_person",
                target_id=person_id,
                details=f"{where}: {client_ip}, locked={locked}",
                ip=client_ip,
                principal=principal,
                principal_name=name,
            )
            return "refused_locked" if locked else "refused"
        if outcome == "confirmed":
            core._audit(
                "admin.person.confirmed",
                target_type="admin_person",
                target_id=person_id,
                details=f"via={via}",
                ip=client_ip,
                principal=principal,
                principal_name=name,
            )
        return "ok"

    def _require_mfa_step_up(request: Request, code: Optional[str] = None) -> None:
        """Ask for a current TOTP code before the second factor is changed.

        Once a secret is enrolled, rotating or removing it needs a code from the
        current secret, even from an open admin session (an auto-login session
        included): holding a session must not be enough to drop the factor.
        The code comes from ``code`` (request body) or the TOTP header; a code
        already verified by this request's authentication counts. Wrong codes
        lock the admin key (``key:admin``), whatever the address (BUG-119).
        """
        from plugboard import admin_mfa as _mfa

        if not _mfa.is_enrolled():
            return
        if getattr(request.state, "admin_mfa_verified", False):
            return
        code = (code or request.headers.get(_ADMIN_TOTP_HEADER, "")).strip()
        client_ip = core._client_address(request)
        if not code:
            core._audit(
                "admin.mfa.step_up_missing", details=f"IP: {client_ip}", ip=client_ip
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"A current MFA code is required (body or {_ADMIN_TOTP_HEADER})",
            )
        from plugboard.ratelimit import check_auth_rate_limit, record_auth_failure

        if not check_auth_rate_limit(_ADMIN_SCOPE):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts",
            )
        if _mfa.verify(code):
            return
        locked = record_auth_failure(_ADMIN_SCOPE)
        core._audit(
            "admin.mfa.step_up_failed",
            details=f"IP: {client_ip}, locked={locked}",
            ip=client_ip,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid MFA code"
        )

    return SimpleNamespace(
        _admin_request_auth=_admin_request_auth,
        _person_request_auth=_person_request_auth,
        _prove_person_code=_prove_person_code,
        _require_mfa_step_up=_require_mfa_step_up,
    )
