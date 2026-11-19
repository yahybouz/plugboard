"""What the operator and integrators read for 4.15.1 (release 4.15.1).

The version is 4.15.1 in ``VERSION``, ``plugboard.__version__``, the compose
image tag and the health examples; the dated CHANGELOG entry names every
ticket of the release (BUG-156, PLU-161, PLU-160, BUG-159), says that the API
contract is unchanged (3.2.0), lists the production settings (none) and what
was not verified; the production runbook has a "Switching to 4.15.1" section
that says nothing has to be set for production to work as before and what to
read after the switch (agy's cage and its egress).

The files in ``docs/api/`` are not pinned to a later contract here: the
release note keeps the contract 4.15.1 publishes (3.2.0).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import plugboard

ROOT = Path(__file__).resolve().parents[1]


def _section(text: str, title: str, level: str = "##") -> str:
    match = re.search(
        rf"(?ms)^{level} {re.escape(title)}\n(.*?)(?=^{level} |^## |\Z)", text
    )
    assert match, title
    return match.group(1)


def _release() -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return _section(text, "[4.15.1] — 2026-11-19")


def _switching() -> str:
    text = (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
        encoding="utf-8"
    )
    return _section(text, "Switching to 4.15.1")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_version_is_4_15_1():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "4.15.1"
    assert plugboard.__version__ == "4.15.1"


def test_the_compose_file_and_the_health_examples_name_4_15_1():
    compose = (ROOT / "deploy" / "ovh" / "compose.yml").read_text(encoding="utf-8")
    assert "${PLUGBOARD_IMAGE:-plugboard:private-full-v4.15.1}" in compose
    monitoring = (ROOT / "docs" / "guide" / "monitoring.md").read_text(encoding="utf-8")
    assert '{"ok": true, "version": "4.15.1"}' in monitoring
    assert '{"ready": true, "version": "4.15.1"}' in monitoring
    assert '"version": "4.15.0"' not in monitoring


def test_one_entry_under_unreleased():
    # Changes made after the release go under Unreleased, above this entry.
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.count("## [4.15.1]") == 1
    assert text.index("## [Unreleased]") < text.index("## [4.15.1] — 2026-11-19")
    assert text.index("## [4.15.1] — 2026-11-19") < text.index("## [4.15.0]")


def test_the_entry_names_every_ticket_of_the_release():
    release = _flat(_release())
    security = _flat(_section(_release(), "Security", level="###"))
    fixed = _flat(_section(_release(), "Fixed", level="###"))
    assert "BUG-156" in security and "--unshare-net" in security
    assert "port `443`" in security and "64 connections" in security
    assert "PLU-161" in security
    for network in ("::/96", "::ffff:0:0:0/96", "fec0::/10"):
        assert network in security, network
    assert "PLU-160" in security and "`ALL_PROXY`" in security
    assert "BUG-159" in fixed and "tokens claude reports" in fixed
    assert "Rolling back to 4.15.0" in release


def test_the_contract_is_unchanged_at_3_2_0():
    (note,) = [
        line for line in _release().splitlines() if line.startswith("**API contract**")
    ]
    assert "unchanged" in note and "**3.2.0**" in note
    openapi = json.loads((ROOT / "docs" / "api" / "openapi.json").read_text("utf-8"))
    mcp = json.loads((ROOT / "docs" / "api" / "mcp.json").read_text("utf-8"))
    assert openapi["info"]["version"] == mcp["contract_version"] == "3.2.0"


def test_no_production_setting_is_added():
    release = _flat(_release())
    assert "**Production settings**: no environment variable is added" in release
    assert "nothing to set for production to work as before" in release
    assert "**Not verified**: agy's real run through its egress" in release


def test_the_runbook_says_how_to_switch_to_4_15_1():
    switching = _flat(_switching())
    assert "plugboard:private-full-v4.15.1" in switching
    assert "Nothing to set for production to work as before" in switching
    assert '"shared_with_container": false' in switching
    assert "agy egress refused" in switching
    assert '{"ok": true, "version": "4.15.1"}' in switching
    assert "Rolling back to 4.15.0 works" in switching
