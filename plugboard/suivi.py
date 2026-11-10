"""Plugboard ticket board behind ``/suivi``: SQLite store and lifecycle rules.

Database: ``data/suivi.db`` (resolved at each call, so ``PLUGBOARD_DATA`` is
honoured). Tables: ``tickets``, ``preuves`` (proofs) and ``transitions``, an
append-only history of status changes.

Rules: moves between columns are free, except that ``bloque`` and
``abandonne`` need a reason (``motif``) and ``termine`` needs at least one
proof. Leaving ``bloque``/``abandonne`` clears the reason.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from plugboard.storage import data_dir, private_sqlite_file

STATUTS = ("a_trier", "pret", "en_cours", "en_revue", "termine", "bloque", "abandonne")
PRIORITES = ("P0", "P1", "P2", "P3")
TYPES = ("bug", "fonction", "dette", "securite")
SOURCES = ("manuel", "superpowers-plan", "superpowers-spec", "todos")
TYPES_PREUVE = ("commande", "commit", "ci", "chemin", "lien")
STATUTS_AVEC_MOTIF = ("bloque", "abandonne")

MAX_TITRE = 200
MAX_DESCRIPTION = 5000
MAX_MOTIF = 500
MAX_PREUVE = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    titre       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    statut      TEXT NOT NULL,
    motif       TEXT NOT NULL DEFAULT '',
    priorite    TEXT NOT NULL,
    type        TEXT NOT NULL,
    source      TEXT NOT NULL,
    source_ref  TEXT UNIQUE,
    auteur      TEXT NOT NULL,
    cree_le     TEXT NOT NULL,
    maj_le      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preuves (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    type      TEXT NOT NULL,
    valeur    TEXT NOT NULL,
    auteur    TEXT NOT NULL,
    cree_le   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transitions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    de        TEXT,
    vers      TEXT NOT NULL,
    motif     TEXT NOT NULL DEFAULT '',
    auteur    TEXT NOT NULL,
    quand     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preuves_ticket ON preuves(ticket_id);
CREATE INDEX IF NOT EXISTS idx_transitions_ticket ON transitions(ticket_id);
"""


