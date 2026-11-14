"""Every background job is switched on by the repository's UAT and prod compose (INC-2).

A scheduler left disabled in a compose file shows on the Jobs page as
``disabled`` forever, and its data (prices, model catalogue) goes stale.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSES = ["deploy/uat/compose.yml", "deploy/ovh/compose.yml"]


def _env(path: str) -> dict:
    data = yaml.safe_load((ROOT / path).read_text())
    env = data["services"]["gateway"]["environment"]
    return {k: str(v) for k, v in env.items()}


@pytest.mark.parametrize("path", COMPOSES)
@pytest.mark.parametrize(
    "name", ["MODEL_AVAILABILITY_SCHEDULER_DISABLED", "PRICING_SCHEDULER_DISABLED"]
)
def test_schedulers_are_not_disabled(path, name):
    assert _env(path).get(name, "0") in ("0", ""), f"{path} disables {name}"


@pytest.mark.parametrize("path", COMPOSES)
def test_periodic_jobs_are_not_disabled(path):
    env = _env(path)
    for name in env:
        if name.endswith("_JOB_DISABLED") or name.endswith("RETENTION_DISABLED"):
            assert env[name] in ("0", ""), f"{path} disables {name}"


@pytest.mark.parametrize("path", COMPOSES)
def test_the_catalogue_refresh_spends_no_subscription_quota(path):
    # Model probes are one real call per model: the refresh job lists the
    # accounts' models without them (agy models), like the 4.13 catalogue sync.
    assert _env(path).get("PLUGBOARD_MODEL_REFRESH_PROBE_KINDS", "") == ""
