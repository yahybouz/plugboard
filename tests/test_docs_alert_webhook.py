"""The guide documents every alert ``ALERT_WEBHOOK_URL`` receives.

The payloads are read from ``plugboard/alerts.py`` with ``ast``: every dict
literal with a ``"type"`` key is an alert. Each one must have a bullet and a
JSON example with the same keys in the ``### Alert webhook`` section of
``docs/guide/monitoring.md``, and be named in the ``ALERT_WEBHOOK_URL`` row of
``docs/guide/configuration.md``, so a consumer filtering by type knows it.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_ALERTS = ROOT / "plugboard" / "alerts.py"
_MONITORING = ROOT / "docs" / "guide" / "monitoring.md"
_CONFIGURATION = ROOT / "docs" / "guide" / "configuration.md"
_JSON_BLOCK = re.compile(r"```json\n(.*?)```", re.S)


def _payloads() -> dict[str, set[str]]:
    payloads: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(_ALERTS.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        if "type" not in keys:
            continue
        value = node.values[keys.index("type")]
        assert isinstance(value, ast.Constant), "alert type must be a literal"
        payloads[value.value] = set(keys)
    return payloads


_PAYLOADS = _payloads()


def _alert_section() -> str:
    text = _MONITORING.read_text(encoding="utf-8")
    start = text.index("### Alert webhook")
    end = re.compile(r"^#{2,3} ", re.M).search(text, start + 1)
    return text[start : end.start() if end else len(text)]


def _examples() -> dict[str, set[str]]:
    examples = {}
    for block in _JSON_BLOCK.findall(_alert_section()):
        payload = json.loads(block)
        examples[payload["type"]] = set(payload)
    return examples


def test_alerts_are_found():
    assert {"provider_down", "spending_alert"} <= set(_PAYLOADS)


@pytest.mark.parametrize("alert_type", sorted(_PAYLOADS))
def test_monitoring_guide_lists_alert(alert_type):
    assert f"- `{alert_type}`" in _alert_section()


@pytest.mark.parametrize("alert_type", sorted(_PAYLOADS))
def test_monitoring_guide_shows_alert_payload(alert_type):
    assert _examples().get(alert_type) == _PAYLOADS[alert_type]


@pytest.mark.parametrize("alert_type", sorted(_PAYLOADS))
def test_configuration_guide_names_alert(alert_type):
    rows = [
        line
        for line in _CONFIGURATION.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `ALERT_WEBHOOK_URL`")
    ]
    assert len(rows) == 1
    assert f"`{alert_type}`" in rows[0]
