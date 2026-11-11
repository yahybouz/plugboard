"""What the Docker image ships, checked without Docker.

The base stage of the ``Dockerfile`` copies paths from the build context, and
``.dockerignore`` decides what the context holds. Both are read here, with
the matching rules Docker documents for ``.dockerignore``: patterns are
anchored at the context root and matched with Go's ``filepath.Match`` (``*``
never crosses ``/``), ``**`` spans any number of directories, leading and
trailing slashes are dropped, a pattern that matches a directory excludes its
content, and the last matching line wins (``!`` re-includes).

Building and running the image is the job of the CI (``docker-build``).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Repository paths the server opens at run time, and where the code does it.
RUNTIME_PATHS = {
    "run.py": "Dockerfile CMD",
    "VERSION": "release check (tests/test_envcompat.py)",
    "plugboard/__init__.py": "the package",
    "manifests/decisions.json": "plugboard/decision_assets.py MANIFEST",
    "static/index.html": "plugboard/app.py _STATIC_DIR",
    "scripts/decision_worker.py": "plugboard/decision_runtime.py _command",
    "scripts/rotate_encryption_key.py": "docs/guide/configuration.md",
}
SHIPPED_DIRS = ("plugboard", "manifests", "static", "scripts")


def _pattern_regex(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**", i):
            i += 2
            if pattern.startswith("/", i):
                out.append("(?:.*/)?")
                i += 1
            else:
                out.append(".*")
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            end = pattern.index("]", i + 1)
            out.append(pattern[i : end + 1])
            i = end
        elif char == "\\" and i + 1 < len(pattern):
            i += 1
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out))


def _ignore_rules() -> list[tuple[bool, re.Pattern]]:
    rules = []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        line = line.strip()
        negate = line.startswith("!")
        pattern = line[1:].strip() if negate else line
        pattern = pattern.strip("/")
        if pattern in ("", "."):
            continue
        rules.append((negate, _pattern_regex(pattern)))
    return rules


RULES = _ignore_rules()


def excluded(path: str) -> bool:
    """True when ``path`` (relative to the context root) is left out."""
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    result = False
    for negate, regex in RULES:
        if any(regex.fullmatch(candidate) for candidate in candidates):
            result = not negate
    return result


def _context_copies() -> list[str]:
    """Context sources of the COPY lines of the base stage."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    base = re.search(r"^FROM .* AS base$(.*?)^FROM ", text, re.M | re.S).group(1)
    sources = []
    for line in base.splitlines():
        words = shlex.split(line) if line.startswith("COPY ") else []
        args = [w for w in words[1:] if not w.startswith("--")]
        if any(w.startswith("--from") for w in words):
            continue
        sources.extend(a.rstrip("/") for a in args[:-1])
    return sources


def _copied(path: str, sources: list[str]) -> bool:
    return any(path == s or path.startswith(s + "/") for s in sources)


def test_matcher_follows_the_documented_dockerignore_rules():
    assert _pattern_regex("*/temp*").fullmatch("somedir/temporary.txt")
    assert not _pattern_regex("*/temp*").fullmatch("a/b/temporary.txt")
    assert _pattern_regex("temp?").fullmatch("tempa")
    assert _pattern_regex("**/*.go").fullmatch("main.go")
    assert _pattern_regex("**/*.go").fullmatch("a/b/main.go")
    assert not _pattern_regex("*.md").fullmatch("docs/guide.md")


@pytest.mark.parametrize("path", sorted(RUNTIME_PATHS))
def test_runtime_files_reach_the_image(path):
    assert (ROOT / path).is_file(), f"{path} no longer exists: update RUNTIME_PATHS"
    assert _copied(path, _context_copies()), f"{path} is not copied by the Dockerfile"
    assert not excluded(path), f"{path} is excluded by .dockerignore"


@pytest.mark.parametrize("directory", SHIPPED_DIRS)
def test_no_source_file_of_a_shipped_directory_is_ignored(directory):
    files = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / directory).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]

    assert files
    assert [f for f in files if excluded(f)] == []


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "data/providers.json",
        "logs/plugboard.log",
        "private/scan-terms.txt",
        "CLAUDE.local.md",
        "tests/conftest.py",
        "docs/guide/configuration.md",
    ],
)
def test_local_state_secrets_tests_and_docs_stay_out(path):
    assert excluded(path)


@pytest.mark.parametrize(
    "path",
    [
        "plugboard/__pycache__/app.cpython-312.pyc",
        "scripts/__pycache__/run_mcp.cpython-312.pyc",
        "plugboard/stale.pyc",
    ],
)
def test_host_bytecode_never_reaches_the_image(path):
    # COPY plugboard/ and scripts/ take whole directories: bytecode left by
    # a local run must be kept out by .dockerignore.
    assert excluded(path)


@pytest.mark.parametrize(
    "path",
    ["static/dev.pem", "scripts/local.key", "plugboard/.env", "scripts/.env.local"],
)
def test_key_material_in_a_shipped_directory_stays_out(path):
    assert excluded(path)


def test_tests_docs_and_data_are_never_copied():
    sources = _context_copies()

    assert not [s for s in sources if s.split("/")[0] in {"tests", "docs", "data"}]
