"""Every relative link of the documentation points at something that exists.

The Markdown files of the repository root, ``docs/``, ``sdk/`` and ``deploy/``
are scanned for inline links and images, reference definitions and HTML
``href`` / ``src`` attributes, outside code blocks and inline code. A relative
target must exist on disk; a ``#fragment`` pointing at a Markdown file must
name one of its headings (or an explicit ``<a id>``), with the anchors GitLab
generates: lowercase, every character except letters, digits, ``-``, ``_``
and spaces removed, spaces turned into hyphens, ``-1``, ``-2``... for repeats.

External links (``https:``, ``mailto:``...) are not fetched: the suite runs
without network.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1", re.M | re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_TARGETS = (
    re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)"),
    re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", re.M),
    re.compile(r"\b(?:href|src)=\"([^\"]+)\""),
)
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$", re.M)
_HTML_ANCHOR = re.compile(r"<a\s+(?:name|id)=\"([^\"]+)\"")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


def doc_files(root: Path = ROOT) -> list[Path]:
    files = [p for p in root.glob("*.md") if not p.name.endswith(".local.md")]
    for directory in ("docs", "sdk", "deploy"):
        files.extend((root / directory).rglob("*.md"))
    return sorted(files)


def _prose(path: Path) -> str:
    return _INLINE_CODE.sub("", _FENCE.sub("", path.read_text(encoding="utf-8")))


def _slug(heading: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # keep link text
    text = re.sub(r"<[^>]+>", "", text).lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = _FENCE.sub("", path.read_text(encoding="utf-8"))
    found: set[str] = set(_HTML_ANCHOR.findall(text))
    seen: dict[str, int] = {}
    for heading in _HEADING.findall(text):
        slug = _slug(heading)
        repeat = seen.get(slug, 0)
        seen[slug] = repeat + 1
        found.add(slug if repeat == 0 else f"{slug}-{repeat}")
    return found


def broken_links(path: Path) -> list[str]:
    text = _prose(path)
    broken = []
    for pattern in _TARGETS:
        for target in pattern.findall(text):
            if _SCHEME.match(target):
                continue
            file_part, _, fragment = target.partition("#")
            dest = (path.parent / unquote(file_part)) if file_part else path
            if not dest.exists():
                broken.append(f"{target} (no such file)")
            elif (
                fragment
                and dest.is_file()
                and dest.suffix == ".md"
                and unquote(fragment).lower() not in anchors(dest)
            ):
                broken.append(f"{target} (no such heading)")
    return broken


def test_checker_reports_a_missing_file_and_an_unknown_heading(tmp_path):
    (tmp_path / "guide.md").write_text(
        "# Guide\n\n## Set up (local) `make init`\n\n## Notes\n\n## Notes\n",
        encoding="utf-8",
    )
    page = tmp_path / "page.md"
    page.write_text(
        "[ok](guide.md#set-up-local-make-init) [repeat](guide.md#notes-1)\n"
        '[self](#title) <a href="guide.md">html</a> [web](https://example.com/x)\n'
        "[gone](missing.md) [bad anchor](guide.md#setup)\n"
        "[ref]: ./also-missing.md\n"
        "`[in code](nowhere.md)`\n\n"
        "```\n[in a fence](nowhere.md)\n```\n\n"
        "# Title\n",
        encoding="utf-8",
    )

    assert broken_links(page) == [
        "missing.md (no such file)",
        "guide.md#setup (no such heading)",
        "./also-missing.md (no such file)",
    ]


def test_the_scan_covers_the_guide_and_the_readme():
    names = {p.relative_to(ROOT).as_posix() for p in doc_files()}

    assert {"README.md", "docs/guide/configuration.md"} <= names


@pytest.mark.parametrize(
    "path", doc_files(), ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_relative_links_resolve(path):
    assert broken_links(path) == []
