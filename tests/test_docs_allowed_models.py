"""The documentation must not show `allowed_models` examples that grant nothing.

Entries are compared as whole strings (`plugboard.model_identity`): a provider
prefix (`"ollama:"`) or a wildcard (`"ollama:*"`) matches no model.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_EXAMPLE = re.compile(r'"allowed_models"\s*:\s*(\[[^\]]*\])')


def _examples():
    files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for found in _EXAMPLE.finditer(text):
            line = text.count("\n", 0, found.start()) + 1
            yield f"{path.relative_to(ROOT)}:{line}", json.loads(found.group(1))


def test_documented_allowed_models_examples_are_full_ids():
    examples = list(_examples())
    assert any(
        where.startswith("docs/guide/clients-and-keys.md:") for where, _ in examples
    ), "the clients and keys guide no longer shows an allowed_models example"
    partial = [
        f"{where}: {entry!r}"
        for where, entries in examples
        for entry in entries
        if entry.endswith(":") or "*" in entry
    ]
    assert not partial, "prefix or wildcard entries grant nothing: " + "; ".join(
        partial
    )
