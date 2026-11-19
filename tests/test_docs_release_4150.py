"""What the operator and integrators read for 4.15.0 (release 4.15.0).

The version is 4.15.0 in ``VERSION``, ``plugboard.__version__``, the compose
image tag and the health examples; the dated CHANGELOG entry holds both
additions merged for this release (tools emulated on CLI routes, ADR-039;
web access per key, PLU-158, with ADR-040), one API contract paragraph that
publishes 3.2.0, the production settings and what was not verified; the
production runbook has a "Switching to 4.15.0" section that says nothing has
to be set for production to work as before.
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
    return _section(text, "[4.15.0] — 2026-11-19")


def _switching() -> str:
    text = (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
        encoding="utf-8"
    )
    return _section(text, "Switching to 4.15.0")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_version_is_4_15_0():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "4.15.0"
    assert plugboard.__version__ == "4.15.0"


def test_the_compose_file_and_the_health_examples_name_4_15_0():
    compose = (ROOT / "deploy" / "ovh" / "compose.yml").read_text(encoding="utf-8")
    assert "${PLUGBOARD_IMAGE:-plugboard:private-full-v4.15.0}" in compose
    monitoring = (ROOT / "docs" / "guide" / "monitoring.md").read_text(encoding="utf-8")
    assert '{"ok": true, "version": "4.15.0"}' in monitoring
    assert '{"ready": true, "version": "4.15.0"}' in monitoring
    assert '"version": "4.14.6"' not in monitoring


def test_one_entry_under_an_empty_unreleased():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.count("## [4.15.0]") == 1
    assert text.index("## [Unreleased]") < text.index("## [4.15.0] — 2026-11-19")
    assert text.index("## [4.15.0] — 2026-11-19") < text.index("## [4.14.6]")
    assert _section(text, "[Unreleased]").strip() == ""


def test_the_entry_holds_both_merged_features():
    added = _flat(_section(_release(), "Added", level="###"))
    assert "ADR-039" in added and "PLUGBOARD_CLI_TOOLS_EMULATION=1" in added
    assert "PLU-158" in added and "web_with_tools_unsupported" in added
    assert "ADR-040" in added and "port `443`" in added


def test_one_contract_version_3_2_0():
    release = _release()
    (note,) = [
        line for line in release.splitlines() if line.startswith("**API contract**")
    ]
    assert "publishes **3.2.0**" in note
    openapi = json.loads((ROOT / "docs" / "api" / "openapi.json").read_text("utf-8"))
    mcp = json.loads((ROOT / "docs" / "api" / "mcp.json").read_text("utf-8"))
    assert openapi["info"]["version"] == mcp["contract_version"] == "3.2.0"


def test_production_settings_are_safe_by_default():
    release = _flat(_release())
    assert "**Production settings**: nothing to set" in release
    for name in (
        "PLUGBOARD_CLI_TOOLS_EMULATION",
        "PLUGBOARD_MAX_INFLIGHT_WEB",
        "PLUGBOARD_WEB_TIMEOUT_S",
        "PLUGBOARD_WEB_HELPER_MODEL",
    ):
        assert name in release, name
    assert "**Not verified**: a real web run" in release


def test_the_runbook_says_how_to_switch_to_4_15_0():
    switching = _flat(_switching())
    assert "plugboard:private-full-v4.15.0" in switching
    assert "Nothing to set for production to work as before" in switching
    assert "PUT /admin/keys/{key_id}/web" in switching
    assert '{"ok": true, "version": "4.15.0"}' in switching


def test_the_adr_040_is_indexed():
    index = (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
    assert "ADR-040-web-et-outils.md" in index
    assert (ROOT / "docs" / "adr" / "ADR-040-web-et-outils.md").is_file()
