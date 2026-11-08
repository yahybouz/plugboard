"""Admin API of the /suivi ticket board.

``app.py`` calls ``build_router(_require_admin)``: the guard is a dependency
of the router itself, so every route needs the admin session (or key, plus
TOTP when MFA is on) and is seen as admin-only by route introspection.
Writes are recorded in the audit trail (``suivi.*`` actions). Handlers are
synchronous so SQLite runs in FastAPI's thread pool, off the event loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import plugboard
from plugboard import audit, suivi, suivi_import
from plugboard.suivi import SuiviError

# Where the versioned plans, specs and TODOS.md live (the repository root).
PROJECT_ROOT = Path(plugboard.__file__).resolve().parent.parent

Statut = Literal[suivi.STATUTS]
Priorite = Literal[suivi.PRIORITES]
TypeTicket = Literal[suivi.TYPES]
TypePreuve = Literal[suivi.TYPES_PREUVE]


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    titre: str = Field(min_length=1, max_length=suivi.MAX_TITRE)
    description: str = Field("", max_length=suivi.MAX_DESCRIPTION)
    priorite: Priorite = "P2"
    type: TypeTicket = "fonction"


class TicketPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statut: Optional[Statut] = None
    motif: Optional[str] = Field(None, max_length=suivi.MAX_MOTIF)
    titre: Optional[str] = Field(None, min_length=1, max_length=suivi.MAX_TITRE)
    description: Optional[str] = Field(None, max_length=suivi.MAX_DESCRIPTION)
    priorite: Optional[Priorite] = None
    type: Optional[TypeTicket] = None


class PreuveCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: TypePreuve
    valeur: str = Field(min_length=1, max_length=suivi.MAX_PREUVE)


def _refused(exc: SuiviError) -> JSONResponse:
    status = 404 if exc.code == "introuvable" else 409
    return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=status)


def _audit(request: Request, action: str, ticket_id: object, details: str) -> None:
    audit.log_action(
        action,
        target_type="suivi_ticket",
        target_id=str(ticket_id),
        details=details[:500],
        ip=request.client.host if request.client else "",
    )


def build_router(require_admin: Callable[[Request], None]) -> APIRouter:
    """The /admin/suivi routes, every one behind ``require_admin``."""
    router = APIRouter(
        prefix="/admin/suivi", tags=["suivi"], dependencies=[Depends(require_admin)]
    )

    @router.get("/tickets")
    def list_tickets():
        return {
            "tickets": suivi.list_tickets(),
            "statuts": list(suivi.STATUTS),
            "priorites": list(suivi.PRIORITES),
            "types": list(suivi.TYPES),
            "types_preuve": list(suivi.TYPES_PREUVE),
        }

    @router.post("/tickets", status_code=201)
    def create_ticket(body: TicketCreate, request: Request):
        try:
            ticket = suivi.create_ticket(
                body.titre,
                description=body.description,
                priorite=body.priorite,
                type=body.type,
            )
        except SuiviError as exc:
            return _refused(exc)
        _audit(request, "suivi.ticket.create", ticket["id"], ticket["titre"])
        return ticket

    @router.patch("/tickets/{ticket_id}")
    def update_ticket(ticket_id: int, body: TicketPatch, request: Request):
        changes = body.model_dump(exclude_none=True)
        try:
            ticket = suivi.update_ticket(ticket_id, **changes)
        except SuiviError as exc:
            return _refused(exc)
        _audit(
            request,
            "suivi.ticket.update",
            ticket_id,
            ", ".join(f"{k}={v}" for k, v in changes.items() if k != "description"),
        )
        return ticket

    @router.post("/tickets/{ticket_id}/preuves", status_code=201)
    def add_preuve(ticket_id: int, body: PreuveCreate, request: Request):
        try:
            preuve = suivi.add_preuve(ticket_id, body.type, body.valeur)
        except SuiviError as exc:
            return _refused(exc)
        _audit(request, "suivi.ticket.preuve", ticket_id, f"{body.type}: {body.valeur}")
        return preuve

    @router.post("/import")
    def import_tickets(request: Request):
        counts = suivi_import.import_sources(PROJECT_ROOT)
        _audit(
            request,
            "suivi.import",
            "",
            " ".join(f"{k}={v}" for k, v in counts.items()),
        )
        return counts

    return router
