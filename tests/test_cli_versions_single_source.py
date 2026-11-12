"""The provider CLI versions of the ``full`` image live in the ``Dockerfile`` only.

Bumping a CLI is a one-line edit of its ``ARG`` in the ``Dockerfile``. No
deployment file may assign the version again (a second copy drifts), and the
host installer ``deploy/ovh/install-host-tools.sh``, which cannot read the
``Dockerfile`` because it carries one checksum per release, must name the
same releases. The tests check the format of each version, never its value.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
HOST_TOOLS = ROOT / "deploy" / "ovh" / "install-host-tools.sh"

# ARG name -> npm package installed with it in the ``full`` stage.
CLI_VERSION_ARGS = {
    "GEMINI_CLI_VERSION": "@google/gemini-cli",
    "CLAUDE_CODE_VERSION": "@anthropic-ai/claude-code",
    "CODEX_VERSION": "@openai/codex",
}

ASSIGNMENT = re.compile(
    r"\b(" + "|".join(CLI_VERSION_ARGS) + r")\s*[:=]\s*[\"']?(\d|latest)"
)
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# Directories never scanned, at any depth.
SKIPPED_DIRS = {".git", ".venv", ".superpowers", "node_modules", "graphify-out"}
# Directories and files never scanned, at the repository root only.
SKIPPED_ROOT_ENTRIES = {"data", "tests", "CHANGELOG.md", "Dockerfile"}
MAX_SCANNED_BYTES = 1024 * 1024


def _dockerfile_args() -> dict[str, list[str]]:
    args: dict[str, list[str]] = {}
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*ARG\s+([A-Za-z0-9_]+)(?:=(\S*))?\s*$", line)
        if match:
            args.setdefault(match[1], []).append(match[2] or "")
    return args


def _dockerfile_arg(name: str) -> str:
    values = _dockerfile_args().get(name, [])
    assert len(values) == 1, (
        f"{name} must be declared exactly once in the Dockerfile: {values}"
    )
    return values[0]


def _scanned_files():
    for directory, dirnames, filenames in os.walk(ROOT):
        here = Path(directory)
        at_root = here == ROOT
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIPPED_DIRS
            and not (at_root and name in SKIPPED_ROOT_ENTRIES)
        ]
        for name in filenames:
            if name == ".git" or (at_root and name in SKIPPED_ROOT_ENTRIES):
                continue
            path = here / name
            if path.is_symlink() or path.stat().st_size > MAX_SCANNED_BYTES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            yield path, text


@pytest.mark.parametrize("name", sorted(CLI_VERSION_ARGS))
def test_dockerfile_pins_each_cli_to_an_exact_version(name):
    value = _dockerfile_arg(name)
    assert SEMVER.match(value), (
        f"{name}={value!r} must be an exact x.y.z version, never 'latest'"
    )


@pytest.mark.parametrize("name, package", sorted(CLI_VERSION_ARGS.items()))
def test_dockerfile_installs_each_cli_with_its_pinned_version(name, package):
    assert f'"{package}@${{{name}}}"' in DOCKERFILE.read_text(encoding="utf-8")


def test_assignment_pattern_catches_the_forms_a_copy_would_take():
    assert ASSIGNMENT.search('        GEMINI_CLI_VERSION: "0.65.0"')
    assert ASSIGNMENT.search("--build-arg CLAUDE_CODE_VERSION=2.1.0")
    assert ASSIGNMENT.search("CODEX_VERSION='latest'")
    assert not ASSIGNMENT.search('"@openai/codex@${CODEX_VERSION}"')
    assert not ASSIGNMENT.search("--build-arg CODEX_VERSION=<version>")


def test_no_file_outside_the_dockerfile_assigns_a_cli_version():
    offenders = []
    for path, text in _scanned_files():
        for number, line in enumerate(text.splitlines(), start=1):
            if ASSIGNMENT.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not offenders, (
        "CLI versions are pinned in the Dockerfile only:\n" + "\n".join(offenders)
    )


def test_host_tools_install_the_codex_release_of_the_image():
    releases = set(
        re.findall(r"/rust-v([^/]+)/", HOST_TOOLS.read_text(encoding="utf-8"))
    )
    assert releases == {_dockerfile_arg("CODEX_VERSION")}


def test_host_tools_install_the_claude_release_of_the_image():
    releases = re.findall(
        r'claude-native-install\.sh"\s+(\S+)', HOST_TOOLS.read_text(encoding="utf-8")
    )
    assert releases == [_dockerfile_arg("CLAUDE_CODE_VERSION")]


def test_host_tools_install_the_agy_archive_of_the_image():
    text = HOST_TOOLS.read_text(encoding="utf-8")
    url = _dockerfile_arg("AGY_URL")
    sha512 = _dockerfile_arg("AGY_SHA512")
    assert url.startswith("https://")
    assert re.fullmatch(r"[0-9a-f]{128}", sha512)
    assert f"fetch '{url}'" in text
    assert f"'{sha512}'" in text
