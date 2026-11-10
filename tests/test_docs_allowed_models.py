"""The documentation must not show `allowed_models` examples that match no model.

Entries are compared as whole strings (`plugboard.model_identity`): a provider
prefix (`"ollama:"`) or a wildcard (`"ollama:*"`) matches no model.

The guide also says what an entry written with a `/v1/models` display prefix
(`claude-code:`) does. That sentence is checked against the key guard, so a
change of the rule cannot leave it behind.
"""

import json
import re
from pathlib import Path

from fastapi import HTTPException

from plugboard.app import _model_route_guard
from tests.conftest import _setup_providers

ROOT = Path(__file__).resolve().parent.parent
_GUIDE = ROOT / "docs" / "guide" / "clients-and-keys.md"
_EXAMPLE = re.compile(r'"allowed_models"\s*:\s*(\[[^\]]*\])')
# The guide's sentence on entries written with a display prefix.
_DISPLAY_PREFIX_REFUSED = "an entry written with one does not allow that model"
_CLAUDE_CLI = {
    "id": "claude-via-claude-cli",
    "name": "claude-via-claude-cli",
    "base_url": "",
    "bearer_token": "",
    "auth_source": "subprocess:claude",
    "default_model": "sonnet",
    "models": [{"id": "sonnet"}],
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}


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
    assert not partial, "prefix or wildcard entries match no model: " + "; ".join(
        partial
    )


def test_guide_sentence_on_display_prefix_entries_matches_the_guard(
    tmp_path, monkeypatch
):
    _setup_providers(tmp_path, monkeypatch, [_CLAUDE_CLI])
    guard = _model_route_guard(
        "c", {"client_id": "c", "allowed_models": ["claude-code:sonnet"]}
    )
    try:
        guard(_CLAUDE_CLI, "sonnet")
        refused = False
    except HTTPException as exc:
        refused = exc.status_code == 403
    guide = " ".join(_GUIDE.read_text(encoding="utf-8").split())
    says_refused = _DISPLAY_PREFIX_REFUSED in guide
    assert says_refused == refused, (
        "docs/guide/clients-and-keys.md (Allowed models) "
        + ("says" if says_refused else "no longer says")
        + " that an entry written with a display prefix does not allow the model,"
        + " but the key guard "
        + ("refuses" if refused else "accepts")
        + " 'claude-code:sonnet' for claude-via-claude-cli:"
        + " rewrite that bullet to match docs/model-access-correction.md"
    )
