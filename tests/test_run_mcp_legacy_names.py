"""``scripts/run_mcp.py`` still accepts the variable names from before the rename.

Each case starts the launcher in a child process, from a scratch copy of the
script whose ``.env`` the test writes: the suite's environment, modules and the
repository's own ``.env`` are never touched. ``FastMCP.run`` is replaced by a
probe that prints what the server module read, so nothing listens.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

ROOT = Path(__file__).resolve().parents[1]
LEGACY = "LLM_" + "ENABLER_"
TOKEN = "synthetic-mcp-token-legacy-names-0123456789"

PROBE = """
import json, os, runpy, sys
from mcp.server.fastmcp import FastMCP

script, legacy, *flags = sys.argv[1:]

def probe(self, transport="stdio", mount_path=None):
    server = sys.modules["plugboard.mcp_server"]
    print(json.dumps({
        "transport": transport,
        "url": server._BASE_URL,
        "api_key": server._API_KEY,
        "admin_key": server._ADMIN_KEY,
        "timeout": server._TIMEOUT,
        "legacy_left": sorted(k for k in os.environ if k.startswith(legacy)),
    }))

FastMCP.run = probe
sys.argv = [script, *flags]
runpy.run_path(script, run_name="__main__")
"""


def _launch(tmp_path: Path, *flags: str, env=None, dotenv: str = "") -> dict:
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "run_mcp.py", tree / "scripts" / "run_mcp.py")
    (tree / ".env").write_text(dotenv, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(tree / "scripts" / "run_mcp.py"), LEGACY]
        + list(flags),
        cwd=tree,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path / "home"),
            "PYTHONPATH": str(ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PLUGBOARD_SKIP_DOTENV": "1",
            "PLUGBOARD_DATA": str(tmp_path / "data"),
            **(env or {}),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_old_names_in_the_environment_reach_the_server(tmp_path):
    seen = _launch(
        tmp_path,
        env={
            LEGACY + "URL": "http://old-name.invalid:9000",
            LEGACY + "API_KEY": "synthetic-old-client-key",
            LEGACY + "ADMIN_KEY": "synthetic-old-admin-key",
            LEGACY + "MCP_TIMEOUT": "42",
        },
    )

    assert seen["transport"] == "stdio"
    assert seen["url"] == "http://old-name.invalid:9000"
    assert seen["api_key"] == "synthetic-old-client-key"
    assert seen["admin_key"] == "synthetic-old-admin-key"
    assert seen["timeout"] == 42


def test_old_names_in_dotenv_reach_the_server(tmp_path):
    seen = _launch(
        tmp_path,
        dotenv=(
            f"{LEGACY}URL=http://from-dotenv.invalid:9001\n"
            f"{LEGACY}API_KEY=synthetic-dotenv-client-key\n"
        ),
    )

    assert seen["url"] == "http://from-dotenv.invalid:9001"
    assert seen["api_key"] == "synthetic-dotenv-client-key"


def test_new_name_wins_over_the_old_one(tmp_path):
    seen = _launch(
        tmp_path,
        env={
            "PLUGBOARD_URL": "http://new-name.invalid:9002",
            LEGACY + "URL": "http://old-name.invalid:9000",
        },
    )

    assert seen["url"] == "http://new-name.invalid:9002"


def test_new_name_in_dotenv_wins_over_an_old_name_in_the_environment(tmp_path):
    seen = _launch(
        tmp_path,
        env={LEGACY + "API_KEY": "synthetic-old-client-key"},
        dotenv="PLUGBOARD_API_KEY=synthetic-new-client-key\n",
    )

    assert seen["api_key"] == "synthetic-new-client-key"


def test_unprefixed_server_keys_are_never_borrowed(tmp_path):
    seen = _launch(
        tmp_path,
        env={"API_KEY": "synthetic-server-key", "ADMIN_KEY": "synthetic-server-admin"},
    )

    assert seen["api_key"] == "" and seen["admin_key"] == ""


@pytest.mark.parametrize("flag", ["--sse", "--http"])
def test_network_mode_keeps_old_names_of_kept_variables_only(tmp_path, flag):
    seen = _launch(
        tmp_path,
        flag,
        env={
            LEGACY + "MCP_TOKEN": TOKEN,
            LEGACY + "URL": "http://old-name.invalid:9000",
            LEGACY + "API_KEY": "synthetic-old-client-key",
            LEGACY + "ADMIN_KEY": "synthetic-old-admin-key",
        },
    )

    assert seen["transport"] in {"sse", "streamable-http"}
    assert seen["url"] == "http://old-name.invalid:9000"
    assert seen["api_key"] == "synthetic-old-client-key"
    assert seen["admin_key"] == ""
    assert seen["legacy_left"] == []
