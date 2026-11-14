"""agy writes without asking inside ``TMPDIR`` (real run on the UAT, agy 1.4.4,
14/11): with none set it falls back on the shared ``/tmp``, where the launch
directories of the other requests live. Plugboard gives each agy request its
own ``TMPDIR``: a private directory under the request's launch directory,
removed when the request ends, however it ends.

A real process is spawned (a shell script named ``agy``), so the directory is
looked at while the CLI runs and after the gateway is done with it.
"""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from pathlib import Path

import pytest

from plugboard import cli_subprocess as cs

_ANSWER = '{"status":"SUCCESS","response":"OK","usage":{}}'
_BODY = {"model": "gemini-test", "messages": [{"role": "user", "content": "hi"}]}


def _read(record: Path) -> dict:
    tmpdir, cwd, mode, arg = record.read_text(encoding="utf-8").splitlines()[:4]
    return {"TMPDIR": tmpdir, "cwd": cwd, "mode": mode, "argv": arg}


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    """Install a fake ``agy`` that records what it was given; returns
    ``(record file, launch directory)``. ``hang=True``: it then never ends."""
    spawn = tmp_path / "spawn"
    spawn.mkdir(mode=0o700)
    monkeypatch.setattr(cs, "_SPAWN_CWD_CACHE", str(spawn))
    monkeypatch.delenv("PLUGBOARD_SUBPROCESS_CWD", raising=False)
    monkeypatch.setattr(cs, "_binary_path_cache", {})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    record = tmp_path / "record.txt"

    def install(hang: bool = False) -> tuple:
        script = bin_dir / "agy"
        script.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"
            f'{{ echo "$TMPDIR"; echo "$PWD"; stat -c %a "$TMPDIR"; echo "$*"; }}'
            f" > {record}.part 2>&1\n"
            f"mv {record}.part {record}\n"
            + ("sleep 30\n" if hang else "")
            + f"echo '{_ANSWER}'\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return record, spawn

    return install


def _assert_private(seen: dict, spawn: Path, server_tmp: Path) -> None:
    tmpdir = seen["TMPDIR"]
    assert tmpdir, "agy must be given a TMPDIR"
    assert seen["cwd"] == str(spawn)
    # Under the launch directory of the request, never the shared one.
    assert os.path.dirname(tmpdir) == str(spawn)
    assert tmpdir not in ("/tmp", tempfile.gettempdir(), str(server_tmp))
    assert seen["mode"] == "700"


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_agy_gets_a_private_tmpdir_removed_with_the_request(
    fake_agy, tmp_path, monkeypatch, stream
):
    record, spawn = fake_agy()
    server_tmp = tmp_path / "server-tmp"
    server_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(server_tmp))  # the server's is not agy's
    pieces: list = []

    result = await cs.complete(
        "agy", dict(_BODY), on_delta=pieces.append if stream else None
    )

    assert result["choices"][0]["message"]["content"] == "OK"
    seen = _read(record)
    assert ("stream-json" in seen["argv"]) is stream
    _assert_private(seen, spawn, server_tmp)
    assert not os.path.exists(seen["TMPDIR"]), "removed when the request ends"
    assert list(spawn.iterdir()) == []


async def test_agy_tmpdir_is_removed_when_the_cli_times_out(fake_agy, tmp_path):
    record, spawn = fake_agy(hang=True)

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("agy", dict(_BODY), timeout_s=1.0)

    assert info.value.timed_out is True
    seen = _read(record)
    _assert_private(seen, spawn, tmp_path / "server-tmp")
    assert not os.path.exists(seen["TMPDIR"])
    assert list(spawn.iterdir()) == []


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_agy_tmpdir_is_removed_when_the_request_is_cancelled(
    fake_agy, tmp_path, stream
):
    record, spawn = fake_agy(hang=True)
    pieces: list = []
    task = asyncio.ensure_future(
        cs.complete(
            "agy",
            dict(_BODY),
            timeout_s=30.0,
            on_delta=pieces.append if stream else None,
        )
    )
    for _ in range(200):  # until the CLI runs
        if record.exists():
            break
        await asyncio.sleep(0.02)
    seen = _read(record)
    _assert_private(seen, spawn, tmp_path / "server-tmp")
    assert os.path.isdir(seen["TMPDIR"]), "there while the CLI runs"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not os.path.exists(seen["TMPDIR"])
    assert list(spawn.iterdir()) == []


@pytest.mark.parametrize("kind", ["claude", "codex", "gemini"])
def test_the_other_clis_get_the_servers_tmpdir_or_none(kind, monkeypatch):
    """What they receive today, pinned: the server's TMPDIR when it has one
    (the allowlist forwards it), nothing otherwise. They take no tool that
    writes there: claude and gemini have none, codex is confined by bwrap."""
    monkeypatch.delenv("TMPDIR", raising=False)
    assert "TMPDIR" not in cs.build_child_env(kind)
    monkeypatch.setenv("TMPDIR", "/var/tmp/server")
    assert cs.build_child_env(kind)["TMPDIR"] == "/var/tmp/server"
