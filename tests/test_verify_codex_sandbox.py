"""``deploy/ovh/verify-codex-sandbox.py`` runs on the UAT too (PLU-71).

The script checks codex's Linux sandbox inside the running gateway without a
model request: ``codex sandbox`` (no prompt, no inference) runs a small shell
probe that must read its working directory and nothing else, and write
nothing. It would have seen INC-4 (every codex sandbox failed on the first
4.14.2 image), but it was cited nowhere and demanded the AppArmor profile of
``deploy/ovh/`` (``plugboard-codex``), so it could not pass in the UAT
container (``plugboard-userns``, ``deploy/uat/``). It now takes the expected
profile as ``--apparmor``; without it, the production profile is still
required, in enforce mode, with the same checks (uid 1000, no new privileges,
no effective capability, a seccomp filter).
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy/ovh/verify-codex-sandbox.py"
STATUS_OK = "Name:\tpython\nNoNewPrivs:\t1\nCapEff:\t0000000000000000\nSeccomp:\t2\n"


@pytest.fixture(scope="module")
def verify():
    spec = importlib.util.spec_from_file_location("verify_codex_sandbox", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _confinement(verify, **changes):
    values = {
        "uid": 1000,
        "status": STATUS_OK,
        "apparmor": "plugboard-codex (enforce)",
        **changes,
    }
    return verify.Confinement(**values)


def test_production_requires_its_own_profile_by_default(verify):
    args = verify.parse_args([])

    assert args.apparmor == "plugboard-codex"
    report = verify.check_confinement(_confinement(verify), args.apparmor)
    assert report["apparmor"] == "plugboard-codex (enforce)"
    with pytest.raises(verify.CheckFailed, match="plugboard-userns"):
        verify.check_confinement(
            _confinement(verify, apparmor="plugboard-userns (enforce)"),
            args.apparmor,
        )


def test_the_uat_profile_is_accepted_when_named(verify):
    args = verify.parse_args(["--apparmor", "plugboard-userns"])
    conf = _confinement(verify, apparmor="plugboard-userns (enforce)")

    report = verify.check_confinement(conf, args.apparmor)

    assert report == {
        "uid": 1000,
        "apparmor": "plugboard-userns (enforce)",
        "no_new_privileges": True,
        "effective_capabilities": 0,
        "seccomp": "filter",
    }


@pytest.mark.parametrize(
    "changes, cause",
    [
        ({"apparmor": "plugboard-userns (complain)"}, "apparmor"),
        ({"apparmor": "unconfined"}, "apparmor"),
        ({"uid": 0}, "uid"),
        (
            {"status": STATUS_OK.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0")},
            "NoNewPrivs",
        ),
        (
            {"status": STATUS_OK.replace("0000000000000000", "00000000a80425fb")},
            "CapEff",
        ),
        ({"status": STATUS_OK.replace("Seccomp:\t2", "Seccomp:\t0")}, "Seccomp"),
        ({"status": "Name:\tpython\n"}, "NoNewPrivs"),
    ],
)
def test_the_uat_profile_weakens_no_other_check(verify, changes, cause):
    conf = _confinement(verify, **{"apparmor": "plugboard-userns (enforce)", **changes})

    with pytest.raises(verify.CheckFailed, match=cause):
        verify.check_confinement(conf, "plugboard-userns")


def test_the_checks_survive_python_optimisations():
    """``assert`` would vanish under ``python -O``: the script never uses it."""
    assert "\n    assert " not in SCRIPT.read_text(encoding="utf-8")


# ── The probe: codex sandbox, never a prompt ────────────────────────────────


def _fake_codex(tmp_path, body: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "codex-argv.json"
    script = bin_dir / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"open({str(log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n" + body,
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def test_a_codex_without_sandbox_fails_the_probe(verify, tmp_path, monkeypatch):
    """A codex that runs the probe unconfined: the outside read succeeds."""
    bin_dir = _fake_codex(
        tmp_path,
        "argv = sys.argv[sys.argv.index('--') + 1 :]\nos.execv(argv[0], argv)\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(verify.CheckFailed, match="LEAKED"):
        verify.probe_codex_sandbox()

    argv = json.loads((tmp_path / "codex-argv.json").read_text())
    # `codex sandbox`, never `codex exec` nor a prompt on stdin.
    assert argv[0] == "sandbox"
    assert "exec" not in argv[: argv.index("--")]


def test_a_codex_sandbox_that_cannot_start_fails_the_probe(
    verify, tmp_path, monkeypatch
):
    bin_dir = _fake_codex(
        tmp_path,
        "sys.stderr.write('bwrap: Can\\'t mount proc on /proc\\n')\nsys.exit(1)\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(verify.CheckFailed, match="mount proc"):
        verify.probe_codex_sandbox()


# ── Cited in the UAT check and the production gate ──────────────────────────


def test_the_uat_readme_runs_it_with_the_uat_profile():
    text = (ROOT / "deploy/uat/README.md").read_text(encoding="utf-8")

    assert (
        "docker exec -i uat-plugboard-gateway-1 python - --apparmor plugboard-userns "
        "< deploy/ovh/verify-codex-sandbox.py" in text
    )


def test_the_production_gate_runs_it_beside_the_agy_check():
    gate = (ROOT / "docs/runbooks/prod-gate.md").read_text(encoding="utf-8")
    codex = "gw exec -T gateway python - < deploy/ovh/verify-codex-sandbox.py"
    agy = "gw exec -T gateway python - < deploy/ovh/verify-agy-sandbox.py"

    assert codex in gate and agy in gate
    assert abs(gate.index(codex) - gate.index(agy)) < 400
