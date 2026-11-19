"""The relay's interpreter shows no third-party package (PLU-171, I4).

agy's cage binds the folder of the gateway's standard library whole, for the
relay (the gateway's Python, ``-I -S``). In the image that folder
(``/usr/local/lib/python3.12``) holds ``site-packages``, the gateway's
third-party dependencies, which agy could read. The relay never imports them
(``-S``): each ``site-packages`` or ``dist-packages`` folder of the relay's
interpreter is now hidden under an empty read-only tmpfs, in agy's cage and
in the cage of a claude web run (PLU-162). The standard library stays.

The tests needing the host's bwrap are skipped where it cannot create
namespaces.
"""

# ruff: noqa: F811 - the fixtures of tests/test_agy_cage.py and test_web_cage.py

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from plugboard import agy_cage, net_cage, web_access
from plugboard import cli_subprocess as cs
from tests.test_agy_cage import real_bwrap, setup  # noqa: F401 - pytest fixtures
from tests.test_agy_cage_net import _run_caged, _store, _tmpdir, fake_agy  # noqa: F401
from tests.test_web_cage import _BODY, _answer, _install_claude, web_setup  # noqa: F401


@pytest.fixture
def stdlib(tmp_path, monkeypatch):
    """A standard library folder of the relay's interpreter holding a
    ``site-packages`` and a ``dist-packages`` with a file each, added to
    what the relay's interpreter needs."""
    lib = tmp_path / "pylib" / "python3.12"
    for name in ("site-packages", "dist-packages"):
        (lib / name).mkdir(parents=True)
        (lib / name / "dependency.py").write_text("SECRET = 'third-party'\n")
    (lib / "os_like.py").write_text("# the standard library\n")
    real = net_cage.relay_python()

    def relay_python():
        return [*real, str(lib)]

    monkeypatch.setattr(net_cage, "relay_python", relay_python)
    monkeypatch.setattr(agy_cage, "_relay_python", relay_python)
    return lib


def _tmpfs(argv) -> list:
    sep = argv.index("--")
    return [argv[i + 1] for i, a in enumerate(argv[:sep]) if a == "--tmpfs"]


def test_agy_cage_hides_the_interpreters_packages(setup, fake_agy, stdlib):
    tmpdir = _tmpdir(setup)
    egress = os.path.join(tmpdir, agy_cage.EGRESS_SOCKET)
    listener = net_cage.bind_unix(egress)
    listener.listen()
    try:
        env = {"HOME": str(setup["home"]), "TMPDIR": tmpdir}
        argv = agy_cage.cage_argv(
            fake_agy, ["models"], env=env, tmpdir=tmpdir, private_store=_store(setup)
        )
    finally:
        listener.close()
    hidden = [str(stdlib / "site-packages"), str(stdlib / "dist-packages")]
    assert sorted(_tmpfs(argv)) == sorted(hidden)
    sep = argv.index("--")
    bound = next(
        i
        for i, a in enumerate(argv[:sep])
        if a == "--ro-bind" and argv[i + 1] == str(stdlib)
    )
    root_ro = next(
        i for i, a in enumerate(argv[:sep]) if argv[i : i + 2] == ["--remount-ro", "/"]
    )
    for path in hidden:
        at = argv.index(path)
        # Over the bound folder, read-only (the root's remount does not
        # reach a tmpfs), before the root is.
        assert bound < at < root_ro
        assert argv[at - 1] == "--tmpfs"
        assert argv[at + 1 : at + 3] == ["--remount-ro", path]


def test_web_cage_hides_the_interpreters_packages(tmp_path, stdlib):
    # The launch directory apart from the interpreter's folder: the cage
    # hides it too (PLU-162), and refuses to hide the relay's Python.
    spawn = tmp_path / "spawn"
    folder = spawn / "web-run"
    folder.mkdir(parents=True)
    listener = net_cage.bind_unix(str(folder / net_cage.EGRESS_SOCKET))
    try:
        argv = net_cage.web_cage_argv(
            "/usr/bin/bwrap", ["claude"], env={}, cwd=str(spawn), folder=str(folder)
        )
    finally:
        listener.close()
    hidden = sorted([str(stdlib / "site-packages"), str(stdlib / "dist-packages")])
    assert sorted(_tmpfs(argv)) == sorted([*hidden, str(spawn)])
    for path in hidden:
        at = argv.index(path)
        assert argv[at - 1] == "--tmpfs" and argv[at + 1 : at + 3] == [
            "--remount-ro",
            path,
        ]
        assert at > argv.index("--dev-bind")


def test_nothing_is_hidden_outside_what_the_relay_has(tmp_path, monkeypatch):
    """Only packages folders inside the relay's own paths, never a link."""
    lib = tmp_path / "lib"
    (lib / "site-packages").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere" / "site-packages"
    elsewhere.mkdir(parents=True)
    (lib / "dist-packages").symlink_to(elsewhere)
    assert net_cage.relay_site_packages([str(lib)]) == [str(lib / "site-packages")]
    assert net_cage.relay_site_packages([str(tmp_path / "other")]) == []


# The probe stands for agy or claude: what it sees of the packages folders.
_LOOK = r"""
import json, os, sys
lib = sys.argv[1]
out = {}
for name in ("site-packages", "dist-packages"):
    path = os.path.join(lib, name)
    out[name] = sorted(os.listdir(path))
    try:
        open(os.path.join(path, "planted.py"), "w").close()
        out[name + "-write"] = "written"
    except OSError as exc:
        out[name + "-write"] = type(exc).__name__
out["stdlib"] = sorted(os.listdir(lib))
print(json.dumps(out))
"""


def _hidden(seen: dict) -> None:
    for name in ("site-packages", "dist-packages"):
        assert seen[name] == [], seen
        assert seen[name + "-write"] != "written", seen
    assert "os_like.py" in seen["stdlib"]


async def test_real_agy_cage_shows_no_package(setup, real_bwrap, stdlib):
    code, stdout, stderr = await _run_caged(
        sys.executable, ["-I", "-S", "-c", _LOOK, str(stdlib)], str(setup["spawn"])
    )
    assert code == 0, stderr
    _hidden(json.loads(stdout))
    assert (stdlib / "site-packages" / "dependency.py").exists()


async def test_real_web_cage_shows_no_package(web_setup, real_bwrap, stdlib):
    _install_claude(
        web_setup["bin"],
        _LOOK.replace("sys.argv[1]", repr(str(stdlib))).replace(
            "print(json.dumps(out))",
            "sys.stdin.read()\nprint(json.dumps({'type': 'result', 'subtype': "
            "'success', 'is_error': False, 'result': json.dumps(out)}))",
        ),
    )
    response = await asyncio.wait_for(
        cs.complete("claude", dict(_BODY), web=web_access.WebPolicy(max_uses=3)), 60
    )
    _hidden(_answer(response))
    assert not (stdlib / "site-packages" / "planted.py").exists()
