"""The CI pins the executables on the image's PATH (PLU-71).

INC-4 came from one new file on the image's PATH: Debian's ``bwrap``, which
codex took instead of its own. The CI's ``docker-build`` step now lists every
executable on the PATH of the built image (as its user, with its PATH) and
compares the list with ``ci/image-path-executables.txt``: a new executable on
the PATH, or one that is gone, fails the CI until that file is updated in the
same change. The list is never written by hand from memory: while the file
holds no entry, the step prints the real list and fails, saying what to do.

Docker is replaced by a fake binary here; the real run is the CI's.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "ci/image-path-executables.txt"

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")
if argv[:2] == ["image", "inspect"]:
    print("amd64")
    sys.exit(0)
if argv and argv[0] == "run" and "PATH" in argv[-1]:
    sys.stdout.write(os.environ.get("FAKE_PATH_LIST", ""))
    sys.exit(int(os.environ.get("FAKE_PATH_CODE", "0")))
sys.exit(0)
"""


@pytest.fixture()
def run_ci(tmp_path):
    exe = tmp_path / "docker"
    exe.write_text(FAKE_DOCKER, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.jsonl"

    def run(expected_text, listed, code=0, **extra_env):
        expected = tmp_path / "expected.txt"
        args = ["--executables-path", str(expected)]
        if expected_text is not None:
            expected.write_text(expected_text, encoding="utf-8")
        env = dict(
            os.environ,
            CI_DOCKER=str(exe),
            FAKE_DOCKER_LOG=str(log),
            FAKE_PATH_LIST=listed,
            FAKE_PATH_CODE=str(code),
            CI_COMMIT_SHA="0123456789abcdef",
            **extra_env,
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "ci/ci_outils.py"),
                "image-docker",
                "--nom",
                "plugboard",
                "--target",
                "full",
                *args,
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = log.read_text() if log.exists() else ""
        calls = [json.loads(line) for line in text.splitlines()]
        log.write_text("")
        return proc, calls

    return run


LISTED = "/usr/local/bin/codex\n/usr/bin/git\n/usr/local/bin/python\n"
PINNED = "# comment\n\n/usr/bin/git\n/usr/local/bin/codex\n/usr/local/bin/python\n"


def _listing_run(calls):
    (run,) = [c for c in calls if c and c[0] == "run" and "PATH" in c[-1]]
    return run


def test_the_same_list_passes(run_ci):
    proc, calls = run_ci(PINNED, LISTED)

    assert proc.returncode == 0, proc.stderr
    assert "3 exécutables" in proc.stdout
    run = _listing_run(calls)
    # In the image, as its user (no --user), without network.
    assert run[run.index("--network") + 1] == "none"
    assert "--user" not in run and "--entrypoint" not in run
    assert run[-3:-1] == ["sh", "-c"]
    assert calls[-1][:2] == ["image", "rm"]


def test_a_new_executable_on_path_fails(run_ci):
    proc, _ = run_ci(PINNED, LISTED + "/usr/bin/bwrap\n")

    assert proc.returncode != 0
    assert "+ /usr/bin/bwrap" in proc.stderr
    assert "dans le même changement" in proc.stderr


def test_a_missing_executable_fails(run_ci):
    proc, _ = run_ci(PINNED, "/usr/bin/git\n/usr/local/bin/python\n")

    assert proc.returncode != 0
    assert "- /usr/local/bin/codex" in proc.stderr


def test_an_empty_list_prints_the_real_one_and_fails(run_ci):
    proc, _ = run_ci("# to be filled from the first CI run\n", LISTED)

    assert proc.returncode != 0
    for line in LISTED.split():
        assert line in proc.stdout
    assert "ne liste encore aucun" in proc.stderr


def test_a_listing_that_fails_fails_the_step(run_ci):
    proc, _ = run_ci(PINNED, LISTED, code=1)

    assert proc.returncode != 0


def test_a_missing_list_file_is_refused_before_any_build(run_ci):
    proc, calls = run_ci(None, LISTED)

    assert proc.returncode == 2
    assert calls == []


def test_the_listing_script_finds_executables_by_real_directory(tmp_path):
    """The shell script the step runs, here on a made-up PATH: executables
    only, a directory reached twice (a link, as /bin to /usr/bin) once."""
    sys.path.insert(0, str(ROOT / "ci"))
    try:
        import ci_outils
    finally:
        sys.path.remove(str(ROOT / "ci"))
    real = tmp_path / "usr" / "bin"
    real.mkdir(parents=True)
    (tmp_path / "bin").symlink_to(real)
    for name, mode in (("tool", 0o755), ("data", 0o644)):
        (real / name).write_text("x")
        (real / name).chmod(mode)
    (real / "subdir").mkdir()
    path = f"{tmp_path / 'bin'}:{real}:{tmp_path / 'missing'}"

    out = subprocess.run(
        ["/bin/sh", "-c", ci_outils.PATH_EXECUTABLES_SCRIPT],
        env={"PATH": path},
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert sorted(set(out.split())) == [f"{os.path.realpath(real)}/tool"]


def test_the_ci_image_step_pins_the_path():
    pipeline = json.loads((ROOT / "ci/pipeline.json").read_text(encoding="utf-8"))
    argv = next(s for s in pipeline["steps"] if s["id"] == "docker-build")["argv"]

    assert argv[argv.index("--executables-path") + 1] == EXPECTED
    assert (ROOT / EXPECTED).is_file()
