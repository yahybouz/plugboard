"""D-61: former commit identities are shown as the public one.

The history is not rewritten. ``.mailmap`` maps every former address (author
and committer) to ``Hermes UAT <hermes@uat.local>``, so ``git log``,
``git shortlog`` and the forges show the public identity.

The test reads ``.mailmap`` and never names an address; a failure gives line
numbers or counts only. The local CI runs on an export without ``.git``: the
format check always runs, the ``git check-mailmap`` check is skipped there.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAILMAP = ROOT / ".mailmap"
CANONICAL = "Hermes UAT <hermes@uat.local>"
# A former address may have a host without a dot (a local machine name).
ENTRY = re.compile(r"Hermes UAT <hermes@uat\.local> <(?P<old>[^<>\s@]+@[^<>\s@]+)>")


def _entries() -> list[tuple[int, str]]:
    """[(line number, useful line)] — comments and blank lines skipped."""
    assert MAILMAP.is_file(), ".mailmap is missing at the repository root"
    rows = MAILMAP.read_text(encoding="utf-8").splitlines()
    return [
        (i, row.strip())
        for i, row in enumerate(rows, 1)
        if row.strip() and not row.lstrip().startswith("#")
    ]


def _addresses() -> list[str]:
    return [ENTRY.fullmatch(row).group("old") for _, row in _entries()]


def test_every_line_maps_to_the_public_identity():
    entries = _entries()
    assert entries, ".mailmap has no mapping"
    malformed = [i for i, row in entries if not ENTRY.fullmatch(row)]
    assert malformed == [], f"malformed .mailmap lines: {malformed}"
    old = [a.lower() for a in _addresses()]
    assert len(old) == len(set(old)), "an address is mapped twice"
    assert "hermes@uat.local" not in old, "the public address maps to itself"


def _repository_root() -> Path | None:
    if shutil.which("git") is None:
        return None
    done = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if done.returncode or Path(done.stdout.strip()).resolve() != ROOT:
        return None
    return ROOT


def test_git_shows_the_public_identity_for_every_listed_address():
    addresses = _addresses()
    if _repository_root() is None:
        pytest.skip("no git history here (CI export): format checked only")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    contacts = []
    for address in addresses:
        contacts += [f"<{address}>", f"Any Name <{address.upper()}>"]
    done = subprocess.run(
        ["git", "-C", str(ROOT), "check-mailmap", *contacts],
        capture_output=True,
        text=True,
        env=env,
    )
    assert done.returncode == 0, "git check-mailmap failed"
    shown = done.stdout.splitlines()
    assert len(shown) == len(contacts)
    wrong = [i for i, who in enumerate(shown, 1) if who != CANONICAL]
    assert wrong == [], f"{len(wrong)} contact(s) not mapped to the public identity"
