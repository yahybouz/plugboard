"""``make init`` creates ``.env`` once and never overwrites it.

Each test runs the real Makefile target in a scratch directory holding a copy
of ``.env.example``: the repository's own ``.env`` and ``data/`` are never
touched.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None or shutil.which("python3") is None,
    reason="make init needs make and python3",
)


def _make_init(workdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "--no-print-directory", "-f", str(ROOT / "Makefile"), "init"],
        cwd=workdir,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(workdir),
            "PLUGBOARD_DATA": str(workdir / "data"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def workdir(tmp_path):
    shutil.copy2(ROOT / ".env.example", tmp_path / ".env.example")
    return tmp_path


def _values(path: Path) -> dict:
    pairs = (
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return {name.strip(): value.strip() for name, value in pairs}


def test_creates_env_with_fresh_secrets_and_owner_only_mode(workdir):
    result = _make_init(workdir)

    assert result.returncode == 0, result.stderr
    env = workdir / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    values = _values(env)
    assert len(values["ADMIN_KEY"]) == 48 and len(values["API_KEY"]) == 48
    assert values["ADMIN_KEY"] != values["API_KEY"]
    assert values["ENCRYPTION_KEY"]


def test_existing_env_is_left_byte_for_byte(workdir):
    env = workdir / ".env"
    env.write_text("ADMIN_KEY=operator-chosen\n# keep me\n", encoding="utf-8")
    env.chmod(0o640)

    result = _make_init(workdir)

    assert result.returncode == 0, result.stderr
    assert "left untouched" in result.stdout
    assert env.read_text(encoding="utf-8") == "ADMIN_KEY=operator-chosen\n# keep me\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o640


def test_existing_empty_env_is_not_filled_in(workdir):
    env = workdir / ".env"
    env.write_text("", encoding="utf-8")

    result = _make_init(workdir)

    assert result.returncode == 0, result.stderr
    assert env.read_text(encoding="utf-8") == ""


def test_second_run_keeps_the_secrets_of_the_first(workdir):
    assert _make_init(workdir).returncode == 0
    first = (workdir / ".env").read_bytes()

    result = _make_init(workdir)

    assert result.returncode == 0, result.stderr
    assert (workdir / ".env").read_bytes() == first


def test_env_symlink_target_is_not_rewritten(workdir):
    target = workdir / "shared.env"
    target.write_text("API_KEY=shared\n", encoding="utf-8")
    (workdir / ".env").symlink_to(target)

    result = _make_init(workdir)

    assert result.returncode == 0, result.stderr
    assert (workdir / ".env").is_symlink()
    assert target.read_text(encoding="utf-8") == "API_KEY=shared\n"


def test_dangling_env_symlink_is_never_written_through(workdir):
    target = workdir / "elsewhere" / "secrets.env"
    target.parent.mkdir()
    (workdir / ".env").symlink_to(target)

    _make_init(workdir)

    assert (workdir / ".env").is_symlink()
    assert not target.exists()


def test_dangling_env_symlink_is_refused_with_a_clear_message(workdir):
    (workdir / ".env").symlink_to(workdir / "missing.env")

    result = _make_init(workdir)

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert ".env is a symbolic link to a missing file" in result.stderr
