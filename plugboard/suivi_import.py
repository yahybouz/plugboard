"""Import /suivi tickets from Superpowers plans and specs and from ``TODOS.md``.

Read-only: the sources are never written. Only these files under the project
root are read: ``docs/superpowers/plans/*.md``, ``docs/superpowers/specs/*.md``
and ``TODOS.md`` (``.atelier/`` traces are private and never imported).

Idempotent: ``source_ref`` identifies a source item. A checked item moves its
ticket to ``en_revue`` when it is still ``a_trier``, ``pret`` or ``en_cours``;
it never closes a ticket, which needs a proof. Importing starts no agent or job.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from plugboard import suivi

log = logging.getLogger(__name__)

PLANS_DIR = "docs/superpowers/plans"
SPECS_DIR = "docs/superpowers/specs"
TODOS_FILE = "TODOS.md"
_PROMOTABLE = ("a_trier", "pret", "en_cours")

_TASK = re.compile(r"^### Task (\d+):\s*(.+?)\s*$")
_BOX = re.compile(r"^\s*- \[([ xX])\]\s")
_TOP_BOX = re.compile(r"^- \[([ xX])\]\s+(.+?)\s*$")
_H1 = re.compile(r"^# (.+?)\s*$")
_H2 = re.compile(r"^## (.+?)\s*$")
_H3 = re.compile(r"^### (.+?)\s*$")
_WHAT = re.compile(r"^\*\*What:\*\*\s*(.+?)\s*$")
_PRIORITY = re.compile(r"^\*\*Priority:\*\*\s*(P[0-3])\b")


@dataclass
class Item:
    titre: str
    description: str
    source: str
    source_ref: str
    coche: bool
    priorite: str = "P2"
    type: str = "fonction"


def slug(text: str) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def _title(text: str, limit: int = suivi.MAX_TITRE) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _plan_name(lines: List[str]) -> str:
    for line in lines:
        match = _H1.match(line)
        if match:
            return re.sub(r"\s+Implementation Plan$", "", match.group(1)).strip()
    return ""


def parse_plan(text: str, ref: str) -> List[Item]:
    """One item per ``### Task N:`` section, else one per top-level checkbox."""
    lines = text.splitlines()
    name = _plan_name(lines)
    tasks = [(i, _TASK.match(line)) for i, line in enumerate(lines)]
    tasks = [(i, m) for i, m in tasks if m]
    items: List[Item] = []
    if tasks:
        for pos, (start, match) in enumerate(tasks):
            end = tasks[pos + 1][0] if pos + 1 < len(tasks) else len(lines)
            for j in range(start + 1, end):
                if _H2.match(lines[j]) or _H3.match(lines[j]):
                    end = j
                    break
            boxes = [_BOX.match(line) for line in lines[start + 1 : end]]
            states = [b.group(1) != " " for b in boxes if b]
            label = f"Task {match.group(1)}: {match.group(2)}"
            items.append(
                Item(
                    titre=_title(f"{name} — {label}" if name else label),
                    description=f"{ref} — {label}",
                    source="superpowers-plan",
                    source_ref=f"{ref}#task-{match.group(1)}",
                    coche=bool(states) and all(states),
                )
            )
        return items
    for line in lines:
        match = _TOP_BOX.match(line)
        if match:
            items.append(
                Item(
                    titre=_title(match.group(2)),
                    description=f"{ref}" + (f" — {name}" if name else ""),
                    source="superpowers-plan",
                    source_ref=f"{ref}#{slug(match.group(2))}",
                    coche=match.group(1) != " ",
                )
            )
    return items


def parse_spec(text: str, ref: str) -> Optional[Item]:
    """One "to plan" item per spec, titled after its first ``#`` heading."""
    for line in text.splitlines():
        match = _H1.match(line)
        if match:
            return Item(
                titre=_title(f"Planifier : {match.group(1)}"),
                description=f"Spec sans plan : {ref}",
                source="superpowers-spec",
                source_ref=ref,
                coche=False,
            )
    return None


def parse_todos(text: str) -> List[Item]:
    """One item per ``### Title`` entry; priority and section give P and type."""
    items: List[Item] = []
    section = ""
    current: Optional[Item] = None
    for line in text.splitlines():
        h2, h3 = _H2.match(line), _H3.match(line)
        if h2:
            section, current = h2.group(1), None
            continue
        if h3:
            current = Item(
                titre=_title(h3.group(1)),
                description="",
                source="todos",
                source_ref=f"{TODOS_FILE}#{slug(h3.group(1))}",
                coche=False,
                type="securite" if section.strip().lower() == "security" else "dette",
            )
            items.append(current)
            continue
        if current is None:
            continue
        what, priority = _WHAT.match(line), _PRIORITY.match(line)
        if what and not current.description:
            current.description = what.group(1)[: suivi.MAX_DESCRIPTION]
        if priority:
            current.priorite = priority.group(1)
    return items


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        log.warning("suivi import: cannot read %s: %s", path, exc)
        return None


def _markdown_files(root: Path, directory: str) -> List[Path]:
    base = root / directory
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob("*.md") if p.is_file())


def collect(root: Path) -> List[Item]:
    """Every importable item under ``root``, plans first."""
    items: List[Item] = []
    plan_texts: List[str] = []
    for path in _markdown_files(root, PLANS_DIR):
        text = _read(path)
        if text is not None:
            plan_texts.append(text)
            items.extend(parse_plan(text, path.relative_to(root).as_posix()))
    for path in _markdown_files(root, SPECS_DIR):
        ref = path.relative_to(root).as_posix()
        if any(ref in text for text in plan_texts):
            continue  # already planned: its tasks are the tickets
        text = _read(path)
        item = parse_spec(text, ref) if text is not None else None
        if item is not None:
            items.append(item)
    todos = root / TODOS_FILE
    if todos.is_file():
        text = _read(todos)
        if text is not None:
            items.extend(parse_todos(text))
    return items


def import_sources(root: Path, auteur: str = "admin") -> Dict[str, int]:
    """Create or promote tickets from the sources; return the counts."""
    counts = {"crees": 0, "mis_a_jour": 0, "inchanges": 0}
    seen = set()
    for item in collect(Path(root)):
        if item.source_ref in seen:
            continue
        seen.add(item.source_ref)
        existing = suivi.find_by_source_ref(item.source_ref)
        if existing is None:
            suivi.create_ticket(
                item.titre,
                description=item.description,
                priorite=item.priorite,
                type=item.type,
                source=item.source,
                source_ref=item.source_ref,
                statut="en_revue" if item.coche else "a_trier",
                auteur=auteur,
            )
            counts["crees"] += 1
        elif item.coche and existing["statut"] in _PROMOTABLE:
            suivi.update_ticket(existing["id"], statut="en_revue", auteur="import")
            counts["mis_a_jour"] += 1
        else:
            counts["inchanges"] += 1
    return counts
