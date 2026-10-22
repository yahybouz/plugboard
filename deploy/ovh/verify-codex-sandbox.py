"""Offline preflight; run with compose exec -T gateway python - < this file.

No model request, token, prompt, or provider API is used. A positive sandbox
read is mandatory before negative tests, so a broken sandbox cannot pass.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

from plugboard.cli_subprocess import build_child_env, codex_isolation_args


def main() -> None:
    """Verify the deployed Linux sandbox without consuming an LLM quota."""
    assert os.getuid() == 1000, "The gateway must remain unprivileged"
    status = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status[key] = value.strip()
    assert status["NoNewPrivs"] == "1"
    assert int(status["CapEff"], 16) == 0
    assert status["Seccomp"] == "2"
    apparmor = Path("/proc/self/attr/current").read_text().strip()
    assert apparmor == "plugboard-codex (enforce)", apparmor
    with tempfile.TemporaryDirectory(prefix="plugboard-sandbox-probe-") as directory:
        parent = Path(directory)
        spawn = parent / "spawn"
        spawn.mkdir()
        (spawn / "allowed.txt").write_text("public-allowed-read")
        outside = parent / "outside-canary.txt"
        outside.write_text("public-outside-denial-canary")
        program = """set -eu
        test "$(cat allowed.txt)" = public-allowed-read
        printf 'CWD_READ_ALLOWED\n'
        for path in "$1" /etc/hostname /app/data/.deployment-proof; do
          if cat "$path" >/dev/null 2>&1; then
            printf 'OUTSIDE_READ_LEAKED\n'; exit 1
          fi
          printf 'OUTSIDE_READ_DENIED\n'
        done
        if touch forbidden-write 2>/dev/null; then
          printf 'CWD_WRITE_LEAKED\n'; exit 1
        fi
        printf 'CWD_WRITE_DENIED\n'
        """
        command = [
            "codex",
            "sandbox",
            *codex_isolation_args(str(spawn)),
            "--",
            "/bin/sh",
            "-c",
            program,
            "probe",
            str(outside),
        ]
        result = subprocess.run(
            command,
            cwd=spawn,
            env=build_child_env("codex"),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr[:1000]
        lines = result.stdout.splitlines()
        assert lines.count("CWD_READ_ALLOWED") == 1
        assert lines.count("OUTSIDE_READ_DENIED") == 3
        assert lines.count("CWD_WRITE_DENIED") == 1
    print(
        json.dumps(
            {
                "sandbox_start": "pass",
                "allowed_read": "pass",
                "outside_reads_denied": 3,
                "writes_denied": "pass",
                "uid": os.getuid(),
                "apparmor": apparmor,
                "no_new_privileges": True,
                "effective_capabilities": 0,
                "seccomp": "filter",
                "model_requests": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
