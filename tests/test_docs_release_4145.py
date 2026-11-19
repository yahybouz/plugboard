"""What the operator and integrators read for 4.14.5 (release 4.14.5).

The version is 4.14.5 or a later release in ``VERSION`` and
``plugboard.__version__``, and the compose image tag and the health examples
name that version (4.14.6 pins its own in its test); the dated CHANGELOG
entry names every
ticket of the release with its commit on the release branch (never the
commit of a work branch), says that the API contracts are unchanged (3.0.1),
says that BUG-106 stays open, and lists what was not verified; the production
runbook has a "Switching to 4.14.5" section that says what to check after the
switch (PID 1 by ``deploy/check-pid1.py``, the Claude quota watch).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import plugboard

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


def _release_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _current_version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def test_the_version_is_4_14_5_or_later():
    version = _current_version()
    assert _release_tuple(version) >= (4, 14, 5)
    assert plugboard.__version__ == version


def test_the_compose_file_and_the_health_examples_name_the_current_version():
    version = _current_version()
    compose = (ROOT / "deploy" / "ovh" / "compose.yml").read_text(encoding="utf-8")
    assert f"${{PLUGBOARD_IMAGE:-plugboard:private-full-v{version}}}" in compose
    monitoring = (ROOT / "docs" / "guide" / "monitoring.md").read_text(encoding="utf-8")
    assert f'{{"ok": true, "version": "{version}"}}' in monitoring
    assert f'{{"ready": true, "version": "{version}"}}' in monitoring
    assert '"version": "4.14.4"' not in monitoring


def test_the_entry_sits_under_unreleased():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.index("## [Unreleased]") < text.index("## [4.14.5] — 2026-11-16")
    assert text.index("## [4.14.5] — 2026-11-16") < text.index("## [4.14.4]")


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


def test_the_contracts_are_unchanged():
    release = _release()
    (note,) = [
        line for line in release.splitlines() if line.startswith("**API contract**")
    ]
    assert "3.0.1" in note and "unchanged" in note
    openapi = json.loads((ROOT / "docs" / "api" / "openapi.json").read_text("utf-8"))
    mcp = json.loads((ROOT / "docs" / "api" / "mcp.json").read_text("utf-8"))
    # 3.0.1 or a later contract (PLU-158 made it 3.1.0 after this release).
    for version in (openapi["info"]["version"], mcp["contract_version"]):
        assert tuple(map(int, version.split("."))) >= (3, 0, 1)


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
