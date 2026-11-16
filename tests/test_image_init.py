"""An init is PID 1 of the gateway's container: tini (BUG-113, BUG-123).

Without an init, the gateway (``python run.py``) is PID 1 of its container,
and every orphan handed to it (the ``bwrap`` of codex's sandbox, ``git``, a
``bash`` left by a CLI...) stays a zombie: 68 in one hour on the UAT with
4.14.2, a ``bash`` after the load test on 15/11, until the container's
process limit forbids any fork. The image now starts Debian's ``tini`` as
PID 1 (``ENTRYPOINT``, in the ``base`` stage, so ``api`` and ``full`` alike,
whatever the Compose file): it reaps every orphan and passes ``SIGTERM`` on to
the gateway, which stops its CLIs itself. No ``-g``: the gateway already
kills the process group of each CLI it starts (``start_new_session``), and a
signal to the whole group would hit the CLIs before the gateway can end their
calls.

Docker is not run here: the CI's ``docker-build`` step builds the image and
checks, in a container of it, that PID 1 is tini and that an orphan does not
stay a zombie.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
TINI = "/usr/bin/tini"
PID1_CHECK = (
    'sh -c \'pid1=$(cat /proc/1/comm); echo "PID 1: $pid1"; test "$pid1" = tini\''
)


def _stages() -> dict:
    parts = re.split(r"(?m)^FROM .* AS (\w+)\s*$", DOCKERFILE)
    return dict(zip(parts[1::2], parts[2::2]))


def _runs(stage: str) -> list:
    joined = re.sub(r"\\\n\s*", " ", stage)
    return [line for line in joined.splitlines() if line.startswith("RUN ")]


def _verifiers() -> list:
    pipeline = json.loads((ROOT / "ci/pipeline.json").read_text(encoding="utf-8"))
    argv = next(s for s in pipeline["steps"] if s["id"] == "docker-build")["argv"]
    return [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == "--verifier"]


def test_the_base_stage_installs_tini_from_debian_and_checks_it():
    (run,) = [r for r in _runs(_stages()["base"]) if " tini" in r]

    assert re.search(r"apt-get install -y --no-install-recommends tini(?: |$)", run)
    # The build fails when the binary is not where the ENTRYPOINT names it.
    assert re.search(rf"&& {re.escape(TINI)} --version(?: |$)", run), run
    for swallow in ("||", "|", "; true", "exit 0", "set +e", "2>/dev/null"):
        assert swallow not in run, swallow


def test_tini_is_the_entrypoint_of_every_target():
    """Set once, in ``base``: ``api`` and ``full`` inherit it."""
    entrypoints = re.findall(r"(?m)^ENTRYPOINT .*$", DOCKERFILE)

    assert entrypoints == [f'ENTRYPOINT ["{TINI}", "--"]']
    assert entrypoints[0] in _stages()["base"]
    for target in ("api", "full"):
        assert "ENTRYPOINT" not in _stages()[target], target


def test_tini_signals_the_gateway_only():
    """No ``-g`` (nor ``TINI_KILL_PROCESS_GROUP``): SIGTERM reaches the
    gateway, which ends its CLIs' process groups itself."""
    assert "-g" not in re.search(r"(?m)^ENTRYPOINT (.*)$", DOCKERFILE).group(1)
    assert "TINI_KILL_PROCESS_GROUP" not in DOCKERFILE


def test_the_command_is_unchanged():
    assert re.findall(r"(?m)^CMD .*$", DOCKERFILE) == ['CMD ["python", "run.py"]']


def test_the_ci_checks_that_pid_1_is_tini():
    assert PID1_CHECK in _verifiers()
    words = shlex.split(PID1_CHECK)
    assert words[:2] == ["sh", "-c"] and len(words) == 3


def test_the_ci_checks_that_an_orphan_is_reaped():
    """In a container of the image, an orphan (``sleep`` left by a shell
    that exits) is handed to PID 1; with tini it is reaped, with the gateway
    as PID 1 it would stay a zombie (the check would then exit 1)."""
    (check,) = [c for c in _verifiers() if "zombies" in c]
    words = shlex.split(check)

    assert words[:2] == ["python", "-c"] and len(words) == 3
    program = words[2]
    compile(program, "<zombie check>", "exec")
    assert '"sleep 1 &"' in program
    assert "raise SystemExit(1 if z else 0)" in program


def test_the_runbooks_say_how_to_see_pid_1_and_the_zombies():
    for doc in ("docs/runbooks/production-instance.md", "deploy/uat/README.md"):
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "/usr/bin/tini -- python run.py" in text, doc
        assert "/proc/1/cmdline" in text, doc
        assert "/proc/[0-9]*/status | wc -l" in text, doc


# -- The runbooks' PID 1 check accepts docker-init in front of tini ----------
#
# Review B3 of 4.14.4: with ``init: true`` in a Compose file, Docker's own
# init (``/sbin/docker-init``, a tini build: ``tini version 0.19.0`` on the
# VPS) is PID 1 and the image's tini its direct child. "PID 1 is not tini:
# go back" would then roll back a sound switch. The check reads ``/proc``
# only (the slim image may have no ``ps``): PID 1's ``comm`` is ``tini``, or
# ``docker-init`` with a ``tini`` among its children (``/proc/*/stat``: the
# fourth field is the parent). Zombies: 0 in both cases (the zombie count is
# unchanged).

_RUNBOOKS = (
    "docs/runbooks/prod-gate.md",
    "docs/runbooks/production-instance.md",
    "deploy/uat/README.md",
)


def _pid1_script(doc: str) -> str:
    text = (ROOT / doc).read_text(encoding="utf-8")
    found = re.findall(r"sh -c '(p1=\$\(cat /proc/1/comm\).*?)'", text, re.S)
    assert found, f"{doc}: no PID 1 check"
    assert len(set(found)) == 1, doc
    return found[0]


def test_the_runbooks_share_one_pid_1_check():
    scripts = {doc: _pid1_script(doc) for doc in _RUNBOOKS}
    assert len(set(scripts.values())) == 1, scripts
    for doc in _RUNBOOKS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        assert "docker-init" in text, doc
        assert "/sbin/docker-init -- /usr/bin/tini -- python run.py" in text, doc
    gate = (ROOT / "docs/runbooks/prod-gate.md").read_text(encoding="utf-8")
    assert "PID 1 is not tini," not in gate


def _fake_proc(root: Path, processes) -> Path:
    """``processes``: (pid, comm, ppid), as /proc shows them."""
    proc = root / "proc"
    for pid, comm, ppid in processes:
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "comm").write_text(comm + "\n")
        (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} 0 0\n")
    return proc


@pytest.mark.parametrize(
    ("processes", "ok"),
    [
        ([(1, "tini", 0), (7, "python", 1)], True),
        ([(1, "docker-init", 0), (7, "tini", 1), (8, "python", 7)], True),
        ([(1, "python", 0), (7, "bash", 1)], False),
        ([(1, "docker-init", 0), (7, "python", 1)], False),
        # tini deeper than a direct child does not count.
        ([(1, "docker-init", 0), (7, "sh", 1), (8, "tini", 7)], False),
    ],
    ids=["tini", "docker-init+tini", "no-init", "docker-init-alone", "tini-deeper"],
)
def test_the_pid_1_check_runs(tmp_path, processes, ok):
    import subprocess

    proc = _fake_proc(tmp_path, processes)
    script = _pid1_script("docs/runbooks/prod-gate.md").replace("/proc", str(proc))
    done = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, timeout=10
    )
    assert (done.returncode == 0) is ok, done.stdout + done.stderr
    if not ok:
        assert "STOP" in done.stdout
