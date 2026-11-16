"""What the operator and integrators read for 4.14.4 (release 4.14.4).

The version is 4.14.4 or a later release in ``VERSION`` and
``plugboard.__version__`` (4.14.5 pins its own in its test); the dated
CHANGELOG entry names every ticket of the release with its commit, says which
API contract it publishes (3.0.1, a wording change from 3.0.0) and corrects
the 4.14.2 note on the pid-1 check (BUG-114); the production runbook has a
"Switching to 4.14.4" section that says what to check after the switch (tini
as PID 1, no zombie, both sandbox checks) and warns about an ``entrypoint:``.
"""

from __future__ import annotations

import re
from pathlib import Path

import plugboard

ROOT = Path(__file__).resolve().parents[1]

TICKETS = (
    "BUG-125",
    "BUG-113",
    "BUG-123",
    "PLU-77",
    "BUG-117",
    "PLU-71",
    "BUG-114",
    "BUG-108",
    "BUG-115",
    "BUG-109",
    "BUG-122",
    "BUG-91",
    "BUG-94",
    "PLU-60",
    "BUG-124",
    "BUG-90",
    "BUG-104",
    "BUG-105",
    "BUG-92",
    "BUG-93",
    "PLU-55",
    "BUG-84",
    "PLU-66",
)


def _section(text: str, title: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text)
    assert match, title
    return match.group(1)


def _release() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return _section(text, "[4.14.4] — 2026-11-16")


def _switching() -> str:
    text = (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
        encoding="utf-8"
    )
    return _section(text, "Switching to 4.14.4")


def _release_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_the_version_is_4_14_4_or_later():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert _release_tuple(version) >= (4, 14, 4)
    assert plugboard.__version__ == version


def test_the_entry_sits_under_unreleased():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.index("## [Unreleased]") < text.index("## [4.14.4] — 2026-11-16")
    assert text.index("## [4.14.4] — 2026-11-16") < text.index("## [4.14.3]")


def test_the_entry_names_every_ticket():
    release = _release()
    for ticket in TICKETS:
        assert re.search(rf"\b{re.escape(ticket)}\b", release), ticket


def test_each_ticket_cites_a_commit():
    release = _release()
    for ticket in TICKETS:
        lines = [line for line in release.splitlines() if ticket in line]
        assert any(re.search(r"`[0-9a-f]{7}`", line) for line in lines), ticket


def test_the_release_names_its_contract():
    release = _release()
    (note,) = [
        line for line in release.splitlines() if line.startswith("**API contract**")
    ]
    assert "3.0.1" in note and "3.0.0" in note
    assert "rpm_limit" in note


def test_the_4_14_2_pid1_note_is_corrected():
    release = _release()
    assert "probe_is_pid1" in release
    assert "4.14.2" in release


def test_the_release_says_what_was_not_verified():
    release = _release()
    assert any(line.startswith("**Not verified**") for line in release.splitlines())


def test_the_runbook_says_how_to_switch_to_4_14_4():
    section = _switching()
    flat = " ".join(section.split())
    assert "plugboard:private-full-v4.14.4" in section
    assert "/usr/bin/tini -- python run.py" in section
    assert "zombie" in flat
    assert "verify-agy-sandbox.py" in section
    assert '"probe_is_pid1": true' in section
    assert "verify-codex-sandbox.py" in section
    assert "entrypoint:" in section


def test_the_4_14_3_switch_section_stays():
    _section(
        (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
            encoding="utf-8"
        ),
        "Switching to 4.14.3",
    )
