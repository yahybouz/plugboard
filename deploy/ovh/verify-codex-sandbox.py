"""Offline check of codex's Linux sandbox inside the running gateway.

Production (deploy/ovh, AppArmor profile plugboard-codex):
    docker compose -f deploy/ovh/compose.yml exec -T gateway python - < this file
UAT (deploy/uat, AppArmor profile plugboard-userns):
    docker exec -i uat-plugboard-gateway-1 python - --apparmor plugboard-userns < this file

No model request, token, prompt, or provider API is used: ``codex sandbox``
runs a small shell probe inside the sandbox codex builds for a chat call
(``codex_isolation_args``), never ``codex exec``. A positive sandbox read is
mandatory before negative tests, so a broken sandbox cannot pass.

``--apparmor`` names the profile the container must run under, in enforce
mode; without it, the production profile ``plugboard-codex`` is required.
The other checks do not change with it: uid 1000, no new privileges, no
effective capability, a seccomp filter. Prints one JSON line; exits 0 when
every check passes, 1 otherwise (the cause on standard error). The checks use
no ``assert``, so ``python -O`` cannot skip them.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional

from plugboard.cli_subprocess import build_child_env, codex_isolation_args

PRODUCTION_APPARMOR = "plugboard-codex"


class CheckFailed(Exception):
    """One check of the sandbox failed."""


@dataclass
class Confinement:
    """What confines the gateway process: its uid, ``/proc/self/status`` and
    its AppArmor label (``/proc/self/attr/current``)."""

    uid: int
    status: str
    apparmor: str


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apparmor",
        default=PRODUCTION_APPARMOR,
        help="AppArmor profile the gateway must run under, in enforce mode "
        f"(default: {PRODUCTION_APPARMOR}; the UAT's is plugboard-userns)",
    )
    return parser.parse_args(argv)


def read_confinement() -> Confinement:
    return Confinement(
        uid=os.getuid(),
        status=Path("/proc/self/status").read_text(),
        apparmor=Path("/proc/self/attr/current").read_text().strip(),
    )


def check_confinement(conf: Confinement, apparmor_profile: str) -> Dict[str, object]:
    """Check the container's confinement; return what the report shows."""
    _require(conf.uid == 1000, f"uid {conf.uid}: the gateway must remain unprivileged")
    status = {}
    for line in conf.status.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()
    for key in ("NoNewPrivs", "CapEff", "Seccomp"):
        _require(key in status, f"{key} missing from /proc/self/status")
    _require(status["NoNewPrivs"] == "1", "NoNewPrivs is not set (no-new-privileges)")
    _require(
        int(status["CapEff"], 16) == 0,
        f"CapEff {status['CapEff']}: the gateway holds capabilities",
    )
    _require(
        status["Seccomp"] == "2", f"Seccomp {status['Seccomp']}: no seccomp filter"
    )
    expected = f"{apparmor_profile} (enforce)"
    _require(
        conf.apparmor == expected,
        f"apparmor {conf.apparmor!r}, expected {expected!r}",
    )
    return {
        "uid": conf.uid,
        "apparmor": conf.apparmor,
        "no_new_privileges": True,
        "effective_capabilities": 0,
        "seccomp": "filter",
    }


_PROBE = """set -eu
test "$(cat allowed.txt)" = public-allowed-read
printf 'CWD_READ_ALLOWED\\n'
for path in "$1" /etc/hostname /app/data/.deployment-proof; do
  if cat "$path" >/dev/null 2>&1; then
    printf 'OUTSIDE_READ_LEAKED\\n'; exit 1
  fi
  printf 'OUTSIDE_READ_DENIED\\n'
done
if touch forbidden-write 2>/dev/null; then
  printf 'CWD_WRITE_LEAKED\\n'; exit 1
fi
printf 'CWD_WRITE_DENIED\\n'
"""


def probe_codex_sandbox() -> Dict[str, object]:
    """Run the probe in ``codex sandbox`` with a chat call's isolation."""
    with tempfile.TemporaryDirectory(prefix="plugboard-sandbox-probe-") as directory:
        parent = Path(directory)
        spawn = parent / "spawn"
        spawn.mkdir()
        (spawn / "allowed.txt").write_text("public-allowed-read")
        outside = parent / "outside-canary.txt"
        outside.write_text("public-outside-denial-canary")
        command = [
            "codex",
            "sandbox",
            *codex_isolation_args(str(spawn)),
            "--",
            "/bin/sh",
            "-c",
            _PROBE,
            "probe",
            str(outside),
        ]
        result = subprocess.run(
            command,
            cwd=spawn,
            env=build_child_env("codex"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        lines = result.stdout.splitlines()
        leaked = [line for line in lines if line.endswith("_LEAKED")]
        _require(not leaked, f"the sandbox let through: {', '.join(leaked)}")
        _require(
            result.returncode == 0,
            f"codex sandbox exited {result.returncode}: {result.stderr[:1000]}",
        )
        _require(lines.count("CWD_READ_ALLOWED") == 1, "the allowed read failed")
        _require(
            lines.count("OUTSIDE_READ_DENIED") == 3, "an outside read was not denied"
        )
        _require(lines.count("CWD_WRITE_DENIED") == 1, "the write was not denied")
    return {
        "sandbox_start": "pass",
        "allowed_read": "pass",
        "outside_reads_denied": 3,
        "writes_denied": "pass",
    }


def main(argv: Optional[List[str]] = None) -> int:
    """Verify the deployed Linux sandbox without consuming an LLM quota."""
    args = parse_args(argv)
    try:
        report = check_confinement(read_confinement(), args.apparmor)
        report = {**probe_codex_sandbox(), **report}
    except (CheckFailed, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"result": "fail", "model_requests": 0}))
        print(f"verify-codex-sandbox: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"result": "pass", **report, "model_requests": 0}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
