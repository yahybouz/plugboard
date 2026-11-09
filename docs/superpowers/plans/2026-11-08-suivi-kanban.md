# Page `/suivi` (Kanban Plugboard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/suivi` page, behind the admin session, showing Plugboard tickets as a Kanban, with tickets stored in SQLite and imported from Superpowers plans/specs and `TODOS.md`.

**Architecture:** Domain module `plugboard/suivi.py` (SQLite store + lifecycle rules) and `plugboard/suivi_import.py` (read-only parsers + idempotent import). `plugboard/suivi_api.py` is an `APIRouter(prefix="/admin/suivi")` that `app.py` includes with `dependencies=[Depends(_require_admin)]`. Static page `static/suivi.{html,js,css}` served by `GET /suivi`.

**Tech Stack:** Python 3.12, FastAPI, sqlite3, vanilla JS/CSS (strict CSP: no inline script), pytest, Playwright E2E (`PLUGBOARD_RUN_E2E=1`).

**Spec:** `docs/superpowers/specs/2026-11-06-suivi-kanban-design.md` (approach 1, approved by YB on 2026-11-08).

## Global Constraints

- Statuses, in column order: `a_trier`, `pret`, `en_cours`, `en_revue`, `termine`, `bloque`, `abandonne`.
- `bloque` and `abandonne` need a non-empty `motif`; `termine` needs at least one proof.
- Priority `P0`–`P3` (default `P2`); type `bug`, `fonction`, `dette`, `securite` (default `fonction`).
- Source `manuel`, `superpowers-plan`, `superpowers-spec`, `todos`; `source_ref` unique (dedup key).
- Admin session only (`_require_admin`); every write goes to `audit.log_action` (`suivi.*` actions).
- Import reads only `docs/superpowers/plans/*.md`, `docs/superpowers/specs/*.md`, `TODOS.md` under the project root; never `.atelier/`; never writes to them; never starts an agent or job.
- A checked source item moves a ticket to `en_revue` at most, never `termine`.
- Mobile ≥ 360 px: no horizontal page scroll, targets ≥ 44 px, status changed with a `<select>` (no drag and drop).
- Data in `data/suivi.db` (path from `plugboard.storage.data_dir()` at call time).

## Review Focus

- Re-running the import after a plan box is checked: one ticket, moved to `en_revue`, no duplicate → `test_import_is_idempotent_and_promotes_checked`.
- A ticket already `termine`/`abandonne`/`bloque` whose source box is checked stays where it is → `test_import_never_moves_closed_or_blocked`.
- Moving to `termine` without proof is refused with a readable message (409), then allowed after a proof → `test_patch_termine_needs_proof`.
- Leaving `bloque` clears its `motif` → `test_leaving_bloque_clears_motif`.
- Opening `/suivi` signed out lands back on `/suivi` after auto-login (honour a safe `next`) → `test_auto_login_honours_safe_next`.

---

### Task 1: Ticket store and lifecycle rules

**Files:** Create `plugboard/suivi.py`; Test `tests/test_suivi_store.py`.

**Interfaces — Produces:**
- `STATUTS: tuple[str, ...]`, `PRIORITES`, `TYPES`, `SOURCES`, `TYPES_PREUVE = ("commande", "commit", "ci", "chemin", "lien")`.
- `class SuiviError(ValueError)` with `.code` in `{"introuvable", "motif_requis", "preuve_requise", "invalide"}`.
- `create_ticket(titre: str, *, description="", priorite="P2", type="fonction", source="manuel", source_ref: str | None = None, statut="a_trier", auteur="admin") -> dict`
- `get_ticket(ticket_id: int) -> dict | None`; `list_tickets() -> list[dict]`; `find_by_source_ref(ref: str) -> dict | None`
- `update_ticket(ticket_id: int, *, auteur="admin", statut=None, motif=None, titre=None, description=None, priorite=None, type=None) -> dict`
- `add_preuve(ticket_id: int, type: str, valeur: str, auteur="admin") -> dict`
- Ticket dict keys: `id, titre, description, statut, motif, priorite, type, source, source_ref, auteur, cree_le, maj_le, preuves[], historique[]`; each history row `{de, vers, motif, auteur, quand}` (append-only table).

