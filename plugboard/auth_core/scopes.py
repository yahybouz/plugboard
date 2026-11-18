"""Audit principal, client address, limit and lockout counters of the auth core.

The principal an admin credential leaves in the audit, the client address, and
whose counter a request takes from the global limit and the key lockout
(BUG-119). Moved verbatim from ``plugboard/app.py`` (PLU-97), behaviour
unchanged. ``core`` is the ``plugboard.app`` module that registers them: any
name defined or re-exported by ``app.py`` is read as ``core.<name>`` when it
runs, even a function of this module (R13).
"""

from __future__ import annotations

from types import ModuleType, SimpleNamespace
from typing import Any, Dict

from fastapi import FastAPI, Request

from plugboard import admin_people
from plugboard import audit as _audit_mod
from plugboard.apikeys import KEY_PREFIXES

_ADMIN_SCOPE = "key:admin"
_ADMIN_KEY_PRINCIPAL = "admin-key"


def _person_principal(person: Dict[str, Any]) -> str:
    """The audit principal of a personal key: ``person:<id>``."""
    return "person:" + person["id"]


def _person_scope(person_id: str) -> str:
    """Lockout counter of one person's codes: ``key:admin:<id>``."""
    return f"{_ADMIN_SCOPE}:{person_id}"


def _set_principal(request: Request, principal: str, name: str = "") -> None:
    """Name the admin credential of this request, for the audit and the routes."""
    request.state.admin_principal = principal
    request.state.admin_principal_name = name
    _audit_mod.set_principal(principal, name)


def register(app: FastAPI, core: ModuleType) -> SimpleNamespace:
    def _client_address(request: Request) -> str:
        """The client address: uvicorn's ``request.client.host``.

        uvicorn replaces the peer with the right-most ``X-Forwarded-For`` entry
        that is not a trusted proxy, and only when the peer is listed in
        ``FORWARDED_ALLOW_IPS`` (``uvicorn/middleware/proxy_headers.py``).
        Plugboard reads no header for it.
        """
        return request.client.host if request.client else "unknown"

    def _address_scope(request: Request) -> str:
        """Counter of the client address, for the limit and the key lockout."""
        return "ip:" + core._client_address(request)

    def _key_scope(request: Request) -> str:
        """The valid key a request carries, as ``key:<key_id>``, else "".

        ``key:admin`` for the admin key or an admin session, ``key:<key_id>`` for
        a client key the auth dependency accepts, "" otherwise (no key, a wrong,
        forged or revoked key). Never raises and logs no refusal (BUG-119). A
        session opened with a personal key is ``key:admin:<id>``, that person's
        own scope; a bare personal key is not counted here (it shows no checks
        without its code), see ``_person_bearer_scope``.

        Same checks as ``_resolve_client``, in its order, with no file read in
        the common case: the admin key is a constant-time comparison; a client
        key is looked up in the key cache (one ``stat``, the file is read only
        when it changed); a session cookie is checked against its signature
        first, so only a correctly signed one is looked up in the session store.
        The admin key needs no TOTP here: callers only count or show generic
        states with it, never act as the admin.
        """
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if core._is_admin_key(token):
            return _ADMIN_SCOPE
        if token.startswith(KEY_PREFIXES):
            # A client key wins over the session cookie, as in _resolve_client.
            rec = core.get_key_record(token, log_refused=False)
            key_id = str(rec.get("key_id") or "") if rec else ""
            return f"key:{key_id}" if key_id else ""
        session = request.cookies.get(core._ADMIN_COOKIE_NAME, "")
        if not core._admin_session_nonce(session):
            return ""
        found = core._admin_session_principal(session)
        if found is None:
            return ""
        principal = found[0]
        if principal == _ADMIN_KEY_PRINCIPAL:
            return _ADMIN_SCOPE
        # The id needs no check here: the principal names a person that
        # ``admin_people.get`` found, and ``get`` refuses any id outside
        # ``ID_PATTERN`` (that is where the id guard lives).
        return core._person_scope(principal.removeprefix("person:"))

    def _person_bearer_scope(request: Request) -> str:
        """``key:admin:<id>`` for a live personal key in the bearer, else "".

        Its code is not checked: the scope only counts the request in that
        person's bucket of the global limit. A revoked or unknown ``pba_`` key,
        an unreadable store or a hand-edited record is "" (the address counts
        it). Never raises and logs no refusal (BUG-119).
        """
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if (
            not token.startswith(admin_people.KEY_PREFIX)
            or not core._effective_admin_key()
            or core._is_admin_key(token)
        ):
            return ""
        person = admin_people.find_by_key(token)
        person_id = person.get("id") if person else None
        if not isinstance(person_id, str) or not admin_people.ID_PATTERN.fullmatch(
            person_id
        ):
            return ""
        return core._person_scope(person_id)

    def _rate_limit_scope(request: Request) -> str:
        """Whose counter a request takes from the global limit (BUG-119).

        The valid key it carries (``_key_scope``), the client address otherwise:
        a wrong, forged or revoked key never leaves the address counter. Behind a
        proxy every client has the proxy's address unless it is trusted, so the
        address counter only holds the calls without a valid key. A live personal
        key, or a session it opened, counts in that person's own bucket
        (``key:admin:<id>``).
        """
        return (
            core._key_scope(request)
            or core._person_bearer_scope(request)
            or core._address_scope(request)
        )

    return SimpleNamespace(
        _client_address=_client_address,
        _address_scope=_address_scope,
        _key_scope=_key_scope,
        _person_bearer_scope=_person_bearer_scope,
        _rate_limit_scope=_rate_limit_scope,
    )
