"""scripts/bench_agy_first_token.py with its fake agy (BUG-106, no inference).

The bench drives the gateway's own chat route in process (ASGI) around a fake
agy written in shell, and splits the time to the first streamed word into
what happens before agy starts and what happens between agy writing its first
word and the client receiving it. It runs in a subprocess: it sets its own
HOME, data directory and keys before importing the gateway.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_agy_cage import _bwrap_unusable_reason

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "bench_agy_first_token.py"
# Short fake timings: init, first word, tail, exit (seconds).
FAST = "0.05,0.05,0.1,0.02"


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    home = tmp_path / "caller-home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(ROOT),
    )


@pytest.fixture
def real_bwrap():
    reason = _bwrap_unusable_reason()
    if reason:
        pytest.skip(f"bwrap unusable: {reason}")


def test_fake_bench_measures_each_variant(tmp_path, real_bwrap):
    out = tmp_path / "bench.json"
    proc = _run(
        tmp_path,
        "--rounds",
        "2",
        "--variants",
        "cage=on;cage=off",
        "--fake-timings",
        FAST,
        "--store-leftovers",
        "20",
        "--json",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(out.read_text())
    assert report["agy"] == "fake"
    rows = {row["variant"]: row for row in report["rows"]}
    assert set(rows) == {"purge=off,cage=on", "purge=off,cage=off"}
    for row in rows.values():
        assert row["calls"] == 2 and row["failed"] == 0
        for field in ("launch", "relay", "first_word", "total"):
            assert row[field]["n"] == 2, (row["variant"], field)
        # A real stream: the first word reaches the client before the end.
        assert row["first_word"]["med"] < row["total"]["med"]
    # Each call's own times are kept.
    calls = report["calls"]
    assert len(calls) == 4
    assert all(c["ok"] and c["relay"] >= 0 and c["launch"] >= 0 for c in calls)
    # The table names every variant and the host load.
    assert "purge=off,cage=on" in proc.stdout and "load" in proc.stdout


def test_fake_bench_leaves_the_shared_store_as_it_was(tmp_path, real_bwrap):
    """In the cage, what each fake call writes stays in its private store,
    as in service (BUG-157); the store's sign-in is never touched."""
    out = tmp_path / "bench.json"
    proc = _run(
        tmp_path,
        "--rounds",
        "1",
        "--variants",
        "cage=on",
        "--fake-timings",
        FAST,
        "--json",
        str(out),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(out.read_text())
    assert report["store_after"]["conversations"] == 0
    assert report["store_after"]["shared_summaries"] is False
    assert report["store_after"]["sign_in"] is True


def test_the_purge_is_refused_on_a_tree_without_it(tmp_path):
    """Since 4.15.2 there is no purge to measure (a private store per call):
    purge=full is for the trees from 4.14.1 to 4.15.1 (--root)."""
    proc = _run(tmp_path, "--variants", "purge=full,cage=off")
    assert proc.returncode == 2
    assert "this tree has no agy purge" in proc.stderr


def test_real_agy_needs_an_existing_binary(tmp_path):
    proc = _run(tmp_path, "--real-agy", str(tmp_path / "no-such-agy"))
    assert proc.returncode == 2
    assert "real agy" in proc.stderr


def test_real_agy_runs_only_in_its_cage(tmp_path):
    """Refused before anything runs: the binary is never started."""
    ran = tmp_path / "ran"
    binary = tmp_path / "agy"
    binary.write_text(f"#!/bin/sh\ntouch {ran}\n")
    binary.chmod(0o755)
    proc = _run(tmp_path, "--real-agy", str(binary), "--variants", "cage=off")
    assert proc.returncode == 2
    assert "cage" in proc.stderr
    assert not ran.exists()


def test_bad_variant_is_refused(tmp_path):
    proc = _run(tmp_path, "--variants", "purge=sometimes")
    assert proc.returncode == 2
    assert "purge" in proc.stderr


def test_the_caller_home_is_never_used_with_the_fake(tmp_path):
    proc = _run(
        tmp_path,
        "--rounds",
        "1",
        "--variants",
        "cage=off",
        "--fake-timings",
        FAST,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert list((tmp_path / "caller-home").iterdir()) == []
