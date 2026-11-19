"""What the operator and integrators read for 4.14.5 (release 4.14.5).

The dated CHANGELOG entry appears once, under Unreleased, and attributes
every change of the release to its ticket and to its commit on the release
branch, and to no other (never the commit of a work branch); it says that
the API contracts are unchanged (3.0.1), says that BUG-106 stays open, and
lists what was not verified; the production runbook has a "Switching to
4.14.5" section that names the 4.14.5 image and says what to check after the
switch (PID 1 by ``deploy/check-pid1.py``, the Claude quota watch).

These are the facts of 4.14.5 only: the current version, the compose image
tag and the health examples are pinned by the test of the current release,
not here. The release note keeps the contract 4.14.5 published (3.0.1); the
files in ``docs/api/`` are not pinned here, since a later change may publish
a newer contract (3.1.0, ADR-039), as 4.14.3 and 4.14.4 allow for theirs.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Ticket -> its commit on release/4.14.5.
COMMITS = {
    "BUG-133": "cd6260d",
    "BUG-126": "14dfb5b",
    "BUG-128": "e768df3",
    "BUG-130": "1959615",
    "BUG-132": "97bb72d",
    "PB-REL-01": "4231df1",
    "BUG-106": "7e582ba",
    "BUG-127": "817651b",
    "BUG-129": "9264b55",
    "PLU-67": "544ff8b",
    "BUG-131": "4750a4c",
}

# The same changes on the work branches of the release, never published.
WORK_BRANCH_COMMITS = (
    "ef6aa10",
    "11d3b74",
    "1d2c221",
    "b0c33e9",
    "77c34a4",
    "dbeb23e",
    "cf9f735",
    "0186e3f",
    "cb3ae50",
    "e584818",
    "a2a29a8",
    "d86bd4e",
)


def _section(text: str, title: str, level: str = "##") -> str:
    match = re.search(
        rf"(?ms)^{level} {re.escape(title)}\n(.*?)(?=^{level} |^## |\Z)", text
    )
    assert match, title
    return match.group(1)


def _release() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return _section(text, "[4.14.5] — 2026-11-16")


def _switching() -> str:
    text = (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
        encoding="utf-8"
    )
    return _section(text, "Switching to 4.14.5")


def _flat(text: str) -> str:
    return " ".join(text.split())


# A change of the entry: "(TICKET, `sha`)" after its bold title.
ATTRIBUTION = re.compile(r"\*\* \(([A-Z]+(?:-[A-Z]+)?-\d+), `([0-9a-f]{7,40})`\)")


def test_one_entry_under_unreleased():
    # Changes made after the release go under Unreleased, above this entry.
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.count("## [4.14.5]") == 1
    assert text.index("## [Unreleased]") < text.index("## [4.14.5] — 2026-11-16")
    assert text.index("## [4.14.5] — 2026-11-16") < text.index("## [4.14.4]")


def test_every_change_is_attributed_to_its_ticket_and_release_commit():
    # Exactly the tickets of the release, each with its own commit: no change
    # is missing, none is added, none cites another ticket's commit.
    pairs = ATTRIBUTION.findall(_release())
    assert len(pairs) == len(COMMITS)
    assert dict(pairs) == COMMITS


def test_each_ticket_cites_its_release_commit():
    release = _release()
    for ticket, sha in COMMITS.items():
        lines = [
            line
            for line in release.splitlines()
            if re.search(rf"\b{re.escape(ticket)}\b", line)
        ]
        assert lines, ticket
        assert any(f"`{sha}`" in line for line in lines), ticket


def test_no_work_branch_commit_is_cited():
    release = _release()
    for sha in WORK_BRANCH_COMMITS:
        assert sha not in release, sha


def test_the_release_note_says_the_contract_is_unchanged():
    release = _release()
    (note,) = [
        line for line in release.splitlines() if line.startswith("**API contract**")
    ]
    assert "3.0.1" in note and "unchanged" in note


def test_bug_106_is_a_bench_and_stays_open():
    added = _section(_release(), "Added", level="###")
    (entry,) = [line for line in added.splitlines() if "BUG-106" in line]
    assert "scripts/bench_agy_first_token.py" in entry
    assert "stays open" in entry


def test_the_release_says_what_was_not_verified():
    release = _release()
    (note,) = [
        line for line in release.splitlines() if line.startswith("**Not verified**")
    ]
    for needle in (
        "usage limit",
        "Monitoring",
        "check-pid1.py",
        "publish-check",
        "BUG-106",
        "Safari",
        "iPhone",
    ):
        assert needle in note, needle


def test_the_runbook_says_how_to_switch_to_4_14_5():
    section = _switching()
    flat = _flat(section)
    assert "plugboard:private-full-v4.14.5" in section
    assert "deploy/check-pid1.py" in section
    assert "/usr/bin/tini" in section and "STOP" in section
    assert "/admin/claude-quota" in section
    assert "plugboard_claude_limit_total" in section
    assert "zombie" in flat


def test_the_4_14_4_switch_section_stays():
    _section(
        (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
            encoding="utf-8"
        ),
        "Switching to 4.14.4",
    )


def test_the_runbook_index_lists_the_publication_runbook():
    index = (ROOT / "docs" / "runbooks" / "README.md").read_text(encoding="utf-8")
    assert "[release-publish.md](release-publish.md)" in index
