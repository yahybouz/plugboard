"""The cages' relays a test started, found and ended (BUG-157).

A relay (``plugboard.net_cage``) is the gateway's Python running the relay's
code, its egress socket on its command line. A test finds the relays it
started by a folder of its own (its ``tmp_path``) on that command line, and
ends whatever is left in a fixture, so that a regression never leaves a relay
behind on the test host.
"""

from __future__ import annotations

import os
import signal
import time
from typing import List

from plugboard import net_cage


def relays_under(marker: str) -> List[int]:
    """The live (not zombie) relays whose command line holds ``marker``."""
    found: List[int] = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read()
            with open(f"/proc/{pid}/stat", "rb") as f:
                state = f.read().rsplit(b")", 1)[1].split()[0]
        except OSError:
            continue
        if (
            net_cage.RELAY_ERROR_PREFIX.encode() in cmdline
            and marker.encode() in cmdline
            and state != b"Z"
        ):
            found.append(int(pid))
    return found


def wait_for_no_relay(marker: str, timeout_s: float = 5.0) -> List[int]:
    """The relays still alive under ``marker`` after ``timeout_s`` at most."""
    deadline = time.monotonic() + timeout_s
    while True:
        left = relays_under(marker)
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.1)


def kill_relays_under(marker: str) -> None:
    """End every relay left under ``marker`` (a test's own folder only)."""
    for pid in relays_under(marker):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
