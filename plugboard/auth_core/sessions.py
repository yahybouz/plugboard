"""Admin key, signed admin sessions, cross-site refusal and the secure cookie.

The admin key check, the signed and server-registered dashboard sessions, the
refusal of a state-changing request that the session cookie authenticates but
another origin sent (PB-SEC-06), and the cookie ``Secure`` policy. Moved
verbatim from ``plugboard/app.py`` (PLU-97), behaviour unchanged. ``core`` is
the ``plugboard.app`` module that registers them: any name defined or
re-exported by ``app.py`` is read as ``core.<name>`` when it runs, even a
function of this module (R13).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request, status

from plugboard import admin_people
from plugboard import admin_sessions
from plugboard.auth_core.scopes import _ADMIN_KEY_PRINCIPAL

log = logging.getLogger("plugboard.app")

# PB-SEC-06: SameSite=Strict keeps the session cookie off cross-site requests
# only. A page served on another port of the same host is same-site, so the
# browser still attaches the cookie to the forms and fetches that page sends.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CROSS_SITE_REFUSED = "Cross-site request refused"


def _origin_is_this_host(origin: str, request: Request) -> bool:
    """``Origin`` names the host and port the request was sent to.

    The scheme is not compared: behind a TLS proxy the app sees plain HTTP.
    """
    from urllib.parse import urlsplit

    try:
        sent_from = urlsplit(origin)
        sent_to = urlsplit("//" + request.headers.get("host", ""))
        default_port = {"http": 80, "https": 443}.get(sent_from.scheme)
        return bool(sent_from.hostname) and (
            sent_from.hostname == sent_to.hostname
            and (sent_from.port or default_port) == (sent_to.port or default_port)
        )
    except ValueError:
        return False


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    def _effective_admin_key() -> str:
        return core._ADMIN_KEY

    def _is_admin_key(token: str) -> bool:
        effective_admin = core._effective_admin_key()
        return bool(
            token and effective_admin and hmac.compare_digest(token, effective_admin)
        )

    def _create_admin_session_token(
        mfa: bool = False, person: Optional[Dict[str, Any]] = None
    ) -> str:
        """Sign a new session and register it server-side (revocable).

        ``mfa`` records that a TOTP code was checked to open it. A session opened
        with a personal key (``person``) always had its code checked, and keeps
        the person's id and ``generation``: it dies with the key (PB-AUTH-01).
        """
        effective_admin = core._effective_admin_key()
        if not effective_admin:
            return ""
        issued_at = int(time.time())
        nonce = secrets.token_urlsafe(18)
        payload = f"{issued_at}.{nonce}"
        signature = hmac.new(
            effective_admin.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        from plugboard import admin_mfa as _mfa

        if person is None:
            admin_sessions.register(
                nonce,
                issued_at,
                mfa=mfa,
                max_age=core._ADMIN_SESSION_MAX_AGE,
                mfa_generation=_mfa.generation(),
            )
        else:
            admin_sessions.register(
                nonce,
                issued_at,
                mfa=True,
                max_age=core._ADMIN_SESSION_MAX_AGE,
                mfa_generation=_mfa.generation(),
                person_id=person["id"],
                person_generation=person["generation"],
            )
        return f"{payload}.{signature}"

    def _admin_session_nonce(token: str) -> str:
        """The nonce of a correctly signed, unexpired session token, else ""."""
        effective_admin = core._effective_admin_key()
        if not token or not effective_admin:
            return ""
        try:
            issued_at_raw, nonce, signature = token.split(".", 2)
            issued_at = int(issued_at_raw)
        except (ValueError, TypeError):
            return ""
        if not nonce or not signature:
            return ""

        payload = f"{issued_at_raw}.{nonce}"
        expected_signature = hmac.new(
            effective_admin.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            return ""

        now = int(time.time())
        if issued_at > now + 60 or now - issued_at > core._ADMIN_SESSION_MAX_AGE:
            return ""
        return nonce

    def _admin_session_principal(token: str) -> Optional[Tuple[str, str]]:
        """The ``(principal, name)`` of a valid admin session, else ``None``.

        Valid: signed, unexpired, still registered, opened under the current MFA
        generation, and opened with TOTP when MFA is required (a session from
        before enforcement does not count). ``("admin-key", "")`` for a session
        of ``ADMIN_KEY``. A session opened with a personal key also needs its
        person to be in the store, not revoked, and of the generation the
        session was opened under (PB-AUTH-01): an unreadable store refuses it.
        """
        nonce = core._admin_session_nonce(token)
        if not nonce:
            return None
        record = admin_sessions.lookup(nonce)
        if record is None:
            return None
        from plugboard import admin_mfa as _mfa

        if record.get("mfa_generation") != _mfa.generation():
            return None
        if not (record.get("mfa") or not _mfa.is_required()):
            return None
        if "person_id" not in record:
            return (_ADMIN_KEY_PRINCIPAL, "")
        person = admin_people.get(record["person_id"])
        if person is None or person.get("revoked_at"):
            return None
        generation = person.get("generation")
        if not isinstance(generation, str) or not generation:
            return None
        if generation != record.get("person_generation"):
            return None
        return (core._person_principal(person), str(person.get("name", "")))

    def _is_admin_session(token: str) -> bool:
        """A valid admin session (see ``_admin_session_principal``)."""
        return core._admin_session_principal(token) is not None

    def _is_cross_origin_browser_request(request: Request) -> bool:
        """The browser says another origin sent this request.

        ``Sec-Fetch-Site`` decides when present (anything but ``same-origin``);
        otherwise ``Origin`` when present. A page cannot set either header; a
        request without both does not come from a browser page.
        """
        site = request.headers.get("sec-fetch-site")
        if site is not None:
            return site.strip().lower() != "same-origin"
        origin = request.headers.get("origin")
        if origin is not None:
            return not core._origin_is_this_host(origin, request)
        return False

    def _refuse_cross_site_session(request: Request) -> None:
        """403 for a state-changing request that the dashboard session cookie
        authenticates but another origin sent (CSRF).

        The admin key in ``Authorization`` carries its own authority: such a
        request is not refused, nor any request the cookie does not authenticate.
        """
        if request.method not in _STATE_CHANGING_METHODS:
            return
        if not core._is_cross_origin_browser_request(request):
            return
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if core._is_admin_key(token):
            return
        if not core._is_admin_session(request.cookies.get(core._ADMIN_COOKIE_NAME, "")):
            return
        log.warning(
            "auth rejected reason=cross_site_session method=%s path=%s",
            request.method,
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_CROSS_SITE_REFUSED
        )

    def _should_use_secure_cookie(request: Request) -> bool:
        """Resolve cookie secure policy from env; default 'auto' secures non-local hosts."""
        if core._COOKIE_SECURE_MODE in {"true", "1", "yes", "on"}:
            return True
        if core._COOKIE_SECURE_MODE in {"false", "0", "no", "off"}:
            return False
        host = (request.url.hostname or "").lower()
        return host not in {"localhost", "127.0.0.1", "::1", "test"}

    return SimpleNamespace(
        _effective_admin_key=_effective_admin_key,
        _is_admin_key=_is_admin_key,
        _create_admin_session_token=_create_admin_session_token,
        _admin_session_nonce=_admin_session_nonce,
        _admin_session_principal=_admin_session_principal,
        _is_admin_session=_is_admin_session,
        _is_cross_origin_browser_request=_is_cross_origin_browser_request,
        _refuse_cross_site_session=_refuse_cross_site_session,
        _should_use_secure_cookie=_should_use_secure_cookie,
    )
