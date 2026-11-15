"""PLU-72: the claude CLI provider serves Claude Fable 5.2 at its official price.

Claude Fable 5.2 (``claude-fable-5-2``) is billed 8 / 40 USD per million
tokens (input / output), Claude Fable 5.1 keeps 10 / 50. Claude Code 2.1.325 is
the release the ``full`` image installs to serve it; the expected version in
the production gate runbook follows the ``Dockerfile``.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from plugboard import billing, official_prices
from plugboard.direct_providers import PRESETS

ROOT = Path(__file__).resolve().parents[1]
MONTH = "2026_11"
CLAUDE_CLI = PRESETS["claude-cli-subprocess"]
CLAUDE_PROVIDER = {
    "id": "claude-cli-subprocess",
    "preset_key": "claude-cli-subprocess",
    "auth_source": CLAUDE_CLI["auth_source"],
    "base_url": CLAUDE_CLI["base_url"],
}


@pytest.fixture(autouse=True)
def _empty_grid(tmp_path, monkeypatch):
    pricing = tmp_path / "pricing"
    pricing.mkdir()
    monkeypatch.setattr(billing, "_PRICING_DIR", pricing)


def _claude_code_version() -> str:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    versions = re.findall(r"(?m)^ARG CLAUDE_CODE_VERSION=(\S+)$", text)
    assert len(versions) == 1, versions
    return versions[0]


def test_claude_cli_catalogue_serves_fable_5_2_and_keeps_5_1():
    models = CLAUDE_CLI["known_models"]
    assert "claude-fable-5-2" in models
    assert "claude-fable-5-1" in models


def test_official_table_prices_fable_5_2_and_keeps_5_1():
    prices = official_prices.prices_on(date(2026, 11, 15))
    assert (
        prices["claude-fable-5-2"]["input_price"],
        prices["claude-fable-5-2"]["output_price"],
    ) == (8.0, 40.0)
    assert (
        prices["claude-fable-5-1"]["input_price"],
        prices["claude-fable-5-1"]["output_price"],
    ) == (10.0, 50.0)


@pytest.mark.parametrize(
    ("model", "expected_cost"),
    [
        # 1M prompt tokens + 100k completion tokens.
        ("claude-fable-5-2", 8.0 + 4.0),
        ("claude-fable-5-1", 10.0 + 5.0),
    ],
)
def test_a_claude_cli_answer_is_billed_at_the_official_price(model, expected_cost):
    match = billing.resolve_price(model, MONTH, provider=CLAUDE_PROVIDER)
    assert match is not None
    assert match.entry.get("source") == "official", match.entry
    cost = billing.calculate_cost(1_000_000, 100_000, match.entry)
    assert cost == pytest.approx(expected_cost)


def test_full_image_installs_a_claude_code_release_that_serves_fable_5_2():
    major, minor, patch = (int(p) for p in _claude_code_version().split("."))
    assert (major, minor, patch) >= (2, 1, 325)


def test_prod_gate_expects_the_claude_code_release_of_the_dockerfile():
    gate = (ROOT / "docs" / "runbooks" / "prod-gate.md").read_text(encoding="utf-8")
    expected = re.findall(r"`(\d+\.\d+\.\d+) \(Claude Code\)`", gate)
    assert expected == [_claude_code_version()]
