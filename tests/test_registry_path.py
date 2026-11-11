"""Where the MCP server reads its update registry.

The path is resolved once, when ``plugboard.mcp_server`` is imported, so the
override, default and legacy-name cases run that import in a child process
with a controlled environment: the test process keeps its own module and
environment untouched. The old in-repository location (``docs/registry.json``)
is a read-time fallback, checked in-process on the module's two paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

ROOT = Path(__file__).resolve().parents[1]
LEGACY = "LLM_" + "ENABLER_"


def _registry_path(tmp_path: Path, **env: str) -> Path:
    """``_REGISTRY_PATH`` as a fresh import computes it under ``env``."""
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "PLUGBOARD_SKIP_DOTENV": "1",
        "PLUGBOARD_DATA": str(tmp_path / "data"),
        "PYTHONDONTWRITEBYTECODE": "1",
        **env,
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import plugboard.mcp_server as m; print(m._REGISTRY_PATH)",
        ],
        cwd=ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return Path(result.stdout.strip().splitlines()[-1])


def test_default_is_registry_json_in_the_data_directory(tmp_path):
    assert _registry_path(tmp_path) == tmp_path / "data" / "registry.json"


def test_override_names_the_registry_file(tmp_path):
    custom = tmp_path / "elsewhere" / "updates.json"

    assert _registry_path(tmp_path, PLUGBOARD_REGISTRY_PATH=str(custom)) == custom


def test_override_expands_the_home_directory(tmp_path):
    path = _registry_path(tmp_path, PLUGBOARD_REGISTRY_PATH="~/updates.json")

    assert path == tmp_path / "home" / "updates.json"


def test_empty_override_falls_back_to_the_default(tmp_path):
    path = _registry_path(tmp_path, PLUGBOARD_REGISTRY_PATH="")

    assert path == tmp_path / "data" / "registry.json"


def test_pre_rename_variable_name_is_still_read(tmp_path):
    old = tmp_path / "old-name.json"

    assert _registry_path(tmp_path, **{LEGACY + "REGISTRY_PATH": str(old)}) == old


def test_new_variable_name_wins_over_the_pre_rename_one(tmp_path):
    new = tmp_path / "new-name.json"
    path = _registry_path(
        tmp_path,
        PLUGBOARD_REGISTRY_PATH=str(new),
        **{LEGACY + "REGISTRY_PATH": str(tmp_path / "old-name.json")},
    )

    assert path == new


# ── Read-time fallback to the old in-repository location ────────────────────


def _registry(version: str) -> str:
    return json.dumps({"version": version, "prompts": []})


@pytest.fixture
def registry_paths(tmp_path, monkeypatch):
    from plugboard import mcp_server

    current = tmp_path / "data" / "registry.json"
    legacy = tmp_path / "repo" / "docs" / "registry.json"
    monkeypatch.setattr(mcp_server, "_REGISTRY_PATH", current)
    monkeypatch.setattr(mcp_server, "_LEGACY_REGISTRY_PATH", legacy)
    return mcp_server, current, legacy


def test_registry_is_read_from_the_configured_path(registry_paths):
    mcp_server, current, legacy = registry_paths
    current.parent.mkdir(parents=True)
    current.write_text(_registry("current"))
    legacy.parent.mkdir(parents=True)
    legacy.write_text(_registry("legacy"))

    assert mcp_server._load_registry()["version"] == "current"


def test_old_location_is_read_when_the_configured_file_is_missing(
    registry_paths, caplog
):
    mcp_server, current, legacy = registry_paths
    legacy.parent.mkdir(parents=True)
    legacy.write_text(_registry("legacy"))

    with caplog.at_level("WARNING", logger=mcp_server.log.name):
        registry = mcp_server._load_registry()

    assert registry["version"] == "legacy"
    assert "old location" in caplog.text and str(current) in caplog.text


def test_no_registry_anywhere_is_reported_as_missing(registry_paths):
    mcp_server, current, _ = registry_paths

    registry = mcp_server._load_registry()

    assert registry["prompts"] == []
    assert registry["missing"] == str(current)
