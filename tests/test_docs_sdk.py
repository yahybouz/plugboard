"""The documentation must say what the Python SDK is and where it comes from.

`sdk/python` is shipped in this repository; no `plugboard-sdk` package is
published on PyPI. `pip install plugboard-sdk` fails today, and would install
whatever someone else publishes under that name tomorrow. The SDK README and
the integration guide carry one status line that names the version declared
in `sdk/python/pyproject.toml`, so a release of the SDK cannot leave it behind.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SDK = ROOT / "sdk" / "python"
_DOCS = [_SDK / "README.md", ROOT / "docs" / "api" / "README.md", ROOT / "README.md"]
_STATUS = re.compile(r"^\*\*SDK status:\*\* (?P<text>.+)$", re.MULTILINE)


def _sdk_version() -> str:
    with (_SDK / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


@pytest.mark.parametrize("doc", _DOCS[:2], ids=lambda p: str(p.relative_to(ROOT)))
def test_sdk_status_line_names_the_shipped_version(doc):
    lines = _STATUS.findall(doc.read_text(encoding="utf-8"))

    assert len(lines) == 1, f"{doc}: expected one '**SDK status:**' line"
    assert f"`{_sdk_version()}`" in lines[0]
    assert "not published on PyPI" in lines[0]
    assert "pip install ./sdk/python" in lines[0]


@pytest.mark.parametrize("doc", _DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_docs_never_install_the_sdk_by_its_package_name(doc):
    text = doc.read_text(encoding="utf-8")

    assert not re.search(r"pip install [\"']?plugboard-sdk", text), doc
