"""The documentation must not show `allowed_models` examples that match no model.

Entries are compared as whole strings (`plugboard.model_identity`): a provider
prefix (`"ollama:"`) or a wildcard (`"ollama:*"`) matches no model.

The guide also says which spellings of a model an entry may use: the bare
model name, `<provider-id>:<model>`, and the full ID `/v1/models` lists, where
the CLI providers appear under a display prefix (`Codex:gpt-6.1-sol`); the
prefix alone (`Codex:`) allows nothing. Each of those sentences is checked
against the real key guard, so a change of the rule cannot leave them behind.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from plugboard.app import _model_route_guard
from tests.conftest import _setup_providers

ROOT = Path(__file__).resolve().parent.parent
_GUIDE = ROOT / "docs" / "guide" / "clients-and-keys.md"
_EXAMPLE = re.compile(r'"allowed_models"\s*:\s*(\[[^\]]*\])')


def _cli(provider_id, kind, model):
    return {
        "id": provider_id,
        "name": provider_id,
        "base_url": "",
        "bearer_token": "",
        "auth_source": f"subprocess:{kind}",
        "default_model": model,
        "models": [{"id": model}],
        "is_active": True,
        "created_at": "2026-01-01T00:00:00+00:00",
    }


_CLAUDE_CLI = _cli("claude-via-claude-cli", "claude", "sonnet")
_CODEX_CLI = _cli("chatgpt-via-codex-cli", "codex", "gpt-6.1-sol")
_GEMINI_CLI = _cli("google-gemini-via-gemini-cli", "gemini", "gemini-4-pro")
# Each CLI provider with the display prefix /v1/models lists it under.
_DISPLAYED = [
    (_CLAUDE_CLI, "claude-code"),
    (_CODEX_CLI, "Codex"),
    (_GEMINI_CLI, "Gemini"),
]

# What the Allowed models section says an entry does for a CLI model:
# (sentence of the guide, spelling of the entry, whether it allows the model).
# The spelling is built from the prefix and native name of each CLI above.
_CLAIMS = [
    ("| bare model name | `gpt-6.1-sol` |", "{native}", True),
    (
        "| `<provider-id>:<model>` | `chatgpt-via-codex-cli:gpt-6.1-sol` |",
        "{owner}:{native}",
        True,
    ),
    (
        "| full ID listed by `/v1/models` | `Codex:gpt-6.1-sol` |",
        "{alias}:{native}",
        True,
    ),
    (
        "An entry equal to the full ID it lists for a model allows that model",
        "{alias}:{native}",
        True,
    ),
    ("the prefix alone (`Codex:`) allows nothing", "{alias}:", False),
    # Said in 4.5.0, when the published ID was refused: it must not come back
    # while the guard accepts that ID.
    ("an entry written with one does not allow that model", "{alias}:{native}", False),
]


def _examples():
    files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for found in _EXAMPLE.finditer(text):
            line = text.count("\n", 0, found.start()) + 1
            yield f"{path.relative_to(ROOT)}:{line}", json.loads(found.group(1))


def _allowed_models_section():
    text = _GUIDE.read_text(encoding="utf-8")
    start = text.index("### Allowed models")
    return " ".join(text[start : text.index("\n## ", start)].split())


def _guard_allows(entry, provider, native):
    """Ask the real key guard whether a key listing only `entry` may use it."""
    guard = _model_route_guard("c", {"client_id": "c", "allowed_models": [entry]})
    try:
        guard(provider, native)
    except HTTPException as exc:
        assert exc.status_code == 403
        return False
    return True


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


@pytest.mark.parametrize(
    "sentence, spelling, allows",
    _CLAIMS,
    ids=[
        "bare-name-row",
        "owner-qualified-row",
        "published-id-row",
        "published-id-sentence",
        "prefix-alone",
        "old-refusal-gone",
    ],
)
def test_guide_sentence_on_entry_spellings_matches_the_guard(
    sentence, spelling, allows, tmp_path, monkeypatch
):
    """The guide says the sentence exactly when the guard does what it says.

    The guard is asked for every CLI provider registered together, as on a real
    install, so the published IDs are computed against the full registry.
    """
    _setup_providers(tmp_path, monkeypatch, [p for p, _ in _DISPLAYED])
    does = {}
    for provider, alias in _DISPLAYED:
        native = provider["default_model"]
        entry = spelling.format(owner=provider["id"], alias=alias, native=native)
        does[entry] = _guard_allows(entry, provider, native) == allows
        if spelling == "{alias}:":
            # A CLI without a default model resolves a bare `Codex:` request
            # to an empty model name: the prefix alone still allows nothing.
            does[entry + " (empty model)"] = (
                _guard_allows(entry, provider, "") == allows
            )
    failing = [entry for entry, ok in does.items() if not ok]
    holds = not failing
    says = sentence in _allowed_models_section()
    assert says == holds, (
        "docs/guide/clients-and-keys.md (Allowed models) "
        + ("says" if says else "does not say")
        + f" {sentence!r}, but the key guard "
        + (f"contradicts it for {', '.join(failing)}" if failing else "agrees")
        + ": rewrite the section to match docs/model-access-correction.md"
    )
