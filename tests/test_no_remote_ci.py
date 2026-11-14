"""The repository has no remote pipeline: CI runs locally (ci/pipeline.json).

A push to GitLab or GitHub must trigger nothing, so the remote definitions are
gone and no living file presents them as the pipeline. Historical records
(CHANGELOG, ADRs, specs and plans, decision manifests) keep their wording.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REMOTE_DEFINITIONS = (".gitlab-ci.yml", ".github/workflows")

# Living files that must not cite a remote pipeline any more.
SCANNED = (
    "README.md",
    "CONTRIBUTING.md",
    "TODOS.md",
    "SECURITY.md",
    "DESIGN.md",
    "Makefile",
    "Dockerfile",
    ".dockerignore",
    "docker-compose.yml",
    "ci",
    "deploy",
    "docs/guide",
    "docs/runbooks",
    "docs/README.md",
    "docs/ARCHITECTURE.md",
    "docs/api",
    "scripts",
    "plugboard",
    "sdk",
    "static",
    "tests",
)
SKIPPED_FILES = {"tests/test_no_remote_ci.py"}

REMOTE_CI_TEXT = re.compile(
    r"\.gitlab-ci\.yml|\.github/workflows|GitHub Actions|GitLab CI|gitlab pipeline",
    re.IGNORECASE,
)


def _living_files():
    for entry in SCANNED:
        path = ROOT / entry
        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = sorted(path.rglob("*"))
        else:
            files = []
        for item in files:
            if not item.is_file() or item.suffix in {".lock", ".svg", ".jpg", ".png"}:
                continue
            rel = item.relative_to(ROOT).as_posix()
            if rel in SKIPPED_FILES or "__pycache__" in rel or "/vendor/" in rel:
                continue
            yield item


def test_remote_ci_definitions_are_removed():
    for rel in REMOTE_DEFINITIONS:
        assert not (ROOT / rel).exists(), f"{rel} must not be in the repository"


def test_local_ci_pipeline_is_the_definition():
    assert (ROOT / "ci/pipeline.json").is_file()


def test_living_files_do_not_cite_a_remote_pipeline():
    offenders = []
    for item in _living_files():
        text = item.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if REMOTE_CI_TEXT.search(line):
                offenders.append(f"{item.relative_to(ROOT).as_posix()}:{number}")
    assert offenders == []
