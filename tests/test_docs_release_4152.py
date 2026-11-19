"""What the operator and integrators read for 4.15.2 (release 4.15.2).

The version is 4.15.2 in ``VERSION``, ``plugboard.__version__``, the compose
image tag and the health examples; the dated CHANGELOG entry names every
ticket of the release and the commit of each on the release branch, says that
the API contract is unchanged (3.2.0), lists the three new production settings
with their defaults, what was left out and what was not verified; the
production runbook has a "Switching to 4.15.2" section that names the three
settings, the changes of behaviour and the checks to make after the switch.

The files in ``docs/api/`` are not pinned to a later contract here: the
release note keeps the contract 4.15.2 publishes (3.2.0).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import plugboard

ROOT = Path(__file__).resolve().parents[1]
DATE = "2026-11-19"

# Ticket -> its commits on release/4.15.2 (the first characters of the SHA).
COMMITS = {
    "BUG-157": ("19b33f1", "56cf37f", "f6b7b8f", "b79a5e6", "ca2cd61"),
    "BUG-160": ("cd97d24",),
    "PLU-171": ("3d72545", "cc167f4", "7fdf823", "cb283f0", "e240253"),
    "BUG-161": ("7765067", "0b45bba"),
    "PLU-35": ("0560c2f",),
    "PLU-170": ("c9ee79e",),
    "PLU-169": ("d327220",),
    "PLU-167": ("9a4da01",),
    "PLU-88": ("e4cfc86",),
    "BUG-158": ("b3a016e",),
    "PLU-165": ("a38685f",),
    "PLU-97": ("deccb2d", "d5603fd"),
    "BUG-151": ("5211341",),
    "BUG-154": ("4398f8c",),
    "BUG-155": ("8d2e2f2",),
    "PLU-162": ("6ab5f3e", "43f7090"),
    "PLU-100": ("68209bd",),
    "PLU-159": ("8d301b0",),
}


def _section(text: str, title: str, level: str = "##") -> str:
    match = re.search(
        rf"(?ms)^{level} {re.escape(title)}\n(.*?)(?=^{level} |^## |\Z)", text
    )
    assert match, title
    return match.group(1)


def _changelog() -> str:
    return (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _release() -> str:
    return _section(_changelog(), f"[4.15.2] — {DATE}")


def _switching() -> str:
    text = (ROOT / "docs" / "runbooks" / "production-instance.md").read_text(
        encoding="utf-8"
    )
    return _section(text, "Switching to 4.15.2")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_version_is_4_15_2():
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "4.15.2"
    assert plugboard.__version__ == "4.15.2"


def test_the_compose_file_and_the_health_examples_name_4_15_2():
    compose = (ROOT / "deploy" / "ovh" / "compose.yml").read_text(encoding="utf-8")
    assert "${PLUGBOARD_IMAGE:-plugboard:private-full-v4.15.2}" in compose
    monitoring = (ROOT / "docs" / "guide" / "monitoring.md").read_text(encoding="utf-8")
    assert '{"ok": true, "version": "4.15.2"}' in monitoring
    assert '{"ready": true, "version": "4.15.2"}' in monitoring
    assert '"version": "4.15.1"' not in monitoring


def test_one_entry_under_unreleased():
    # Changes made after the release go under Unreleased, above this entry.
    text = _changelog()
    assert text.count("## [4.15.2]") == 1
    assert text.index("## [Unreleased]") < text.index(f"## [4.15.2] — {DATE}")
    assert text.index(f"## [4.15.2] — {DATE}") < text.index("## [4.15.1]")


def test_each_section_title_appears_once():
    titles = re.findall(r"(?m)^### (.+)$", _release())
    assert titles == ["Security", "Fixed", "Changed"]


def test_the_entry_names_every_ticket_and_its_commits():
    release = _release()
    (commits,) = [
        line
        for line in release.splitlines()
        if line.startswith("**Tickets and commits**")
    ]
    for ticket, shas in COMMITS.items():
        assert ticket in release, ticket
        for sha in shas:
            assert f"`{sha}`" in commits, (ticket, sha)


def test_the_listed_commits_are_on_the_release_branch():
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("no git history here")
    for ticket, shas in COMMITS.items():
        for sha in shas:
            done = subprocess.run(
                ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
            assert done.returncode == 0, (ticket, sha)


def test_the_entry_says_what_each_ticket_did():
    security = _flat(_section(_release(), "Security", level="###"))
    fixed = _flat(_section(_release(), "Fixed", level="###"))
    changed = _flat(_section(_release(), "Changed", level="###"))
    assert "BUG-157" in security and "`agy-store-*`" in security
    assert "PLU-162" in security and "--unshare-net" in security
    assert "sent another TLS name" in security
    assert "PLU-169" in security and "PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS" in security
    assert "BUG-160" in fixed and "web_budget_exhausted" in fixed
    assert "BUG-161" in fixed and "PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN" in fixed
    assert "PLU-35" in changed and "PLUGBOARD_DECISIONS_ENABLED" in changed
    assert "PLU-97" in changed and "plugboard/auth_core/" in changed


def test_the_contract_is_unchanged_at_3_2_0():
    (note,) = [
        line for line in _release().splitlines() if line.startswith("**API contract**")
    ]
    assert "unchanged" in note and "**3.2.0**" in note
    openapi = json.loads((ROOT / "docs" / "api" / "openapi.json").read_text("utf-8"))
    mcp = json.loads((ROOT / "docs" / "api" / "mcp.json").read_text("utf-8"))
    assert openapi["info"]["version"] == mcp["contract_version"] == "3.2.0"
    from plugboard.api_contract import CONTRACT_VERSION

    assert CONTRACT_VERSION == "3.2.0"


def test_the_entry_lists_the_three_settings_what_was_left_out_and_not_verified():
    release = _flat(_release())
    (settings,) = [
        _flat(line)
        for line in _release().splitlines()
        if line.startswith("**Production settings**")
    ]
    assert "three variables are new" in settings
    assert "`PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN`" in settings and "`20000`" in settings
    assert "`PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS`" in settings and "`600`" in settings
    assert (
        "`PLUGBOARD_DECISIONS_ENABLED`" in settings and "before the switch" in settings
    )
    assert "**Left out of the release**" in release
    for ticket in ("PLU-166", "PLU-35", "PLU-100", "P1-11"):
        left = release.split("**Left out of the release**")[1].split("**")[0]
        assert ticket in left, ticket
    assert "PB-AUTH-01" in release and "test_people.py" in release
    assert "**Not verified**" in release
    assert "Rolling back to 4.15.1" in release


def test_the_runbook_says_how_to_switch_to_4_15_2():
    switching = _flat(_switching())
    assert "plugboard:private-full-v4.15.2" in switching
    for name, default in (
        ("PLUGBOARD_WEB_HOLD_TOKENS_PER_TURN", "20000"),
        ("PLUGBOARD_EGRESS_TUNNEL_MAX_SECONDS", "600"),
        ("PLUGBOARD_DECISIONS_ENABLED", "off"),
    ):
        assert f"`{name}`" in switching, name
        assert default in switching, (name, default)
    assert "before the switch" in switching
    assert "check-agys-shared-bin-and-builtin-once-after-switching-to-4152" in switching
    assert "PLUGBOARD_SUBPROCESS_CWD" in switching
    assert "provider_sandbox_unavailable" in switching
    assert "web_budget_exhausted" in switching
    assert '"store_private": true' in switching
    assert "agy egress refused" in switching and "sent another TLS name" in switching
    assert "web egress refused" in switching and "web egress tunnel" in switching
    assert '{"ok": true, "version": "4.15.2"}' in switching
    assert "Rolling back to 4.15.1 works" in switching


def test_the_runbook_lists_what_a_real_call_must_show():
    switching = _flat(_switching())
    for proof in (
        "two real agy calls",
        "oauth2.googleapis.com",
        "cloudcode-pa.googleapis.com",
        "web run of claude in its cage",
        "tool_use",
        "two claude routes",
        "BUG-161",
        "/v1/decisions/models",
        "plugboard-userns",
    ):
        assert proof in switching, proof
