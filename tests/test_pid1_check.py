"""BUG-132: the PID 1 check reads the binary, not the process name.

Review B-1 of 4.14.4: the runbooks' check (BUG-113, BUG-123) trusted
``/proc/1/comm`` and read the parent in ``/proc/<pid>/stat`` from the first
space, not after the last ``)``. A process that names itself ``tini`` (any
program can, with ``prctl``) passed, and a name holding ``) S 1`` shifted the
fields. The check is now one versioned script, ``deploy/check-pid1.py``, run
in the container (``python - < deploy/check-pid1.py``) by the three docs:

- PID 1's binary (``readlink /proc/1/exe``) is ``/usr/bin/tini``: pass;
- or ``/sbin/docker-init`` (``init: true``) with a direct child whose binary
  is ``/usr/bin/tini``: pass;
- anything else (python or uvicorn as PID 1: tini bypassed by an
  ``entrypoint:``), or a binary that cannot be read: ``STOP``, exit 1.

Each test builds a fake ``/proc`` and passes its root as the argument.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "check-pid1.py"
TINI = "/usr/bin/tini"
DOCKER_INIT = "/sbin/docker-init"
PYTHON = "/usr/local/bin/python3.12"
RUNBOOKS = (
    "docs/runbooks/prod-gate.md",
    "docs/runbooks/production-instance.md",
    "deploy/uat/README.md",
)


def _fake_proc(root: Path, processes) -> Path:
    """``processes``: (pid, comm, ppid, exe); exe None = unreadable."""
    proc = root / "proc"
    for pid, comm, ppid, exe in processes:
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "comm").write_text(comm + "\n")
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560\n")
        if exe is not None:
            (d / "exe").symlink_to(exe)
    # Entries that are not processes are skipped.
    (proc / "self").mkdir()
    (proc / "meminfo").write_text("MemTotal: 1 kB\n")
    return proc


def _run(proc: Path):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(proc)],
        capture_output=True,
        text=True,
        timeout=20,
    )


CASES = {
    "tini": ([(1, "tini", 0, TINI), (7, "python", 1, PYTHON)], True),
    "docker-init+tini": (
        [
            (1, "docker-init", 0, DOCKER_INIT),
            (7, "tini", 1, TINI),
            (8, "python", 7, PYTHON),
        ],
        True,
    ),
    # tini bypassed: an entrypoint: makes the gateway PID 1.
    "python-pid1": ([(1, "python", 0, PYTHON), (7, "bash", 1, "/usr/bin/bash")], False),
    "uvicorn-pid1": ([(1, "uvicorn", 0, PYTHON)], False),
    # A process that only calls itself tini.
    "named-tini": ([(1, "tini", 0, PYTHON), (7, "python", 1, PYTHON)], False),
    "docker-init-alone": (
        [(1, "docker-init", 0, DOCKER_INIT), (7, "python", 1, PYTHON)],
        False,
    ),
    "docker-init+named-tini": (
        [(1, "docker-init", 0, DOCKER_INIT), (7, "tini", 1, PYTHON)],
        False,
    ),
    # tini deeper than a direct child does not count.
    "tini-deeper": (
        [
            (1, "docker-init", 0, DOCKER_INIT),
            (7, "sh", 1, "/usr/bin/dash"),
            (8, "tini", 7, TINI),
        ],
        False,
    ),
    # PID 1's binary cannot be read: no silent pass.
    "unreadable": ([(1, "tini", 0, None)], False),
    "deleted-binary": ([(1, "tini", 0, TINI + " (deleted)")], False),
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_pid_1_check(tmp_path, case):
    processes, ok = CASES[case]
    done = _run(_fake_proc(tmp_path, processes))
    assert (done.returncode == 0) is ok, done.stdout + done.stderr
    if ok:
        assert "STOP" not in done.stdout
    else:
        assert done.returncode == 1
        assert "STOP" in done.stdout


def test_docker_init_names_tini_child(tmp_path):
    processes, _ = CASES["docker-init+tini"]
    done = _run(_fake_proc(tmp_path, processes))
    assert f"PID 1: {DOCKER_INIT}" in done.stdout
    assert "tini under it: pid 7" in done.stdout


def test_stat_is_read_after_the_last_parenthesis(tmp_path):
    """A child whose name holds ``) S 1`` is not taken for a child of PID 1:
    its parent is the field after the last ``)``, here 5."""
    proc = _fake_proc(tmp_path, [(1, "docker-init", 0, DOCKER_INIT)])
    d = proc / "9"
    d.mkdir()
    (d / "comm").write_text("x) S 1\n")
    (d / "stat").write_text("9 (x) S 1) S 5 9 9 0 -1 4194560\n")
    (d / "exe").symlink_to(TINI)
    done = _run(proc)
    assert done.returncode == 1, done.stdout
    assert "STOP" in done.stdout


def test_a_process_vanishing_mid_scan_is_skipped(tmp_path):
    processes, _ = CASES["docker-init+tini"]
    proc = _fake_proc(tmp_path, processes)
    (proc / "12").mkdir()  # no stat: the process ended during the scan
    done = _run(proc)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_default_root_is_proc():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"""else\s+["']/proc["']""", text)


@pytest.mark.parametrize("doc", RUNBOOKS)
def test_the_runbooks_run_the_versioned_script(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert "python - < deploy/check-pid1.py" in text, doc
    # The name-based check is gone.
    assert "cat /proc/1/comm" not in text, doc
    assert '"(tini)"' not in text, doc
    assert "/sbin/docker-init -- /usr/bin/tini -- python run.py" in text, doc
    assert "/proc/[0-9]*/status | wc -l" in text, doc