class SuiviError(ValueError):
    """A refused ticket operation; ``code`` is stable, the message is for people."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    db = private_sqlite_file(data_dir() / "suivi.db")
    conn = sqlite3.connect(str(db), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _check_choice(value: str, allowed: tuple, label: str) -> None:
    if value not in allowed:
        raise SuiviError(
            "invalide", f"{label} inconnu : {value!r} ({', '.join(allowed)})"
        )


def _clean_titre(titre: Any) -> str:
    titre = titre.strip() if isinstance(titre, str) else ""
    if not titre:
        raise SuiviError("invalide", "Le titre est obligatoire.")
    if len(titre) > MAX_TITRE:
        raise SuiviError("invalide", f"Titre trop long (> {MAX_TITRE} caractères).")
    return titre


def _clean_description(description: Any) -> str:
    description = description if isinstance(description, str) else ""
    if len(description) > MAX_DESCRIPTION:
        raise SuiviError(
            "invalide", f"Description trop longue (> {MAX_DESCRIPTION} caractères)."
        )
    return description


def _clean_motif(motif: Optional[str]) -> str:
    motif = motif.strip() if isinstance(motif, str) else ""
    if len(motif) > MAX_MOTIF:
        raise SuiviError("invalide", f"Motif trop long (> {MAX_MOTIF} caractères).")
    return motif


def _load(conn: sqlite3.Connection, ticket_id: int) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        return None
    ticket = dict(row)
    ticket["preuves"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, type, valeur, auteur, cree_le FROM preuves"
            " WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        )
    ]
    ticket["historique"] = [
        dict(r)
        for r in conn.execute(
            "SELECT de, vers, motif, auteur, quand FROM transitions"
            " WHERE ticket_id = ? ORDER BY id",
            (ticket_id,),
        )
    ]
    return ticket


def _require(conn: sqlite3.Connection, ticket_id: int) -> Dict[str, Any]:
    ticket = _load(conn, ticket_id)
    if ticket is None:
        raise SuiviError("introuvable", f"Ticket #{ticket_id} introuvable.")
    return ticket


def _check_move(ticket: Dict[str, Any], statut: str, motif: str) -> None:
    _check_choice(statut, STATUTS, "Statut")
    if statut in STATUTS_AVEC_MOTIF and not motif:
        raise SuiviError(
            "motif_requis", "Indiquez un motif pour bloquer ou abandonner un ticket."
        )
    if statut == "termine" and not ticket["preuves"]:
        raise SuiviError(
            "preuve_requise",
            "Ajoutez au moins une preuve (commande, commit, CI…) avant « terminé ».",
        )


def create_ticket(
    titre: str,
    *,
    description: str = "",
    priorite: str = "P2",
    type: str = "fonction",
    source: str = "manuel",
    source_ref: Optional[str] = None,
    statut: str = "a_trier",
    auteur: str = "admin",
) -> Dict[str, Any]:
    """Create a ticket and its first history row; return it."""
    titre = _clean_titre(titre)
    description = _clean_description(description)
    _check_choice(priorite, PRIORITES, "Priorité")
    _check_choice(type, TYPES, "Type")
    _check_choice(source, SOURCES, "Source")
    _check_choice(statut, STATUTS, "Statut")
    if statut == "termine" or statut in STATUTS_AVEC_MOTIF:
        raise SuiviError("invalide", f"Un nouveau ticket ne peut pas être {statut!r}.")
    now = _now()
    conn = _connect()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO tickets (titre, description, statut, priorite, type,"
                " source, source_ref, auteur, cree_le, maj_le)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    titre,
                    description,
                    statut,
                    priorite,
                    type,
                    source,
                    source_ref,
                    auteur,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            raise SuiviError(
                "invalide", f"Un ticket existe déjà pour la source {source_ref!r}."
            ) from None
        ticket_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO transitions (ticket_id, de, vers, auteur, quand)"
            " VALUES (?, NULL, ?, ?, ?)",
            (ticket_id, statut, auteur, now),
        )
        conn.commit()
        return _require(conn, ticket_id)
    finally:
        conn.close()


def get_ticket(ticket_id: int) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        return _load(conn, ticket_id)
    finally:
        conn.close()


def find_by_source_ref(ref: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM tickets WHERE source_ref = ?", (ref,)
        ).fetchone()
        return _load(conn, int(row["id"])) if row else None
    finally:
        conn.close()


def list_tickets() -> List[Dict[str, Any]]:
    conn = _connect()
    try:
        ids = [int(r["id"]) for r in conn.execute("SELECT id FROM tickets ORDER BY id")]
        return [t for t in (_load(conn, i) for i in ids) if t is not None]
    finally:
        conn.close()


# Columns update_ticket may write; the SET clause is built only from these.
_EDITABLE = ("titre", "description", "priorite", "type", "statut", "motif", "maj_le")


def update_ticket(
    ticket_id: int,
    *,
    auteur: str = "admin",
    statut: Optional[str] = None,
    motif: Optional[str] = None,
    titre: Optional[str] = None,
    description: Optional[str] = None,
    priorite: Optional[str] = None,
    type: Optional[str] = None,
) -> Dict[str, Any]:
    """Edit fields and/or move the ticket; a move appends a history row."""
    conn = _connect()
    try:
        ticket = _require(conn, ticket_id)
        fields: Dict[str, Any] = {}
        if titre is not None:
            fields["titre"] = _clean_titre(titre)
        if description is not None:
            fields["description"] = _clean_description(description)
        if priorite is not None:
            _check_choice(priorite, PRIORITES, "Priorité")
            fields["priorite"] = priorite
        if type is not None:
            _check_choice(type, TYPES, "Type")
            fields["type"] = type
        moved = statut is not None and statut != ticket["statut"]
        if statut is not None:
            clean_motif = _clean_motif(motif)
            _check_move(ticket, statut, clean_motif)
            fields["statut"] = statut
            fields["motif"] = clean_motif if statut in STATUTS_AVEC_MOTIF else ""
        if not fields:
            return ticket
        now = _now()
        fields["maj_le"] = now
        if not set(fields) <= set(_EDITABLE):  # pragma: no cover - programming error
            raise RuntimeError(
                f"unexpected columns: {sorted(set(fields) - set(_EDITABLE))}"
            )
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE tickets SET {assignments} WHERE id = ?",  # nosec B608
            (*fields.values(), ticket_id),
        )
        if moved:
            conn.execute(
                "INSERT INTO transitions (ticket_id, de, vers, motif, auteur, quand)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ticket_id, ticket["statut"], statut, fields["motif"], auteur, now),
            )
        conn.commit()
        return _require(conn, ticket_id)
    finally:
        conn.close()


def add_preuve(
    ticket_id: int, type: str, valeur: str, auteur: str = "admin"
) -> Dict[str, Any]:
    """Attach a proof (command + exit code, commit, CI link, path…) to a ticket."""
    _check_choice(type, TYPES_PREUVE, "Type de preuve")
    valeur = valeur.strip() if isinstance(valeur, str) else ""
    if not valeur:
        raise SuiviError("invalide", "La preuve est vide.")
    if len(valeur) > MAX_PREUVE:
        raise SuiviError("invalide", f"Preuve trop longue (> {MAX_PREUVE} caractères).")
    conn = _connect()
    try:
        _require(conn, ticket_id)
        now = _now()
        cur = conn.execute(
            "INSERT INTO preuves (ticket_id, type, valeur, auteur, cree_le)"
            " VALUES (?, ?, ?, ?, ?)",
            (ticket_id, type, valeur, auteur, now),
        )
        conn.execute("UPDATE tickets SET maj_le = ? WHERE id = ?", (now, ticket_id))
        conn.commit()
        return {
            "id": int(cur.lastrowid),
            "type": type,
            "valeur": valeur,
            "auteur": auteur,
            "cree_le": now,
        }
    finally:
        conn.close()
