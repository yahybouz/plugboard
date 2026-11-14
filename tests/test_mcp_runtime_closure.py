"""Lot B: the MCP server runs with the MCP lock alone, and the full image has it.

``scripts/run_mcp.py`` runs in its own environment (requirements-mcp.lock: no
FastAPI, requests, tiktoken...). This test imports the server while every
package that only the API lock carries is blocked, so an import added to the
gateway side that the MCP environment lacks fails here, not at a client.
The full image installs the same lock in /opt/venv-mcp; the Dockerfile is read
here, building it is the CI's job.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

ROOT = Path(__file__).resolve().parents[1]


def _lock_names(name: str) -> set:
    out = set()
    for line in (ROOT / name).read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==", line)
        if m:
            out.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
    return out


def _api_only_modules() -> list:
    only = _lock_names("requirements.lock") - _lock_names("requirements-mcp.lock")
    blocked = []
    for mod, dists in md.packages_distributions().items():
        if all(re.sub(r"[-_.]+", "-", d).lower() in only for d in dists):
            blocked.append(mod)
    return sorted(blocked)


_BLOCK = """
import sys
BLOCKED = set({blocked!r})
class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("blocked: " + name + " is not in requirements-mcp.lock")
sys.meta_path.insert(0, _Block())
sys.path.insert(0, {root!r})
import plugboard.mcp_server as m
assert m.mcp.name == "plugboard"
print("ok")
"""


def test_mcp_server_imports_without_the_api_only_packages():
    blocked = _api_only_modules()
    assert "fastapi" in blocked  # the probe itself works
    code = _BLOCK.format(blocked=blocked, root=str(ROOT))
    out = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PLUGBOARD_SKIP_DOTENV": "1", "PATH": "/usr/bin:/bin"},
    )
    assert out.returncode == 0, out.stderr[-800:]
    assert out.stdout.strip() == "ok"


def _stage(name: str) -> str:
    text = (ROOT / "Dockerfile").read_text()
    parts = re.split(r"(?m)^FROM .* AS (\w+)\s*$", text)
    stages = dict(zip(parts[1::2], parts[2::2]))
    return stages[name]


def test_full_image_installs_the_locked_mcp_environment():
    full = _stage("full")
    assert "requirements-mcp.lock" in full
    assert "/opt/venv-mcp" in full
    assert re.search(r"--require-hashes[\s\\]+-r\s+\S*requirements-mcp\.lock", full)
    # Built image fails when the library does not import.
    assert re.search(r"/opt/venv-mcp/bin/python\s+-c\s+[\"']import mcp", full)


def test_slim_api_image_does_not_carry_the_mcp_environment():
    assert "venv-mcp" not in _stage("api")
    assert "venv-mcp" not in _stage("base")


def test_dockerignore_keeps_the_mcp_lock_in_the_context():
    for line in (ROOT / ".dockerignore").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert "requirements" not in line, line
