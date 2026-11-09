"""Ticket store behind /suivi: lifecycle rules and history."""

from __future__ import annotations

import pytest

from plugboard import suivi
from plugboard.suivi import SuiviError


@pytest.fixture(autouse=True)
def _data(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    yield tmp_path


def _code(excinfo) -> str:
    return excinfo.value.code


def test_create_starts_in_a_trier_with_history():
    t = suivi.create_ticket("Fix the login page", auteur="yb")
    assert t["statut"] == "a_trier"
    assert t["priorite"] == "P2" and t["type"] == "fonction"
    assert t["source"] == "manuel" and t["source_ref"] is None
    assert t["preuves"] == []
    assert [(h["de"], h["vers"], h["auteur"]) for h in t["historique"]] == [
        (None, "a_trier", "yb")
    ]
    assert suivi.get_ticket(t["id"]) == t
    assert [x["id"] for x in suivi.list_tickets()] == [t["id"]]


def test_free_moves_between_active_columns():
    t = suivi.create_ticket("A")
    for statut in ("en_cours", "pret", "en_revue", "a_trier"):
        t = suivi.update_ticket(t["id"], statut=statut)
        assert t["statut"] == statut
    assert [h["vers"] for h in t["historique"]] == [
        "a_trier",
        "en_cours",
        "pret",
        "en_revue",
        "a_trier",
    ]


@pytest.mark.parametrize("statut", ["bloque", "abandonne"])
def test_blocked_and_dropped_need_a_reason(statut):
    t = suivi.create_ticket("A")
    for motif in (None, "", "   "):
        with pytest.raises(SuiviError) as exc:
            suivi.update_ticket(t["id"], statut=statut, motif=motif)
        assert _code(exc) == "motif_requis"
    t = suivi.update_ticket(t["id"], statut=statut, motif="waiting for admin")
    assert t["statut"] == statut and t["motif"] == "waiting for admin"
    assert t["historique"][-1]["motif"] == "waiting for admin"


def test_leaving_bloque_clears_motif():
    t = suivi.create_ticket("A")
    suivi.update_ticket(t["id"], statut="bloque", motif="root needed")
    t = suivi.update_ticket(t["id"], statut="en_cours")
    assert t["statut"] == "en_cours" and t["motif"] == ""


def test_termine_needs_a_proof():
    t = suivi.create_ticket("A")
    with pytest.raises(SuiviError) as exc:
        suivi.update_ticket(t["id"], statut="termine")
    assert _code(exc) == "preuve_requise"
    p = suivi.add_preuve(t["id"], "ci", "ci journal plugboard 0530ea6 : vert")
    assert p["type"] == "ci"
    t = suivi.update_ticket(t["id"], statut="termine")
    assert t["statut"] == "termine"
    assert [x["valeur"] for x in t["preuves"]] == [
        "ci journal plugboard 0530ea6 : vert"
    ]


def test_edit_fields_without_moving():
    t = suivi.create_ticket("A")
    t = suivi.update_ticket(
        t["id"], titre="B", description="d", priorite="P0", type="bug"
    )
    assert (t["titre"], t["description"], t["priorite"], t["type"]) == (
        "B",
        "d",
        "P0",
        "bug",
    )
    assert len(t["historique"]) == 1  # no status change, no transition row


def test_unknown_ticket():
    with pytest.raises(SuiviError) as exc:
        suivi.update_ticket(999, statut="pret")
    assert _code(exc) == "introuvable"
    with pytest.raises(SuiviError) as exc:
        suivi.add_preuve(999, "ci", "x")
    assert _code(exc) == "introuvable"
    assert suivi.get_ticket(999) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"titre": ""},
        {"titre": "   "},
        {"titre": "x" * 201},
        {"titre": "ok", "priorite": "P9"},
        {"titre": "ok", "type": "epic"},
        {"titre": "ok", "statut": "done"},
        {"titre": "ok", "source": "jira"},
        {"titre": "ok", "description": "x" * 5001},
    ],
)
def test_invalid_creation(kwargs):
    with pytest.raises(SuiviError) as exc:
        suivi.create_ticket(**kwargs)
    assert _code(exc) == "invalide"


def test_invalid_update_and_proof():
    t = suivi.create_ticket("A")
    with pytest.raises(SuiviError) as exc:
        suivi.update_ticket(t["id"], statut="done")
    assert _code(exc) == "invalide"
    for type_, valeur in (("photo", "x"), ("ci", ""), ("ci", "x" * 501)):
        with pytest.raises(SuiviError) as exc:
            suivi.add_preuve(t["id"], type_, valeur)
        assert _code(exc) == "invalide"


def test_source_ref_is_unique():
    suivi.create_ticket("A", source="todos", source_ref="TODOS.md#a")
    assert suivi.find_by_source_ref("TODOS.md#a")["titre"] == "A"
    assert suivi.find_by_source_ref("TODOS.md#b") is None
    with pytest.raises(SuiviError) as exc:
        suivi.create_ticket("A again", source="todos", source_ref="TODOS.md#a")
    assert _code(exc) == "invalide"


def test_database_lives_in_data_dir(_data):
    suivi.create_ticket("A")
    assert (_data / "suivi.db").is_file()
