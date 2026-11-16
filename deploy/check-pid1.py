"""Check that PID 1 of the gateway's container is an init (BUG-113, BUG-132).

Run in the container, from a checkout of the deployed commit, without
copying anything into it:

    docker exec -i <container> python - < deploy/check-pid1.py

Pass (exit 0) when PID 1's binary is ``/usr/bin/tini`` (the image's
``ENTRYPOINT``), or ``/sbin/docker-init`` (Compose ``init: true``) with a
direct child whose binary is ``/usr/bin/tini``. Anything else prints a line
starting with ``STOP`` and exits 1: the gateway (python, uvicorn) as PID 1
means an ``entrypoint:`` bypassed tini and orphans stay zombies.

The binary is read with ``readlink /proc/<pid>/exe``, never from the process
name (``comm``), which any program can set to ``tini``. The parent is read
from ``/proc/<pid>/stat`` after the *last* ``)``: the name between the
parentheses may itself hold ``) S 1``. A binary that cannot be read (another
user's process) is a STOP, not a pass: run again with ``docker exec -u 0``.

Only the standard library; ``/proc`` only (the slim image may have no
``ps``). An optional argument replaces ``/proc`` (tests use a fake tree).
"""

from __future__ import annotations

import os
import sys

TINI = "/usr/bin/tini"
DOCKER_INIT = "/sbin/docker-init"


def _exe(proc: str, pid: str) -> str | None:
    try:
        return os.readlink(os.path.join(proc, pid, "exe"))
    except OSError:
        return None


def _ppid(proc: str, pid: str) -> int | None:
    try:
        with open(os.path.join(proc, pid, "stat"), encoding="utf-8") as f:
            stat = f.read()
    except OSError:
        return None  # the process ended during the scan
    end = stat.rfind(")")
    fields = stat[end + 1 :].split() if end >= 0 else []
    # After the last ")": state, ppid, ...
    if len(fields) < 2 or not fields[1].lstrip("-").isdigit():
        return None
    return int(fields[1])


def check(proc: str) -> tuple[bool, list[str]]:
    lines: list[str] = []
    first = _exe(proc, "1")
    if first is None:
        return False, [
            "STOP: cannot read the binary of PID 1 (/proc/1/exe); "
            "run again as root: docker exec -u 0 -i <container> python -"
        ]
    lines.append(f"PID 1: {first}")
    if first == TINI:
        return True, lines
    if first != DOCKER_INIT:
        lines.append(
            "STOP: PID 1 is neither tini nor docker-init "
            "(an entrypoint: bypassed tini?)"
        )
        return False, lines
    unreadable = 0
    for pid in sorted(os.listdir(proc), key=lambda p: (len(p), p)):
        if not pid.isdigit() or pid == "1" or _ppid(proc, pid) != 1:
            continue
        exe = _exe(proc, pid)
        if exe is None:
            unreadable += 1
        elif exe == TINI:
            lines.append(f"tini under it: pid {pid}")
            return True, lines
    reason = "STOP: docker-init without tini as its direct child"
    if unreadable:
        reason += f" ({unreadable} child binaries unreadable: run as root)"
    lines.append(reason)
    return False, lines


def main(argv: list[str]) -> int:
    proc = argv[1] if len(argv) > 1 else "/proc"
    ok, lines = check(proc)
    for line in lines:
        print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
