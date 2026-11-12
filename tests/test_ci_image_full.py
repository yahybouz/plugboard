"""The CI builds the image that is deployed, and a missing CLI fails it.

PLU-20: the UAT (uat-deployer, target ``full``) and the OVH host
(``deploy/ovh/compose.yml``) deploy the ``full`` target, so the image step
of the local CI (``ci/pipeline.json``) builds that target and starts every
CLI in it. BUG-50: a failed CLI install fails the build instead of printing
a warning. Docker itself is replaced by a fake binary here; the real build is
the CI's ``docker-build`` step.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
CLI_ARGS = ("GEMINI_CLI_VERSION", "CLAUDE_CODE_VERSION", "CODEX_VERSION")


def _full_stage() -> str:
    return re.search(r"^FROM base AS full$(.*)", DOCKERFILE, re.M | re.S).group(1)


def _image_step() -> dict:
    pipeline = json.loads((ROOT / "ci/pipeline.json").read_text(encoding="utf-8"))
    return next(s for s in pipeline["steps"] if s["id"] == "docker-build")


def _option_values(argv: list, option: str) -> list:
    return [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == option]


def test_ci_image_step_builds_the_full_target():
    argv = _image_step()["argv"]

    assert _option_values(argv, "--target") == ["full"]
    assert _option_values(argv, "--user") == ["app"]


def test_ci_image_step_starts_every_cli_of_the_image():
    argv = _image_step()["argv"]
    checks = _option_values(argv, "--verifier")

    for cli in ("gemini", "claude", "codex", "gh"):
        assert f"{cli} --version" in checks
    # agy only ships for amd64 (no official archive elsewhere).
    assert _option_values(argv, "--verifier-amd64") == ["agy --version"]


def test_gitlab_pipeline_builds_the_same_target():
    doc = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8"))
    script = "\n".join(doc["docker-build"]["script"])

    assert "--target full" in script
    assert "--target api" not in script


def test_cli_versions_are_pinned_to_the_ovh_build_args():
    compose = yaml.safe_load(
        (ROOT / "deploy/ovh/compose.yml").read_text(encoding="utf-8")
    )
    build_args = compose["services"]["gateway"]["build"]["args"]
    stage = _full_stage()

    for name in CLI_ARGS:
        default = re.search(rf"^ARG {name}=(\S+)$", stage, re.M).group(1)
        assert default != "latest", name
        assert default == build_args[name], name


def test_a_failed_cli_install_fails_the_build():
    stage = _full_stage()
    installs = [
        line for line in stage.splitlines() if line.startswith("RUN npm install -g")
    ]

    assert len(installs) == 4  # npm itself, then the three CLIs
    assert not [line for line in installs if "||" in line]
    assert "[WARN]" not in stage.replace(
        'echo "[WARN] agy: no official archive for', ""
    )


def test_the_cli_check_step_fails_on_a_missing_cli():
    stage = _full_stage()
    check = re.search(
        r"^RUN set -e; .*?Installed CLI binaries.*?\n(?!\s)", stage, re.M | re.S
    )

    assert check, "the CLI check step is missing"
    body = check.group(0)
    for cli in ("gemini", "claude", "codex", "gh", "agy"):
        assert f"{cli} --version" in body
    # A pipe or an `|| echo` would hide a failing CLI.
    assert "|" not in body
    assert "not installed" not in body


# ── ci/ci_outils.py image-docker, against a fake docker ──────────────────

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")
if argv[:2] == ["image", "inspect"]:
    fmt = argv[argv.index("--format") + 1]
    if "Config.User" in fmt:
        print("app")
    elif "Healthcheck" in fmt:
        print('["CMD","python","-c","urlopen(/health)"]')
    elif "Architecture" in fmt:
        print(os.environ.get("FAKE_ARCH", "amd64"))
    sys.exit(0)
if argv and argv[0] == "run":
    if os.environ.get("FAKE_FAIL") and os.environ["FAKE_FAIL"] in argv:
        sys.exit(127)
    sys.exit(0)
sys.exit(0)
"""


@pytest.fixture()
def fake_docker(tmp_path):
    exe = tmp_path / "docker"
    exe.write_text(FAKE_DOCKER, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.jsonl"

    def run(*extra, **env):
        full_env = dict(
            os.environ,
            CI_DOCKER=str(exe),
            FAKE_DOCKER_LOG=str(log),
            CI_COMMIT_SHA="0123456789abcdef",
            **env,
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
                "--user",
                "app",
                "--healthcheck",
                "health",
                *extra,
            ],
            cwd=ROOT,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = log.read_text() if log.exists() else ""
        calls = [json.loads(line) for line in text.splitlines()]
        log.write_text("")
        return proc, calls

    return run


def _runs(calls):
    return [c for c in calls if c and c[0] == "run"]


def test_image_docker_runs_each_check_without_network(fake_docker):
    proc, calls = fake_docker(
        "--verifier", "claude --version", "--verifier", "codex --version"
    )

    assert proc.returncode == 0, proc.stderr
    runs = _runs(calls)
    assert [r[-2:] for r in runs] == [["claude", "--version"], ["codex", "--version"]]
    for r in runs:
        assert "--rm" in r
        assert r[r.index("--network") + 1] == "none"
    # The image is always removed at the end.
    assert calls[-1][:2] == ["image", "rm"]


def test_image_docker_fails_when_a_cli_does_not_start(fake_docker):
    proc, calls = fake_docker(
        "--verifier",
        "claude --version",
        "--verifier",
        "codex --version",
        FAKE_FAIL="codex",
    )

    assert proc.returncode != 0
    assert "codex --version" in proc.stderr
    assert calls[-1][:2] == ["image", "rm"]


def test_amd64_only_checks_run_on_amd64(fake_docker):
    proc, calls = fake_docker("--verifier-amd64", "agy --version", FAKE_ARCH="amd64")

    assert proc.returncode == 0, proc.stderr
    assert [r[-2:] for r in _runs(calls)] == [["agy", "--version"]]


def test_amd64_only_checks_are_skipped_elsewhere(fake_docker):
    proc, calls = fake_docker("--verifier-amd64", "agy --version", FAKE_ARCH="arm64")

    assert proc.returncode == 0, proc.stderr
    assert _runs(calls) == []
    assert "agy --version" in proc.stdout
