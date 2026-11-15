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
import shlex
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


def _runs(stage: str) -> list:
    """Each RUN instruction of a stage, its continuation lines joined."""
    joined = re.sub(r"\\\n\s*", " ", stage)
    return [line for line in joined.splitlines() if line.startswith("RUN ")]


# Ways a shell step can swallow a failure. Each one would bring BUG-50 back.
SWALLOWS = (
    "||",
    "|",
    "; true",
    "&& true",
    "exit 0",
    "set +e",
    "command -v",
    "2>/dev/null",
)


def _swallowed(run: str) -> list:
    return [s for s in SWALLOWS if s in run]


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


def test_cli_versions_are_pinned_in_the_dockerfile_only():
    # One source (PLU-29): the OVH build passes no CLI version, so the image
    # always gets the Dockerfile's pinned releases.
    compose = yaml.safe_load(
        (ROOT / "deploy/ovh/compose.yml").read_text(encoding="utf-8")
    )
    build_args = compose["services"]["gateway"]["build"].get("args") or {}
    stage = _full_stage()

    for name in CLI_ARGS:
        default = re.search(rf"^ARG {name}=(\S+)$", stage, re.M).group(1)
        assert default != "latest", name
        assert name not in build_args, name


def test_the_ovh_default_image_names_this_release():
    """A plain `docker compose up -d` on the private host must never start an
    older image: bump the default of deploy/ovh/compose.yml with VERSION."""
    compose = yaml.safe_load(
        (ROOT / "deploy/ovh/compose.yml").read_text(encoding="utf-8")
    )
    image = compose["services"]["gateway"]["image"]
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    assert image == f"${{PLUGBOARD_IMAGE:-plugboard:private-full-v{version}}}"


def test_a_failed_cli_install_fails_the_build():
    installs = [r for r in _runs(_full_stage()) if r.startswith("RUN npm install -g")]

    assert len(installs) == 4  # npm itself, then the three CLIs
    for run in installs:
        assert _swallowed(run) == [], run
    assert "[WARN]" not in _full_stage().replace(
        'echo "[WARN] agy: no official archive for', ""
    )


def test_the_cli_check_step_fails_on_a_missing_cli():
    checks = [r for r in _runs(_full_stage()) if "Installed CLI binaries" in r]

    assert len(checks) == 1, "the CLI check step is missing"
    run = checks[0]
    assert run.startswith("RUN set -e; ")
    assert _swallowed(run) == []
    assert "not installed" not in run
    # Each CLI is run as a bare command (start of a statement), so its exit
    # status reaches `set -e`; agy only under the amd64 test.
    for cli in ("gemini", "claude", "codex", "gh"):
        assert re.search(rf"(?:; |^RUN )\s*{cli} --version;", run), cli
    assert re.search(r"= \"amd64\" \]; then agy --version; fi$", run)


# ── INC-4: no bwrap on the image's PATH ─────────────────────────────────────
# codex 0.163.0 prefers a bwrap found on PATH to the one it ships
# (codex-resources/bwrap). 4.14.2's first image installed Debian's bubblewrap
# at /usr/bin/bwrap: codex took it, and it could not mount /proc in the
# container (every codex call answered 502, production and UAT, 15/11). The
# gateway's bwrap (agy's cage) lives at a fixed path off PATH instead.

