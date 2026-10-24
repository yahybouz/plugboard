"""User scope: Laya hosted locally; Jev API; Kev removed from product capabilities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError


def test_parent_public_decision_capabilities_are_only_jev_and_laya():
    from plugboard.decisions import DECISION_MODELS, DecisionRequest

    expected = {"jev", "laya", "laya-multilingual"}
    assert set(DECISION_MODELS) == expected
    assert (
        set(DecisionRequest.model_json_schema()["properties"]["model"]["enum"])
        == expected
    )


def test_parent_download_manifest_contains_only_laya_checkpoints():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "manifests/decisions.json").read_text())
    assert set(manifest["models"]) == {"laya", "laya-multilingual"}


def test_parent_removed_kev_cannot_be_submitted_as_a_decision():
    from plugboard.decisions import DecisionRequest

    with pytest.raises(ValidationError):
        DecisionRequest.model_validate(
            {
                "model": "kev-0.8b",
                "state": "Synthetic request without personal data.",
                "questions": {
                    "q": {"type": "noul", "instructions": "Is the request synthetic?"}
                },
            }
        )
