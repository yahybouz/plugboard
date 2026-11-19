"""No zombie left behind where the gateway is its container's PID 1.

On the UAT host the gateway (``python run.py``) is the PID 1 of its
container, with no init: every process orphaned in the container is handed
to it, and nothing reaps it. Without ``--as-pid-1``, bwrap starts an init of
its own as the pid 1 of the cage's pid namespace; the outer bwrap leaves as
soon as that init reports agy's exit code, without waiting for it, so the
init is orphaned and becomes a zombie of the gateway: one per agy call and
per ``agy --version`` probe (the watchdog's, every ~30 s), 68 of them on the
UAT gateway on 15/11, until the container's process limit forbids any fork.

The test runs the gateway's code in a Python child that declares itself a
child subreaper (``PR_SET_CHILD_SUBREAPER``), so orphans come to it as they
come to a container's PID 1, and leaves the pytest process as it is. The
child makes agy calls (plain and streamed) and version probes through the
host's real bwrap around a fake agy (a shell), then lists its zombie
children, reaps them itself, and reports them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_agy_cage import _SHELL_RO, _bwrap_unusable_reason, _install
from tests.test_agy_cage_regressions import realistic_gemini

ROOT = Path(__file__).resolve().parents[1]
_CID = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f60718"

# A fake agy: its version, or one answer (agy's ``json`` payload, or its
# ``stream-json`` events) after leaving a short program in the background.
_FAKE_AGY = f"""case "$1" in --version) echo 'agy 9.9.9'; exit 0 ;; esac
cat >/dev/null
sleep 0.3 </dev/null >/dev/null 2>&1 &
PAYLOAD='{{"conversation_id":"{_CID}","status":"SUCCESS","response":"OK","usage":{{}}}}'
case " $* " in
  *" stream-json "*)
    printf '{{"event":"init","conversation_id":"{_CID}","init":{{}}}}\\n'
    printf '{{"event":"step_update","step_update":{{"conversation_id":"{_CID}","step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"OK"}}}}\\n'
    printf '{{"event":"result","result":%s}}\\n' "$PAYLOAD" ;;
  *) printf '%s\\n' "$PAYLOAD" ;;
esac
"""

# Run as a child of the test: a subreaper, like a container's PID 1.
_GATEWAY_AS_PID_1 = r"""
import asyncio, ctypes, functools, json, os, sys, time

PR_SET_CHILD_SUBREAPER = 36
if ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0):
    sys.exit("prctl(PR_SET_CHILD_SUBREAPER) refused")

from plugboard import agy_cage
from plugboard import cli_subprocess as cs

p = json.loads(sys.argv[1])
cs._SPAWN_CWD_CACHE = p["spawn"]
cs._CALLS_ROOT_CACHE = p["calls"]
agy_cage.cage_argv = functools.partial(
    agy_cage.cage_argv, _extra_ro_for_tests=tuple(p["shell_ro"])
)
body = {"model": "gemini-test", "messages": [{"role": "user", "content": "hi"}]}


async def calls():
    seen = []
    for _ in range(p["rounds"]):
        plain = await cs.complete("agy", dict(body))
        pieces = []
        streamed = await cs.complete("agy", dict(body), on_delta=pieces.append)
        probe = await cs.probe_cli_version("agy")
        seen.append(
            [
                plain["choices"][0]["message"]["content"],
                streamed["choices"][0]["message"]["content"],
                "".join(pieces),
                probe["version"] if probe["ok"] else probe["error"],
            ]
        )
    return seen


def zombies():
    found = []
    me = str(os.getpid())
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/status") as fh:
                fields = dict(
                    line.split(":", 1) for line in fh.read().splitlines() if ":" in line
                )
        except OSError:
            continue
        if fields.get("PPid", "").strip() == me and fields["State"].strip()[0] == "Z":
            found.append(
                {
                    "pid": int(entry),
                    "name": fields["Name"].strip(),
                    "nspid": fields.get("NSpid", "").split(),
                }
            )
    return found


answers = asyncio.run(calls())
# Every child the code started is awaited by now: what is left is orphans.
# A short, bounded wait for the last ones to end.
deadline = time.monotonic() + p["settle_s"]
left = zombies()
while time.monotonic() < deadline:
    time.sleep(0.1)
    left = zombies()
for zombie in left:
    os.waitpid(zombie["pid"], os.WNOHANG)
# Reap whatever else came to this subreaper, so nothing is left to the host.
while True:
    try:
        if os.waitpid(-1, os.WNOHANG) == (0, 0):
            break
    except ChildProcessError:
        break
print(json.dumps({"answers": answers, "zombies": left}))
"""


@pytest.fixture
def gateway(tmp_path):
    reason = _bwrap_unusable_reason()
    if reason:
        pytest.skip(f"bwrap unusable: {reason}")
    home = tmp_path / "home"
    realistic_gemini(home)
    (home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text(
        "token\n"
    )
    spawn = tmp_path / "spawn"
    spawn.mkdir(mode=0o700)
    calls = tmp_path / "calls"
    calls.mkdir(mode=0o700)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install(bin_dir, "agy", _FAKE_AGY)
    data = tmp_path / "data"
    data.mkdir()
    script = tmp_path / "gateway.py"
    script.write_text(_GATEWAY_AS_PID_1, encoding="utf-8")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PLUGBOARD_", "LLM_ENABLER_"))
    }
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}:/usr/bin:/bin",
        PYTHONPATH=str(ROOT),
        PLUGBOARD_DATA=str(data),
        PLUGBOARD_SKIP_DOTENV="1",
    )
    return {
        "script": script,
        "env": env,
        "spawn": spawn,
        "calls": calls,
        "bin": bin_dir,
    }


def _run(gateway, rounds: int = 3, settle_s: float = 1.5) -> dict:
    params = {
        "spawn": str(gateway["spawn"]),
        "calls": str(gateway["calls"]),
        "shell_ro": list(_SHELL_RO),
        "rounds": rounds,
        "settle_s": settle_s,
    }
    proc = subprocess.run(
        [sys.executable, str(gateway["script"]), json.dumps(params)],
        env=gateway["env"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_agy_calls_and_probes_leave_no_zombie_to_a_pid_1_gateway(gateway):
    out = _run(gateway)

    assert out["answers"] == [["OK", "OK", "OK", "agy 9.9.9"]] * 3
    assert out["zombies"] == []
    assert list(gateway["spawn"].iterdir()) == []
    assert list(gateway["calls"].iterdir()) == []
