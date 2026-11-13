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
            assert "--require-hashes" in argv and "--strict" in argv, step["id"]
            audited |= {argv[i + 1] for i, a in enumerate(argv) if a == "-r"}
    return audited


def _audited_by_make() -> set:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = re.search(r"^audit:.*?(?=^\S)", makefile, re.M | re.S).group(0)
    assert "pip-audit" in target and "--require-hashes" in target
    return set(re.findall(r"requirements[\w-]*\.lock", target))


def test_the_three_locks_exist():
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
