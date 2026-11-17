"""What the operator and integrators read for 4.14.6 (release 4.14.6).

The version is 4.14.6 in ``VERSION``, ``plugboard.__version__``, the compose
image tag and the health examples; the dated CHANGELOG entry names every
ticket of the release with its commit on the release branch, says that the
API contracts are unchanged (3.0.1), says why gh is built from its sources
and that it is temporary, and lists what was not verified; the production
runbook has a "Switching to 4.14.6" section that says what to check after the
switch (the gh version, the health version).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import plugboard

ROOT = Path(__file__).resolve().parents[1]

# Ticket -> its commits on release/4.14.6 (the review fixes of SEC-VEILLE
# and BUG-143 came after the release commit).
COMMITS = {
    "SEC-VEILLE": ("9da4ba3", "625d431"),
    "BUG-143": ("12e3fdc", "4c7e5d1"),
    "BUG-146": ("6153cab",),
    "PLU-81": ("88bd506",),
    "BUG-138": ("88bd506",),
    "D-61": ("cf3b751",),
}


def _section(text: str, title: str, level: str = "##") -> str:
    match = re.search(
        rf"(?ms)^{level} {re.escape(title)}\n(.*?)(?=^{level} |^## |\Z)", text
    )
    assert match, title
    return match.group(1)


def _release() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return _section(text, "[4.14.6] — 2026-11-17")


def _switching() -> str:
    text = (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
        encoding="utf-8"
    )
    return _section(text, "Switching to 4.14.6")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_version_is_4_14_6():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "4.14.6"
    assert plugboard.__version__ == "4.14.6"


def test_the_compose_file_and_the_health_examples_name_4_14_6():
    compose = (ROOT / "deploy" / "ovh" / "compose.yml").read_text(encoding="utf-8")
    assert "${PLUGBOARD_IMAGE:-plugboard:private-full-v4.14.6}" in compose
    monitoring = (ROOT / "docs" / "guide" / "monitoring.md").read_text(encoding="utf-8")
    assert '{"ok": true, "version": "4.14.6"}' in monitoring
    assert '{"ready": true, "version": "4.14.6"}' in monitoring
    assert '"version": "4.14.5"' not in monitoring


def test_the_entry_sits_under_unreleased():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.index("## [Unreleased]") < text.index("## [4.14.6] — 2026-11-17")
    assert text.index("## [4.14.6] — 2026-11-17") < text.index("## [4.14.5]")


def test_each_ticket_cites_its_release_commit():
    release = _release()
    for ticket, shas in COMMITS.items():
        lines = [
            line
            for line in release.splitlines()
            if re.search(rf"(?<![\w-]){re.escape(ticket)}(?![\w-])", line)
        ]
        assert lines, ticket
        for sha in shas:
            assert any(f"`{sha}`" in line for line in lines), (ticket, sha)


def test_the_gh_build_is_explained_and_temporary():
    security = _section(_release(), "Security", level="###")
    (entry,) = [
        line
        for line in security.splitlines()
        if line.startswith("- ") and "SEC-VEILLE" in line
    ]
    flat = _flat(entry)
    for needle in (
        "CVE-2026-78661",
        "golang.org/x/net",
        "v0.62.0",
        "v0.61.0",
        "2.104.0",
        "golang:1.27.4-trixie",
        "github.com",
        "proxy.golang.org",
        "gnupg",
        "Temporary",
        "ci/image-path-executables.txt",
        "/usr/local/bin/gh",
    ):
        assert needle in flat, needle


def test_the_quota_excerpt_redaction_is_listed():
    security = _section(_release(), "Security", level="###")
    (entry,) = [
        line
        for line in security.splitlines()
        if line.startswith("- ") and "BUG-143" in line
    ]
    for needle in (
        "/admin/claude-quota",
        "provider.quota_limit",
        "x-api-key",
        "Authorization",
        "token=",
        "AIza",
        'token="',
        "ANTHROPIC_API_KEY=",
        "GITHUB_TOKEN=",
        "_PASSWORD=",
        "sort_key=",
        "100,000",
    ):
        assert needle in entry, needle


def test_the_contracts_are_unchanged():
    release = _release()
    (note,) = [
        line for line in release.splitlines() if line.startswith("**API contract**")
    ]
    assert "3.0.1" in note and "unchanged" in note
    openapi = json.loads((ROOT / "docs" / "api" / "openapi.json").read_text("utf-8"))
    mcp = json.loads((ROOT / "docs" / "api" / "mcp.json").read_text("utf-8"))
    assert openapi["info"]["version"] == "3.0.1"
    assert mcp["contract_version"] == "3.0.1"


def test_the_release_says_what_was_not_verified():
    release = _release()
    (note,) = [
        line for line in release.splitlines() if line.startswith("**Not verified**")
    ]
    for needle in (
        "trivy",
        "gh auth token",
        "\\uXXXX",
        "4.14.5",
        "image-path-executables.txt",
    ):
        assert needle in note, needle


def test_the_runbook_says_how_to_switch_to_4_14_6():
    section = _switching()
    flat = _flat(section)
    assert "plugboard:private-full-v4.14.6" in section
    assert "gh version 2.104.0 (2026-11-12)" in flat
    assert '"version": "4.14.6"' in flat
    assert "prod-gate.md" in section


def test_the_4_14_5_switch_section_stays():
    _section(
        (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
            encoding="utf-8"
        ),
        "Switching to 4.14.5",
    )