- [x] Tests: create → `a_trier` + one history row (`de=None`); `bloque`/`abandonne` without motif → `SuiviError("motif_requis")`; `termine` without proof → `preuve_requise`, then OK after `add_preuve`; `test_leaving_bloque_clears_motif`; unknown id → `introuvable`; invalid statut/priorite/type/titre (empty or > 200 chars) → `invalide`; duplicate `source_ref` → `invalide`.
- [x] Run, see them fail; implement (one connection per call, WAL, `busy_timeout`, tables `tickets`, `preuves`, `transitions`); run, pass; commit `feat(suivi): ticket store and lifecycle rules`.

### Task 2: Import from plans, specs and TODOS.md

**Files:** Create `plugboard/suivi_import.py`; Test `tests/test_suivi_import.py`.

**Interfaces — Consumes:** Task 1. **Produces:** `parse_plan(text: str, ref: str) -> list[Item]`, `parse_spec(text: str, ref: str) -> Item | None`, `parse_todos(text: str) -> list[Item]`, `import_sources(root: Path, auteur="admin") -> dict` returning `{"crees": int, "mis_a_jour": int, "inchanges": int}`; `Item` dataclass `(titre, description, source, source_ref, coche, priorite="P2", type="fonction")`.

- Plan: with `### Task N: Name` headings, one item per task, `source_ref = "<relpath>#task-N"`, `coche` when the task has boxes and all are `[x]`; without task headings, one item per `- [ ]`/`- [x]` line, `source_ref = "<relpath>#<slug>"`.
- Spec: one item `"Planifier : <first # heading>"`, `source_ref = relpath`, skipped when a plan's text contains that relpath.
- TODOS.md: one item per `### Title`, `source_ref = "TODOS.md#<slug>"`, priority from `**Priority:** Pn`, type `securite` under `## Security`, else `dette`.

- [x] Tests: parsers on inline samples; `test_import_is_idempotent_and_promotes_checked`; `test_import_never_moves_closed_or_blocked`; a spec referenced by a plan creates no ticket; `.atelier/` files are ignored; missing directories → zero counts.
- [x] Run, fail; implement; run, pass; commit `feat(suivi): import tickets from Superpowers plans and TODOS.md`.

### Task 3: Admin API and `/suivi` route

**Files:** Create `plugboard/suivi_api.py`; Modify `plugboard/app.py` (include router, `GET /suivi`, safe `next` on auto-login); Test `tests/test_suivi_api.py`.

**Interfaces — Consumes:** Tasks 1–2. **Produces:** `GET /admin/suivi/tickets` → `{"tickets": [...], "statuts": [...]}`; `POST /admin/suivi/tickets` (201); `PATCH /admin/suivi/tickets/{id}`; `POST /admin/suivi/tickets/{id}/preuves` (201); `POST /admin/suivi/import`. `SuiviError` → 404 (`introuvable`) or 409 (others) with `{"detail": message, "code": code}`; body validation → 422. Project root for import: `pathlib.Path(plugboard.__file__).resolve().parent.parent`.

- [x] Tests: each endpoint 401 without auth paired with the same call succeeding with the admin key; `test_patch_termine_needs_proof`; writes leave a `suivi.*` audit row; `GET /suivi` signed out → redirect to `/login?next=/suivi`, signed in → HTML containing `suivi.js`; `test_auto_login_honours_safe_next` (`next=//evil` → `/dashboard`).
- [x] Run, fail; implement; run, pass; commit `feat(suivi): admin API and /suivi page route`.

### Task 4: Kanban page

**Files:** Create `static/suivi.html`, `static/suivi.js`, `static/suivi.css`; Modify `static/index.html` (sidebar link "Suivi" → `/suivi`), `static/app.js` (`initTabs` ignores links without `data-tab`); Modify `tests/e2e/conftest.py` (sign in through `/login?token=` read from the server's `data/.login_token`, since PB-SEC-02); Test `tests/e2e/test_suivi.py`.

- Columns in `STATUTS` order; card = title, `#id`, priority, type, source; `<select>` of statuses (asks for a motif on `bloque`/`abandonne`); proof form (type + value); create form; "Importer" button showing the counts; API errors shown in an `aria-live` region. Under 768 px columns stack as `<details>` (open when not empty).
- [x] E2E: desktop and 390×844 — create a ticket, move it with the select, `termine` refused then accepted after a proof, `document.documentElement.scrollWidth <= innerWidth`, no JS error.
- [x] Run (`PLUGBOARD_RUN_E2E=1`), pass; commit `feat(suivi): Kanban page`.

### Task 5: Docs and changelog

- [x] `docs/guide/monitoring.md` section "Ticket board (`/suivi`)"; CHANGELOG `### Added`; spec status → approved. Full `make test` + `ruff`; commit `docs(suivi): guide and changelog`.
