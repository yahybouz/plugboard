"""Pytest fixtures shared across the live-test framework.

Loads catalog.yaml + eval_config.yaml once per session. Framework unit tests
run on normal ``pytest`` invocations. Tests calling real external services
must use ``@pytest.mark.live`` (or module-level ``pytestmark``); the shared
tests/conftest.py requires ``PLUGBOARD_RUN_LIVE_TESTS=1`` for that marker.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CATALOG_PATH = Path(__file__).parent / "catalog.yaml"
_EVAL_CONFIG_PATH = Path(__file__).parent / "eval_config.yaml"


@pytest.fixture(scope="session")
def catalog() -> dict:
    """Load the model ground-truth catalog."""
    with _CATALOG_PATH.open() as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="session")
def eval_config() -> dict:
    """Load the evaluation thresholds + alerting policy."""
    with _EVAL_CONFIG_PATH.open() as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="session")
def model_ids(catalog) -> list:
    return [m["id"] for m in catalog["models"]]
