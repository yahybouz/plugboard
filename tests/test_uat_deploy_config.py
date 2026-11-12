"""The UAT deployment configuration in ``deploy/uat/`` carries no secret.

``compose.yml``, ``seccomp-plugboard-userns.json`` and ``apparmor-plugboard-userns``
are byte-for-byte copies of the files on the UAT host; ``README.md`` records their
SHA-256 so a root operator can check the host for drift. The env examples list
variable names only, and ``deployer-entry.json`` is the target state of the
deployer's entry: the CLI versions come from the ``Dockerfile``, not build args.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
UAT = ROOT / "deploy" / "uat"

FAITHFUL_COPIES = (
    "compose.yml",
    "seccomp-plugboard-userns.json",
    "apparmor-plugboard-userns",
)
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
ENV_NAME_ONLY = re.compile(r"^[A-Z][A-Z0-9_]*=$")


def _gateway() -> dict:
    compose = yaml.safe_load((UAT / "compose.yml").read_text(encoding="utf-8"))
    return compose["services"]["gateway"]


def _env_names(name: str) -> list[str]:
    names = []
    for line in (UAT / name).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        assert ENV_NAME_ONLY.match(stripped), f"{name}: {stripped!r} carries a value"
        names.append(stripped[:-1])
    return names


def _key_names(value) -> list[str]:
    if isinstance(value, dict):
        names = list(value)
        for child in value.values():
            names.extend(_key_names(child))
        return names
    if isinstance(value, list):
        return [name for child in value for name in _key_names(child)]
    return []


def test_compose_reads_its_image_from_compose_env():
    assert _gateway()["image"].startswith("${PLUGBOARD_IMAGE")


def test_compose_publishes_on_loopback_only():
    assert _gateway()["ports"] == ["127.0.0.1:18765:8765"]


def test_compose_environment_names_no_secret():
    environment = _gateway()["environment"]
    names = (
        list(environment)
        if isinstance(environment, dict)
        else [entry.split("=", 1)[0] for entry in environment]
    )
    assert names
    assert [name for name in names if SECRET_NAME.search(name)] == []


def test_compose_drops_capabilities_and_confines_the_gateway():
    gateway = _gateway()
    assert gateway["cap_drop"] == ["ALL"]
    assert (
        "seccomp=/srv/plugboard/seccomp/plugboard-userns.json"
        in gateway["security_opt"]
    )
    assert "apparmor=plugboard-userns" in gateway["security_opt"]


@pytest.mark.parametrize("name", ["compose.env.example", "runtime.env.example"])
def test_env_examples_carry_names_without_values(name):
    assert _env_names(name)


def test_compose_env_example_names_the_image():
    assert _env_names("compose.env.example") == ["PLUGBOARD_IMAGE"]


def test_runtime_env_example_names_exactly_the_four_secrets():
    assert sorted(_env_names("runtime.env.example")) == sorted(
        ["ADMIN_KEY", "API_KEY", "ENCRYPTION_KEY", "PLUGBOARD_OLLAMA_TOKEN"]
    )


def test_seccomp_profile_denies_by_default():
    profile = json.loads(
        (UAT / "seccomp-plugboard-userns.json").read_text(encoding="utf-8")
    )
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"


def test_apparmor_profile_is_named():
    text = (UAT / "apparmor-plugboard-userns").read_text(encoding="utf-8")
    assert "profile plugboard-userns" in text


def test_deployer_entry_builds_full_without_build_args():
    entry = json.loads((UAT / "deployer-entry.json").read_text(encoding="utf-8"))
    assert entry["images"][0]["target"] == "full"
    names = _key_names(entry)
    assert "build_args" not in names
    assert [name for name in names if SECRET_NAME.search(name)] == []


def test_readme_keeps_runtime_secret_values_out_of_the_repository():
    readme = (UAT / "README.md").read_text(encoding="utf-8")
    assert "secrets/runtime.env" in readme
    assert re.search(r"values .*not (?:in the repository|committed|versioned)", readme)


def _table_row_holds(readme: str, name: str, digest: str) -> bool:
    """A Markdown table row whose first cell is ``name`` holds ``digest``."""
    for line in readme.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == f"`{name}`" and f"`{digest}`" in cells[1:]:
            return True
    return False


def test_the_row_check_catches_swapped_digests():
    readme = (
        "| File | SHA-256 |\n|---|---|\n| `a.yml` | `bbb` |\n| `b.json` | `aaa` |\n"
    )
    assert _table_row_holds(readme, "a.yml", "bbb")
    assert not _table_row_holds(readme, "a.yml", "aaa")
    assert not _table_row_holds("`a.yml` and `aaa` in prose", "a.yml", "aaa")


@pytest.mark.parametrize("name", FAITHFUL_COPIES)
def test_readme_records_the_sha256_of_each_faithful_copy(name):
    digest = hashlib.sha256((UAT / name).read_bytes()).hexdigest()
    readme = (UAT / "README.md").read_text(encoding="utf-8")
    assert _table_row_holds(readme, name, digest), (
        f"README.md needs a table row for `{name}` holding its digest {digest}"
    )
