"""Temporary directories made by the gateway and by the suite are removed at exit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(code: str, tmp_path: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k != "PLUGBOARD_DATA"}
    env["TMPDIR"] = str(tmp_path)
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return out.stdout.strip().splitlines()[-1]


def test_spawn_dir_is_removed_when_the_process_exits(tmp_path):
    path = _run(
        "from plugboard import cli_subprocess as c; p = c._clean_spawn_cwd(); print(p)",
        tmp_path,
    )
    assert Path(path).name.startswith("plugboard-spawn-")
    assert not Path(path).exists()


def test_suite_data_dir_is_removed_when_the_run_ends(tmp_path):
    path = _run(
        "import tests.conftest, os; print(os.environ['PLUGBOARD_DATA'])", tmp_path
    )
    assert Path(path).name.startswith("plugboard-tests-")
    assert not Path(path).exists()