# The image's PATH when the Dockerfile sets none: python:3.12-slim's
# (``/usr/local/bin`` put before Debian's default).
_BASE_IMAGE_PATH = (
    "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)
# Debian's merged /usr: /bin and /sbin are links to /usr/bin and /usr/sbin.
_MERGED_USR = {"/bin": "/usr/bin", "/sbin": "/usr/sbin"}
# Where Debian's bubblewrap package installs its command.
_DEBIAN_BWRAP = "/usr/bin/bwrap"
_SHELL_OPERATORS = {";", "&&", "||", "|", "&", "(", ")"}
_SHELL_KEYWORDS = {"if", "then", "else", "elif", "fi", "do", "done", "!", "set"}


def _merged(path: str) -> str:
    for link, target in _MERGED_USR.items():
        if path == link or path.startswith(link + "/"):
            return target + path[len(link) :]
    return path


def image_path_dirs(dockerfile: str = DOCKERFILE) -> list:
    """The directories of the image's PATH (``ENV PATH`` applied in order)."""
    path = _BASE_IMAGE_PATH
    for match in re.finditer(r"^ENV\s+PATH[= ]\s*(\S+)", dockerfile, re.M):
        value = match.group(1).strip("\"'")
        path = value.replace("${PATH}", path).replace("$PATH", path)
    return [_merged(d.rstrip("/")) for d in path.split(":") if d]


def _statements(run: str) -> list:
    """The simple commands of a RUN step, as word lists (shell quoting kept)."""
    lexer = shlex.shlex(run[len("RUN ") :], posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    statements, words = [], []
    for token in lexer:
        if token in _SHELL_OPERATORS:
            if words:
                statements.append(words)
            words = []
        elif words or token not in _SHELL_KEYWORDS:
            words.append(token)
    if words:
        statements.append(words)
    return statements


def bwrap_files_in_image(stage: str) -> set:
    """Where a ``bwrap`` file is once the stage's RUN steps are done.

    ``apt-get install ... bubblewrap`` puts Debian's at ``/usr/bin/bwrap``;
    ``mv``, ``install`` and ``cp`` move or copy it, ``rm`` removes it, and
    ``ln -s`` may name a new one.
    """
    files: set = set()
    for run in _runs(stage):
        for words in _statements(run):
            command, args = words[0], words[1:]
            operands = [_merged(a) for a in args if not a.startswith("-")]
            if command == "apt-get" and "install" in args and "bubblewrap" in args:
                files.add(_DEBIAN_BWRAP)
            elif command == "mv" and len(operands) == 2 and operands[0] in files:
                files.discard(operands[0])
                files.add(operands[1])
            elif command in ("install", "cp") and "-d" not in args:
                # install's -m/-o/-g take a value: only the last two operands
                # are the source and the destination.
                if len(operands) >= 2 and operands[-2] in files:
                    files.add(operands[-1])
            elif command == "rm":
                files.difference_update(operands)
            elif command == "ln" and operands and operands[-1].endswith("/bwrap"):
                files.add(operands[-1])
    return files


def test_the_simulation_sees_a_bwrap_left_on_path():
    """Not vacuous: 4.14.2's first image (bubblewrap installed, nothing moved)
    leaves /usr/bin/bwrap on PATH; moving it off PATH clears it."""
    first = "RUN apt-get update \\\n && apt-get install -y curl bubblewrap\n"
    moved = (
        first.rstrip("\n")
        + " \\\n && install -m 0755 /usr/bin/bwrap /opt/x/bwrap && rm /bin/bwrap\n"
    )
    assert bwrap_files_in_image(first) == {"/usr/bin/bwrap"}
    assert bwrap_files_in_image(moved) == {"/opt/x/bwrap"}
    assert "/usr/bin" in image_path_dirs("FROM x\n")


def test_no_bwrap_on_the_image_path():
    """INC-4: codex must keep its own bwrap, so the image has none on PATH;
    the gateway's (agy's cage) is at agy_cage.GATEWAY_BWRAP, off PATH."""
    from plugboard import agy_cage

    files = bwrap_files_in_image(_full_stage())
    path = image_path_dirs()
    on_path = sorted(f for f in files if os.path.dirname(f) in path)

    assert on_path == [], f"bwrap on the image's PATH {path}: {on_path}"
    assert agy_cage.GATEWAY_BWRAP in files
    assert os.path.dirname(agy_cage.GATEWAY_BWRAP) not in path


def test_the_build_fails_when_a_bwrap_is_on_path():
    """The build itself checks it (a step after every install), and starts
    the gateway's bwrap by its fixed path."""
    from plugboard import agy_cage

    runs = _runs(_full_stage())
    guards = [r for r in runs if "command -v bwrap" in r]
    assert len(guards) == 1, guards
    # An `if`, not `! command -v`: `set -e` ignores a command negated by `!`.
    assert re.search(r"if command -v bwrap; then .*exit 1; fi", guards[0])
    assert "! command -v" not in guards[0]
    # After the last step that installs anything.
    installs = [
        i for i, r in enumerate(runs) if "apt-get install" in r or "npm install" in r
    ]
    assert runs.index(guards[0]) > max(installs)
    check = next(r for r in runs if "Installed CLI binaries" in r)
    assert f"; {agy_cage.GATEWAY_BWRAP} --version;" in check
    assert re.search(r"(?:; |^RUN )\s*bwrap --version", check) is None


def test_the_ci_image_step_checks_bwrap_off_path():
    """The CI's docker-build step runs the same two checks in the built image,
    as its user and with its PATH."""
    from plugboard import agy_cage

    checks = _option_values(_image_step()["argv"], "--verifier")

    assert f"{agy_cage.GATEWAY_BWRAP} --version" in checks
    (guard,) = [c for c in checks if "command -v bwrap" in c]
    words = shlex.split(guard)
    assert words[:2] == ["sh", "-c"] and len(words) == 3
    assert re.search(r"if command -v bwrap; then .*exit 1; fi", words[2])


def test_a_shell_check_reaches_docker_run_as_one_command(fake_docker):
    """ci_outils splits a check with shlex: `sh -c '...'` reaches `docker run`
    as three words, the script whole."""
    script = "if command -v bwrap; then exit 1; fi; echo no bwrap on PATH"
    proc, calls = fake_docker("--verifier", f"sh -c '{script}'")

    assert proc.returncode == 0, proc.stderr
    assert _docker_runs(calls)[0][-3:] == ["sh", "-c", script]


def test_the_swallow_detector_sees_the_known_bypasses():
    bypasses = [
        'RUN npm install -g "@openai/codex@1" \\\n    || true',
        'RUN npm install -g "@openai/codex@1"; exit 0',
        "RUN set -e; set +e; gemini --version;",
        "RUN set -e; if command -v gemini; then gemini --version; fi",
        "RUN set -e; gemini --version 2>/dev/null | head -1;",
    ]
    for text in bypasses:
        assert _swallowed(_runs(text)[0]), text


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
if argv and argv[0] == "ps":
    print(os.environ.get("FAKE_LEFTOVER", ""))
    sys.exit(0)
if argv and argv[0] == "run":
    if os.environ.get("FAKE_SLEEP"):
        import time
        time.sleep(float(os.environ["FAKE_SLEEP"]))
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


def _docker_runs(calls):
    return [c for c in calls if c and c[0] == "run"]


def test_image_docker_runs_each_check_without_network(fake_docker):
    proc, calls = fake_docker(
        "--verifier", "claude --version", "--verifier", "codex --version"
    )

    assert proc.returncode == 0, proc.stderr
    runs = _docker_runs(calls)
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
    assert [r[-2:] for r in _docker_runs(calls)] == [["agy", "--version"]]


def test_amd64_only_checks_are_skipped_elsewhere(fake_docker):
    proc, calls = fake_docker("--verifier-amd64", "agy --version", FAKE_ARCH="arm64")

    assert proc.returncode == 0, proc.stderr
    assert _docker_runs(calls) == []
    assert "agy --version" in proc.stdout


def test_the_build_log_is_kept_and_each_check_is_labelled(fake_docker):
    proc, calls = fake_docker("--verifier", "gh --version")

    assert proc.returncode == 0, proc.stderr
    build = next(c for c in calls if c[0] == "build")
    # A failing RUN must show its cause (npm ETARGET, 429...), so no -q.
    assert "-q" not in build
    assert "--progress=plain" in build
    run = _docker_runs(calls)[0]
    assert (
        run[run.index("--label") + 1]
        == "ci.locale.image=ci-locale/plugboard:0123456789ab"
    )


def test_leftovers_of_a_killed_run_are_removed_first(fake_docker):
    proc, calls = fake_docker(FAKE_LEFTOVER="c0ffee1 c0ffee2")

    assert proc.returncode == 0, proc.stderr
    build_at = next(i for i, c in enumerate(calls) if c[0] == "build")
    before = calls[:build_at]
    assert ["rm", "-f", "c0ffee1", "c0ffee2"] in before
    assert ["image", "rm", "-f", "ci-locale/plugboard:0123456789ab"] in before
    ps = next(c for c in before if c[0] == "ps")
    assert "label=ci.locale.image=ci-locale/plugboard:0123456789ab" in ps


def test_a_check_that_hangs_is_killed_and_fails(fake_docker):
    proc, calls = fake_docker(
        "--verifier", "gemini --version", "--delai-verifier", "1", FAKE_SLEEP="5"
    )

    assert proc.returncode != 0
    assert "gemini --version" in proc.stderr
    name = _docker_runs(calls)[0][_docker_runs(calls)[0].index("--name") + 1]
    assert ["rm", "-f", name] in calls
    assert calls[-1][:2] == ["image", "rm"]


def test_an_unreadable_architecture_fails_the_amd64_checks(fake_docker):
    proc, calls = fake_docker("--verifier-amd64", "agy --version", FAKE_ARCH="")

    assert proc.returncode != 0
    assert _docker_runs(calls) == []


@pytest.mark.parametrize(
    "extra",
    [
        ("--verifier", ""),
        ("--verifier", "   "),
        ("--verifier-amd64", 'agy "--version'),
        ("--delai-verifier", "0"),
    ],
)
def test_invalid_check_options_are_refused_before_any_build(fake_docker, extra):
    proc, calls = fake_docker(*extra)

    assert proc.returncode == 2
    assert calls == []


# ── The Python SDK is tested and linted by the CI too ─────────────────────


def _step(step_id: str) -> dict:
    pipeline = json.loads((ROOT / "ci/pipeline.json").read_text(encoding="utf-8"))
    return next(s for s in pipeline["steps"] if s["id"] == step_id)


def test_ci_runs_the_sdk_tests_in_their_own_step():
    pipeline = json.loads((ROOT / "ci/pipeline.json").read_text(encoding="utf-8"))
    ids = [s["id"] for s in pipeline["steps"]]
    argv = _step("pytest-sdk")["argv"]

    # After the main suite, in the dev environment, with the SDK's own config
    # (`pytest tests/` from the root cannot import plugboard_sdk).
    assert ids.index("pytest-sdk") == ids.index("pytest") + 1
    assert argv[:3] == ["{scratch}/venv/bin/python", "-m", "pytest"]
    assert _option_values(argv, "-c") == ["sdk/python/pyproject.toml"]
    assert "sdk/python/tests" in argv
    assert not [a for a in argv if a.startswith("--cov")]


@pytest.mark.parametrize("step_id", ["ruff", "ruff-format"])
def test_ci_lints_the_sdk(step_id):
    assert "sdk/python/" in _step(step_id)["argv"]


# ── PLU-74: no pip in the MCP virtualenv ────────────────────────────────────
# `python -m venv` seeds /opt/venv-mcp with the pip bundled by the image's
# ensurepip (25.0.1 in the image in service, 15/11: PYSEC-2026-196, -1795,
# -1796, -2875, -2876, -3721). The MCP server never needs pip at run time, so
# the virtualenv is created without it and filled by the image's own pip
# through `--python`, which re-runs that pip under the venv's interpreter with
# the same arguments (pip/_internal/cli/main_parser.py, pip >= 22.3).

_MCP_PYTHON = "/opt/venv-mcp/bin/python"
_NO_MCP_PIP = (
    "sh -c 'if /opt/venv-mcp/bin/python -m pip --version; then exit 1; fi; "
    "echo no pip in /opt/venv-mcp'"
)


def _mcp_run() -> str:
    (run,) = [r for r in _runs(_full_stage()) if "requirements-mcp.lock" in r]
    return run


def test_the_mcp_virtualenv_is_created_without_pip():
    run = _mcp_run()

    assert re.search(r"python -m venv --without-pip /opt/venv-mcp(?: |$)", run)
    # Filled by the image's pip, hash-checked, from the same lock.
    assert re.search(
        rf"python -m pip --python {_MCP_PYTHON} install\s+--no-cache-dir\s+"
        r"--require-hashes\s+-r\s+/tmp/requirements-mcp\.lock",
        run,
    ), run
    assert "/opt/venv-mcp/bin/pip" not in _full_stage()
    assert f'{_MCP_PYTHON} -c "import mcp, aiohttp' in run
    assert _swallowed(run) == [], run


def test_the_build_fails_when_the_mcp_virtualenv_has_pip():
    runs = _runs(_full_stage())
    guards = [r for r in runs if f"if {_MCP_PYTHON} -m pip --version" in r]

    assert len(guards) == 1, guards
    assert re.search(
        rf"if {_MCP_PYTHON} -m pip --version; then .*exit 1; fi", guards[0]
    )
    # `set -e` ignores a command negated by `!`.
    assert "! " + _MCP_PYTHON not in guards[0]
    assert runs.index(guards[0]) >= runs.index(_mcp_run())


def test_the_ci_image_step_checks_pip_in_both_environments():
    """The built image has no pip, in the MCP virtualenv or in the gateway's
    own environment."""
    checks = _option_values(_image_step()["argv"], "--verifier")

    for check in (_NO_MCP_PIP, _NO_GATEWAY_PIP):
        assert check in checks
        words = shlex.split(check)
        assert words[:2] == ["sh", "-c"] and len(words) == 3
    # The gateway's pip is no longer expected to start.
    assert "python -m pip --version" not in checks


# ── PLU-74: no pip at all in the runtime image ──────────────────────────────
# Nothing runs pip once the image is built (plugboard/, scripts/, deploy/,
# run.py, the CI checks). The base stage keeps it to install the locks; the
# final targets remove it after its last use, so no pip advisory can reach a
# running gateway. `(?<![/\w])` keeps the MCP venv's `/opt/venv-mcp/bin/python
# -m pip` apart from the gateway's `python -m pip`.

_NO_GATEWAY_PIP = (
    "sh -c 'if python -m pip --version; then exit 1; fi; "
    "echo no pip in the gateway environment'"
)
_GATEWAY_PIP = r"(?<![/\w])python -m pip"


def _stage(name: str) -> str:
    parts = re.split(r"(?m)^FROM .* AS (\w+)\s*$", DOCKERFILE)
    return dict(zip(parts[1::2], parts[2::2]))[name]


def _uses_pip(run: str) -> bool:
    return bool(
        re.search(_GATEWAY_PIP + r"(?! uninstall -y pip)", run)
        or re.search(r"(?:^RUN |[;&|] )\s*pip3? ", run)
    )


@pytest.mark.parametrize("target", ["api", "full"])
def test_the_runtime_image_removes_pip_after_its_last_use(target):
    runs = _runs(_stage(target))
    removals = [
        i
        for i, r in enumerate(runs)
        if re.search(_GATEWAY_PIP + " uninstall -y pip", r)
    ]
    assert len(removals) == 1, runs
    (removed,) = removals
    guard = runs[removed]
    # Removed, then checked in the same step (an `if`, not `!`).
    assert re.search(
        _GATEWAY_PIP + r" uninstall -y pip .*if python -m pip --version; "
        r"then .*exit 1; fi",
        guard,
    ), guard
    assert _swallowed(guard) == [], guard
    # No step uses pip afterwards (the MCP install comes before, in full).
    assert [r for r in runs[removed + 1 :] if _uses_pip(r)] == []
    if target == "full":
        assert runs.index(_mcp_run()) < removed


def test_the_base_stage_keeps_pip_to_install_the_lock():
    base = _stage("base")
    assert re.search(
        r"(?m)^RUN pip install --require-hashes -r requirements\.lock$", base
    )
    assert "uninstall" not in base
