"""Every dependency lock the project ships is audited for CVEs (PLU-30).

pip-audit read only requirements-dev.lock: the MCP server's lock
(requirements-mcp.lock, installed in CI) and the decisions runtime's
(requirements-decisions.lock) were never audited.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOCKS = sorted(p.name for p in ROOT.glob("requirements*.lock"))


def _audited_in_ci() -> set:
    pipeline = json.loads((ROOT / "ci/pipeline.json").read_text(encoding="utf-8"))
    audited = set()
    for step in pipeline["steps"]:
        argv = step.get("argv", [])
        if argv and argv[0].endswith("/pip-audit"):
            for flag in ("--require-hashes", "--strict", "--disable-pip"):
                assert flag in argv, (step["id"], flag)
            audited |= {argv[i + 1] for i, a in enumerate(argv) if a == "-r"}
    return audited


def _audited_by_make() -> set:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = re.search(r"^audit:.*?(?=^\S|\Z)", makefile, re.M | re.S)
    assert target, "audit target not found in the Makefile"
    return _loop_locks(target.group(0))


def _loop_locks(text: str) -> set:
    """Locks of the `for lock in ...; do pip-audit ... -r $lock` loop."""
    loop = re.search(r"for lock in ([^;]+);\s*do[^\n]*?(?:\\\n[^\n]*?)*pip-audit", text)
    assert loop, "no pip-audit loop over the locks"
    for flag in ("--require-hashes", "--strict", "--disable-pip"):
        assert flag in text, flag
    return set(loop.group(1).split())


def test_the_known_locks_exist():
    assert {
        "requirements.lock",  # the image's
        "requirements-dev.lock",
        "requirements-mcp.lock",
        "requirements-decisions.lock",
    } <= set(LOCKS)


@pytest.mark.parametrize("lock", LOCKS)
def test_every_lock_is_audited_in_ci(lock):
    assert lock in _audited_in_ci()


@pytest.mark.parametrize("lock", LOCKS)
def test_every_lock_is_audited_by_make_audit(lock):
    assert lock in _audited_by_make()


@pytest.mark.parametrize("path", [".gitlab-ci.yml", ".github/workflows/ci.yml"])
def test_remote_ci_definitions_audit_every_lock(path):
    # Kept in step with ci/pipeline.json, which translates them.
    assert _loop_locks((ROOT / path).read_text(encoding="utf-8")) == set(LOCKS)
