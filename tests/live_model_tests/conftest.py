"""Pytest fixtures shared across the live-test framework.

Loads catalog.yaml + eval_config.yaml once per session, exposes them as
fixtures, and skips the suite by default so it never runs on the normal
``pytest`` invocation (only when ``PLUGBOARD_RUN_LIVE_TESTS=1``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

_CATALOG_PATH = Path(__file__).parent / "catalog.yaml"
_EVAL_CONFIG_PATH = Path(__file__).parent / "eval_config.yaml"


def pytest_collection_modifyitems(config, items):
    """Skip every live-test by default unless explicitly enabled."""
    if os.environ.get("PLUGBOARD_RUN_LIVE_TESTS", "0") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="Live tests skipped — set PLUGBOARD_RUN_LIVE_TESTS=1 to enable."
    )
    for item in items:
        if "live_model_tests" in str(item.fspath):
            item.add_marker(skip_live)


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
