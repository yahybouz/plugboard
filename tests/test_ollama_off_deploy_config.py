"""Ollama is off by default in what the repository ships (PLU-53, lot A).

The UAT and OVH composes and the environment examples turn it off and no
longer depend on the relay (a bridge network, a URL, an embedding identity).
Nothing is removed from the code: ``OLLAMA_ENABLED=true`` brings it back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSES = [
    ROOT / "deploy" / "uat" / "compose.yml",
    ROOT / "deploy" / "ovh" / "compose.yml",
]


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", COMPOSES, ids=["uat", "ovh"])
def test_compose_turns_ollama_off_without_relay(path):
    doc = _doc(path)
    env = doc["services"]["gateway"]["environment"]
    assert env["OLLAMA_ENABLED"] == "false"
    assert "OLLAMA_URL" not in env
    assert "ollama" not in str(env.get("PLUGBOARD_EMBEDDING_MODELS", "")).lower()
    assert "ollama-private" not in (doc["services"]["gateway"].get("networks") or [])
    assert "ollama-private" not in (doc.get("networks") or {})


def test_environment_example_defaults_to_off():
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "OLLAMA_ENABLED=false" in lines
    assert "OLLAMA_ENABLED=auto" not in lines


def test_uat_runtime_secrets_no_longer_name_the_relay_token():
    text = (ROOT / "deploy" / "uat" / "runtime.env.example").read_text(encoding="utf-8")
    names = [ln.split("=")[0] for ln in text.splitlines() if "=" in ln and ln[0] != "#"]
    assert "PLUGBOARD_OLLAMA_TOKEN" not in names
