"""The ``full`` image builds the GitHub CLI from its sources (SEC-VEILLE).

``plugboard/cli_auth.py`` runs ``gh auth token`` for the GitHub Copilot and
GitHub Models providers, so the image keeps ``gh``. The last published
release, 2.104.0, embeds golang.org/x/net v0.61.0 (CVE-2026-78661, HIGH, fixed
in v0.62.0) and no fixed gh exists yet. A Go stage therefore builds the
pinned tag, checks its commit, raises x/net and checks the binary it makes;
the ``full`` stage only copies it. Read without Docker: the real build and
the image scan are the CI's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")

# Lowest acceptable versions: gh 2.104.0's go.mod asks for toolchain
# go1.27.4, and x/net v0.62.0 is the first release with the fix.
MIN_GO = (1, 27, 4)
MIN_XNET = (0, 62, 0)
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Ways a shell step can swallow a failure (same list as test_ci_image_full).
SWALLOWS = ("||", "|", "; true", "&& true", "exit 0", "set +e", "2>/dev/null")


def _stages() -> dict:
    parts = re.split(r"(?m)^FROM (\S+) AS (\w+)\s*$", DOCKERFILE)
    return {
        name: (image, body)
        for image, name, body in zip(parts[1::3], parts[2::3], parts[3::3])
    }


def _runs(stage: str) -> list:
    joined = re.sub(r"\\\n\s*", " ", stage)
    return [line for line in joined.splitlines() if line.startswith("RUN ")]


def _args(text: str) -> dict:
    return dict(re.findall(r"(?m)^ARG ([A-Za-z0-9_]+)=(\S+)\s*$", text))


def _version(value: str) -> tuple:
    match = SEMVER.match(value)
    assert match, f"{value!r} must be an exact x.y.z version"
    return tuple(int(part) for part in match.groups())


def _gh_stage() -> str:
    stages = _stages()
    assert "gh" in stages, "no `FROM ... AS gh` stage builds the GitHub CLI"
    return stages["gh"][1]


def _gh_build_run() -> str:
    runs = [r for r in _runs(_gh_stage()) if "go build" in r]
    assert len(runs) == 1, runs
    return runs[0]


def test_the_full_stage_no_longer_installs_gh_from_apt():
    _, full = _stages()["full"]
    assert "cli.github.com" not in full
    assert "githubcli" not in full
    assert "github-cli.list" not in full
    for run in _runs(full):
        if "apt-get install" in run:
            packages = run.split("apt-get install", 1)[1].split("&&", 1)[0].split()
            assert "gh" not in packages, run
            # gnupg only served the GitHub CLI key.
            assert "gnupg" not in packages, run


def test_the_header_no_longer_says_gh_comes_from_apt():
    assert "gh via its apt repository" not in DOCKERFILE


def test_the_go_image_is_pinned_to_an_exact_release():
    image = _args(DOCKERFILE.split("\nFROM ", 1)[0]).get("GO_IMAGE", "")
    match = re.fullmatch(r"golang:(\d+\.\d+\.\d+)(?:-[a-z0-9.]+)?", image)
    assert match, f"GO_IMAGE={image!r} must name an exact golang:x.y.z tag"
    assert _version(match[1]) >= MIN_GO
    assert _stages()["gh"][0] == "${GO_IMAGE}"


def test_the_gh_stage_pins_its_release_commit_and_x_net():
    args = _args(_gh_stage())
    _version(args.get("GH_VERSION", ""))
    assert re.fullmatch(r"[0-9a-f]{40}", args.get("GH_COMMIT", ""))
    assert _version(args.get("GH_XNET_VERSION", "")) >= MIN_XNET
    # One source per version: no other stage declares them again.
    for name in ("GH_VERSION", "GH_COMMIT", "GH_XNET_VERSION", "GO_IMAGE"):
        assert len(re.findall(rf"(?m)^ARG {name}\b", DOCKERFILE)) == 1, name


def test_the_gh_build_checks_the_tag_commit_and_fails_otherwise():
    run = _gh_build_run()
    assert '--branch "v${GH_VERSION}"' in run
    assert "https://github.com/cli/cli.git" in run
    assert re.search(
        r'test "\$\(git -C \S+ rev-parse HEAD\)" = "\$\{GH_COMMIT\}"', run
    ), run
    assert run.startswith("RUN set -e")
    assert [s for s in SWALLOWS if s in run] == []


def test_the_gh_build_raises_x_net_and_verifies_the_modules():
    run = _gh_build_run()
    steps = [
        'go get "golang.org/x/net@v${GH_XNET_VERSION}"',
        "go mod tidy",
        "go mod verify",
        "CGO_ENABLED=0 go build -trimpath",
    ]
    positions = [run.find(step) for step in steps]
    assert -1 not in positions, dict(zip(steps, positions))
    assert positions == sorted(positions), "x/net is raised before the build"
    # No toolchain download behind the pinned image's back.
    assert "GOTOOLCHAIN=local" in _gh_stage()


def test_the_gh_binary_keeps_its_release_version():
    run = _gh_build_run()
    assert "-X github.com/cli/cli/v2/internal/build.Version=${GH_VERSION}" in run
    assert "./cmd/gh" in run
    assert 'grep -qxF "gh version ${GH_VERSION} (' in run


def test_the_gh_binary_is_checked_for_the_raised_x_net():
    run = _gh_build_run()
    assert "go version -m" in run
    assert 'want="v${GH_XNET_VERSION}"' in run
    assert '$2 == "golang.org/x/net"' in run


def test_the_full_stage_copies_gh_onto_the_path_and_starts_it():
    _, full = _stages()["full"]
    assert re.search(r"(?m)^COPY --from=gh \S+ /usr/local/bin/gh$", full)
    checks = [r for r in _runs(full) if "Installed CLI binaries" in r]
    assert len(checks) == 1
    assert re.search(r"(?:; |^RUN )\s*gh --version;", checks[0])


def test_the_dockerfile_says_when_to_go_back_to_apt():
    assert re.search(r"(?i)back to (?:the )?apt", _gh_stage())


# Executables the ``gnupg`` package pulls onto the PATH (gnupg -> dirmngr,
# gpg, gpg-agent, gpgconf, gpgsm; gpg-agent -> pinentry-curses, which also
# provides the /usr/bin/pinentry alternative).
GNUPG_EXECUTABLES = (
    "/usr/bin/dirmngr",
    "/usr/bin/dirmngr-client",
    "/usr/bin/gpg",
    "/usr/bin/gpg-agent",
    "/usr/bin/gpg-connect-agent",
    "/usr/bin/gpgconf",
    "/usr/bin/gpgsm",
    "/usr/bin/pinentry",
    "/usr/bin/pinentry-curses",
)


def _path_list() -> set:
    text = (ROOT / "ci" / "image-path-executables.txt").read_text(encoding="utf-8")
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _full_apt_packages() -> set:
    _, full = _stages()["full"]
    packages = set()
    for run in _runs(full):
        if "apt-get install" in run:
            words = run.split("apt-get install", 1)[1].split("&&", 1)[0].split()
            packages |= {w for w in words if not w.startswith("-")}
    return packages


def test_the_path_list_expects_gh_where_the_dockerfile_copies_it():
    # The CI compares the image's PATH with ci/image-path-executables.txt
    # (PLU-71): moving gh without updating the list fails docker-build.
    _, full = _stages()["full"]
    dest = re.search(r"(?m)^COPY --from=gh \S+ (\S+)$", full)
    assert dest, "no `COPY --from=gh` in the full stage"
    listed = _path_list()
    assert dest[1] in listed, f"{dest[1]} missing from ci/image-path-executables.txt"
    others = {p for p in listed if p.rsplit("/", 1)[-1] == "gh" and p != dest[1]}
    assert others == set(), f"gh still expected at {sorted(others)}"


def test_the_path_list_has_no_gnupg_executable_without_gnupg():
    if "gnupg" in _full_apt_packages():
        pytest.skip("the full stage installs gnupg")
    left = sorted(set(GNUPG_EXECUTABLES) & _path_list())
    assert left == [], f"gnupg is no longer installed, yet the list expects {left}"
