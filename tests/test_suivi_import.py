"""Importing /suivi tickets from Superpowers plans/specs and TODOS.md."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugboard import suivi
from plugboard.suivi_import import import_sources, parse_plan, parse_spec, parse_todos

PLAN_WITH_TASKS = """# Widget Implementation Plan

**Spec:** `docs/superpowers/specs/2026-01-01-widget-design.md`

### Task 1: Store

- [x] Write the failing test
- [x] Implement

### Task 2: API

- [x] Write the failing test
- [ ] Implement

### Task 3: Docs

Nothing to tick here.
"""

PLAN_WITH_BOXES = """# Small plan

- [ ] Add the button
- [x] Remove the old link
  - [ ] nested step is not a ticket
"""

SPEC = """# Widget — design

Body.
"""

TODOS = """# TODOS

## Security

### Session revocation

**What:** Revoke admin sessions.

**Priority:** P1

## Dashboard

### Model family aliases

**What:** Group models by family.

**Priority:** P3
"""


def test_parse_plan_one_item_per_task():
    items = parse_plan(PLAN_WITH_TASKS, "docs/superpowers/plans/p.md")
    assert [(i.titre, i.source_ref, i.coche) for i in items] == [
        ("Widget — Task 1: Store", "docs/superpowers/plans/p.md#task-1", True),
        ("Widget — Task 2: API", "docs/superpowers/plans/p.md#task-2", False),
        ("Widget — Task 3: Docs", "docs/superpowers/plans/p.md#task-3", False),
    ]
    assert {i.source for i in items} == {"superpowers-plan"}


def test_parse_plan_one_item_per_top_level_box():
    items = parse_plan(PLAN_WITH_BOXES, "docs/superpowers/plans/s.md")
    assert [(i.titre, i.source_ref, i.coche) for i in items] == [
        ("Add the button", "docs/superpowers/plans/s.md#add-the-button", False),
        (
            "Remove the old link",
            "docs/superpowers/plans/s.md#remove-the-old-link",
            True,
        ),
    ]


def test_parse_spec_and_todos():
    item = parse_spec(SPEC, "docs/superpowers/specs/w.md")
    assert (item.titre, item.source, item.source_ref, item.coche) == (
        "Planifier : Widget — design",
        "superpowers-spec",
        "docs/superpowers/specs/w.md",
        False,
    )
    assert parse_spec("no heading", "x.md") is None
    todos = parse_todos(TODOS)
    assert [(t.titre, t.source_ref, t.priorite, t.type) for t in todos] == [
        ("Session revocation", "TODOS.md#session-revocation", "P1", "securite"),
        ("Model family aliases", "TODOS.md#model-family-aliases", "P3", "dette"),
    ]
    assert todos[0].description == "Revoke admin sessions."


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    (repo / "docs/superpowers/plans").mkdir(parents=True)
    (repo / "docs/superpowers/specs").mkdir(parents=True)
    (repo / "docs/superpowers/plans/2026-01-01-widget.md").write_text(PLAN_WITH_TASKS)
    (repo / "docs/superpowers/specs/2026-01-01-widget-design.md").write_text(SPEC)
    (repo / "docs/superpowers/specs/2026-02-01-other-design.md").write_text(
        "# Other — design\n"
    )
    (repo / "TODOS.md").write_text(TODOS)
    atelier = repo / ".atelier/traces/x/docs/superpowers/plans"
    atelier.mkdir(parents=True)
    (atelier / "secret.md").write_text("- [ ] private trace\n")
    return repo


def _by_ref():
    return {t["source_ref"]: t for t in suivi.list_tickets()}


def test_import_creates_tickets(root):
    counts = import_sources(root, auteur="yb")
    assert counts == {"crees": 6, "mis_a_jour": 0, "inchanges": 0}
    tickets = _by_ref()
    # The widget spec has a plan: no "Planifier" ticket for it.
    assert "docs/superpowers/specs/2026-01-01-widget-design.md" not in tickets
    assert "docs/superpowers/specs/2026-02-01-other-design.md" in tickets
    # A checked task arrives in review, never done.
    assert tickets["docs/superpowers/plans/2026-01-01-widget.md#task-1"]["statut"] == (
        "en_revue"
    )
    assert tickets["docs/superpowers/plans/2026-01-01-widget.md#task-2"]["statut"] == (
        "a_trier"
    )
    assert not any("private trace" in t["titre"] for t in tickets.values())
    assert {t["auteur"] for t in tickets.values()} == {"yb"}


def test_import_is_idempotent_and_promotes_checked(root):
    import_sources(root)
    assert import_sources(root) == {"crees": 0, "mis_a_jour": 0, "inchanges": 6}
    plan = root / "docs/superpowers/plans/2026-01-01-widget.md"
    plan.write_text(PLAN_WITH_TASKS.replace("- [ ] Implement", "- [x] Implement"))
    assert import_sources(root) == {"crees": 0, "mis_a_jour": 1, "inchanges": 5}
    task2 = _by_ref()["docs/superpowers/plans/2026-01-01-widget.md#task-2"]
    assert task2["statut"] == "en_revue"
    assert task2["historique"][-1]["auteur"] == "import"
    assert len(suivi.list_tickets()) == 6


def test_import_never_moves_closed_or_blocked(root):
    import_sources(root)
    refs = _by_ref()
    t1 = refs["docs/superpowers/plans/2026-01-01-widget.md#task-1"]
    suivi.add_preuve(t1["id"], "commit", "f9f5e6b")
    suivi.update_ticket(t1["id"], statut="termine")
    t2 = refs["docs/superpowers/plans/2026-01-01-widget.md#task-2"]
    suivi.update_ticket(t2["id"], statut="bloque", motif="waiting")
    plan = root / "docs/superpowers/plans/2026-01-01-widget.md"
    plan.write_text(PLAN_WITH_TASKS.replace("- [ ] Implement", "- [x] Implement"))
    import_sources(root)
    refs = _by_ref()
    assert refs[t1["source_ref"]]["statut"] == "termine"
    assert refs[t2["source_ref"]]["statut"] == "bloque"


def test_import_with_nothing_to_read(tmp_path, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path / "data"))
    assert import_sources(tmp_path / "empty") == {
        "crees": 0,
        "mis_a_jour": 0,
        "inchanges": 0,
    }


def test_import_does_not_touch_sources(root):
    before = {p: p.read_bytes() for p in root.rglob("*.md")}
    import_sources(root)
    assert {p: p.read_bytes() for p in root.rglob("*.md")} == before
