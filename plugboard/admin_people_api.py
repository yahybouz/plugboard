"""Admin API of the personal dashboard keys (PB-AUTH-01).

``app.py`` calls ``build_router(_require_admin, admin_key_set)``: the guard is
a dependency of the router itself, so every route needs an admin (the admin
key or a person, with TOTP as on every admin route) and stays out of the
client API contract. Any admin may create, list and revoke keys, a person
revoking themselves included.

``POST`` answers the raw key, the TOTP secret and its ``otpauth://`` URI
once, never cached; nothing else ever shows them. A store that cannot be
read or written answers 503 with a generic message (the cause goes to the
log by its type only), never a 500, and leaves no partial record. A creation
the audit log cannot record is revoked and answers 503: the raw key is never
returned without its audit entry. Handlers
are synchronous so the file store runs in FastAPI's thread pool.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from plugboard import admin_people, audit
from plugboard.admin_people import PeopleStoreUnreadable, PersonError
from plugboard.storage import StoreUnreadableError

log = logging.getLogger(__name__)

_ADMIN_KEY_PRINCIPAL = "admin-key"
_NEEDS_ADMIN_KEY = "Personal keys need ADMIN_KEY"
_STORE_DOWN = "The personal key store cannot be read or written: see the server log"
_NOT_FOUND = "Personal key not found"
_AUDIT_DOWN = "The audit log cannot be written: see the server log"
# Every way the store can fail: unreadable on read, refused or lost on write.
_STORE_FAULTS = (PeopleStoreUnreadable, StoreUnreadableError, OSError)


class PersonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=256)


def _principal(request: Request) -> tuple[str, str]:
    """The caller: ``admin-key`` when the auth is off (no proof was given)."""
    principal = getattr(request.state, "admin_principal", "") or _ADMIN_KEY_PRINCIPAL
    name = getattr(request.state, "admin_principal_name", "") or ""
    return principal, name


def _store_down(action: str, exc: BaseException) -> HTTPException:
    log.error("Personal key store failed on %s: %s", action, type(exc).__name__)
    return HTTPException(status_code=503, detail=_STORE_DOWN)


def _audit(request: Request, action: str, view: dict) -> None:
    principal, name = _principal(request)
    audit.log_action(
        action,
        target_type="admin_person",
        target_id=str(view.get("id", "")),
        details=str(view.get("name", ""))[:500],
        ip=request.client.host if request.client else "",
        principal=principal,
        principal_name=name,
    )


def _undo_creation(person_id: str, request: Request, cause: BaseException) -> None:
    """Revoke a key whose creation could not be audited; best-effort."""
    log.error(
        "Personal key %s not audited, revoking it: %s", person_id, type(cause).__name__
    )
    try:
        admin_people.revoke(person_id, revoked_by=_principal(request)[0])
    except Exception as exc:
        # The raw key is never handed out, so a pending record nobody holds
        # the key of is all that can remain.
        log.error(
            "Personal key %s not revoked after the audit failed: %s",
            person_id,
            type(exc).__name__,
        )


def build_router(
    require_admin: Callable[[Request], None], admin_key_set: Callable[[], bool]
) -> APIRouter:
    """The /admin/people routes, every one behind ``require_admin``."""
    router = APIRouter(
        prefix="/admin/people",
        tags=["admin-people"],
        dependencies=[Depends(require_admin)],
    )

    def _need_admin_key() -> None:
        # With auth fully off the guard lets everyone in: a personal key
        # would then mean nothing, so none is handed out.
        if not admin_key_set():
            raise HTTPException(status_code=503, detail=_NEEDS_ADMIN_KEY)

    @router.post("", status_code=201)
    def create_person(body: PersonCreate, request: Request) -> JSONResponse:
        _need_admin_key()
        try:
            made = admin_people.create(body.name, created_by=_principal(request)[0])
        except PersonError as exc:
            status = 409 if exc.code == "name_taken" else 422
            raise HTTPException(status_code=status, detail=str(exc)) from None
        except _STORE_FAULTS as exc:
            raise _store_down("create", exc) from None
        try:
            _audit(request, "admin.person.created", made["person"])
        except Exception as exc:
            # No raw key without its audit entry: undo the creation, then refuse.
            _undo_creation(made["person"]["id"], request, exc)
            raise HTTPException(status_code=503, detail=_AUDIT_DOWN) from None
        return JSONResponse(
            made, status_code=201, headers={"Cache-Control": "no-store"}
        )

    @router.get("")
    def list_people() -> dict:
        try:
            return {"people": admin_people.list_people()}
        except _STORE_FAULTS as exc:
            raise _store_down("list", exc) from None

    @router.delete("/{person_id}")
    def revoke_person(person_id: str, request: Request) -> dict:
        try:
            outcome = admin_people.revoke(person_id, revoked_by=_principal(request)[0])
        except _STORE_FAULTS as exc:
            raise _store_down("revoke", exc) from None
        if outcome is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        view, changed = outcome
        if changed:
            _audit(request, "admin.person.revoked", view)
        return view

    return router
