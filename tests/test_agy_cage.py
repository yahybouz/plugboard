"""Every agy launch runs inside a bubblewrap cage (BUG-86).

agy runs tenant prompts with the gateway's uid. Without a cage it could read
the application data, the other CLIs' sign-ins (``~/.claude``, ``~/.codex``),
``/usr/local`` and, through ``/proc/<gateway pid>/environ``, the server's
keys, and write anywhere the gateway can. ``plugboard.agy_cage`` builds the
only command line that launches agy: ``bwrap`` with its own user, pid, ipc
and uts namespaces, an empty root holding the system libraries and a few
``/etc`` files read-only, agy's store (``~/.gemini/antigravity-cli``) and the
request's private ``TMPDIR`` writable, the binary read-only, the rest of the
root read-only. No cage, no agy: a client gets ``503``
``provider_sandbox_unavailable`` and nothing is launched.

The integration tests run the host's real ``bwrap`` around a fake agy (a
shell script). A shell needs ``/usr/bin`` and the ``/bin`` link, which agy
does not: they are passed through ``cage_argv``'s test-only parameter
``_extra_ro_for_tests``, never through a variable or an option. They are
skipped, with the reason, where bwrap cannot create namespaces (a CI runner
without user namespaces).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from plugboard import agy_cage
from plugboard import cli_subprocess as cs

ROOT = Path(__file__).resolve().parents[1]
_ANSWER = '{"status":"SUCCESS","response":"OK","usage":{}}'
_BODY = {"model": "gemini-test", "messages": [{"role": "user", "content": "hi"}]}
_SECRETS = {
    "ADMIN_KEY": "adm-secret-4f1c",
    "API_KEY": "api-secret-9b2e",
    "ENCRYPTION_KEY": "enc-secret-77aa",
    "PLUGBOARD_SOMETHING": "pb-secret-31d0",
}
CLIENT_DETAIL = (
    "The agy provider is unavailable: its sandbox (bubblewrap) could not "
    "start, so agy was not run (reference {ref} in the gateway log)."
)
# What a shell script needs on top of what agy needs (test-only).
_SHELL_RO = ("/usr/bin", "/bin")


# ── bwrap usable on this host? (probed once per session) ────────────────────


@functools.lru_cache(maxsize=1)
def _bwrap_unusable_reason() -> str:
    """Empty when the host's bwrap creates the namespaces the cage uses."""
    if not sys.platform.startswith("linux"):
        return "not Linux"
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return "bwrap not installed"
    argv = [bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc"]
    argv += ["--unshare-uts", "--ro-bind", "/usr", "/usr"]
    for path in ("/bin", "/lib", "/lib64"):
        if os.path.islink(path):
            argv += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            argv += ["--ro-bind", path, path]
    argv += ["--proc", "/proc", "--dev", "/dev", "--remount-ro", "/"]
    argv += ["--", shutil.which("true") or "/bin/true"]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return proc.stderr.decode("utf-8", "replace").strip()[:200]
    return ""


@pytest.fixture
def real_bwrap():
    reason = _bwrap_unusable_reason()
    if reason:
        pytest.skip(f"bwrap unusable: {reason}")


# ── Fake agy installation ───────────────────────────────────────────────────


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A throwaway HOME (agy's store holds a sign-in token, and agy's
    ``~/.gemini/config`` and ``~/.gemini/projects.json`` exist, as after its
    first run), the private launch directory, a ``bin`` holding the fake agy,
    and PATH on it."""
    home = tmp_path / "home"
    store = home / ".gemini" / "antigravity-cli"
    store.mkdir(parents=True)
    (store / "antigravity-oauth-token").write_text("old-token\n")
    config = home / ".gemini" / "config"
    (config / "projects").mkdir(parents=True)
    (config / "mcp_config.json").write_text("{}\n")
    (home / ".gemini" / "projects.json").write_text("{}\n")
    for other in (".claude", ".codex"):
        (home / other).mkdir()
        (home / other / "credentials.json").write_text("other-cli-secret")
    app_data = tmp_path / "app" / "data"
    app_data.mkdir(parents=True)
    (app_data / "providers.json").write_text("app-data-secret")
    spawn = tmp_path / "spawn"
    spawn.mkdir(mode=0o700)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(cs, "_SPAWN_CWD_CACHE", str(spawn))
    monkeypatch.delenv("PLUGBOARD_SUBPROCESS_CWD", raising=False)
    monkeypatch.setattr(cs, "_binary_path_cache", {})
    # bwrap is looked up on PATH, as outside the image: whatever this machine
    # holds at the image's fixed path (INC-4) is not used.
    monkeypatch.setattr(
        agy_cage, "GATEWAY_BWRAP", str(tmp_path / "no-gateway-bwrap"), raising=False
    )
    return {
        "home": home,
        "store": store,
        "config": config,
        "projects": home / ".gemini" / "projects.json",
        "spawn": spawn,
        "bin": bin_dir,
        "app_data": app_data,
        "tmp": tmp_path,
    }


def _install(bin_dir: Path, name: str, body: str) -> Path:
    script = bin_dir / name
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _fake_agy_recording(setup, body: str = "") -> Path:
    """A fake agy that records it ran (outside any cage: the marker is in
    the test's tmp dir, so a run inside the cage cannot write it)."""
    ran = setup["tmp"] / "agy-ran"
    _install(
        setup["bin"],
        "agy",
        f"touch {ran} 2>/dev/null\ncat >/dev/null\n{body}echo '{_ANSWER}'\n",
    )
    return ran


@pytest.fixture
def shell_in_cage(monkeypatch):
    """The cage as built for agy, plus /usr/bin and /bin for the fake agy's
    shell, through the test-only parameter."""
    real = agy_cage.cage_argv
    monkeypatch.setattr(
        agy_cage,
        "cage_argv",
        functools.partial(real, _extra_ro_for_tests=_SHELL_RO),
    )


# ── Command line ────────────────────────────────────────────────────────────


def _argv(setup, binary: str = "/opt/agy/bin/agy", **kw):
    tmpdir = setup["spawn"] / "agy-tmp-x"
    tmpdir.mkdir(mode=0o700, exist_ok=True)
    env = {"HOME": str(setup["home"]), "TMPDIR": str(tmpdir), **kw.pop("env", {})}
    return (
        agy_cage.cage_argv(
            binary, ["--output-format", "json"], env=env, tmpdir=str(tmpdir), **kw
        ),
        str(tmpdir),
    )


def _pairs(argv, option):
    return [tuple(argv[i + 1 : i + 3]) for i, arg in enumerate(argv) if arg == option]


@pytest.fixture
def fake_binary(tmp_path):
    binary = tmp_path / "opt" / "agy"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def test_cage_argv_isolates_agy(setup, fake_binary):
    argv, tmpdir = _argv(setup, str(fake_binary))
    store = str(setup["store"])
    binary = str(fake_binary)

    assert os.path.basename(argv[0]) == "bwrap"
    for flag in (
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        # agy is pid 1 of the cage: no init of bwrap's left unreaped.
        "--as-pid-1",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
    ):
        assert flag in argv, flag
    # The network stays (agy needs it); no new session: _kill_group reaches
    # the whole group through bwrap's pid.
    for absent in ("--unshare-net", "--unshare-all", "--new-session", "--setenv"):
        assert absent not in argv, absent
    assert ("--proc", "/proc") == tuple(
        argv[argv.index("--proc") : argv.index("--proc") + 2]
    )
    assert argv[argv.index("--dev") + 1] == "/dev"
    # Writable: the store and the private TMPDIR, nothing else.
    assert _pairs(argv, "--bind") == [(store, store), (tmpdir, tmpdir)]
    # The binary read-only, after the store; then the root read-only.
    assert (binary, binary) in _pairs(argv, "--ro-bind")
    i_store = argv.index("--bind")
    i_bin = next(
        i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == binary
    )
    i_remount = argv.index("--remount-ro")
    assert i_store < i_bin < i_remount and argv[i_remount + 1] == "/"
    assert argv[argv.index("--chdir") + 1] == tmpdir
    sep = argv.index("--")
    assert argv[sep + 1 :] == [binary, "--output-format", "json"]
    mounted = {argv[i + 2] for i, a in enumerate(argv[:sep]) if "bind" in a}
    for hidden in ("/usr/bin", "/usr/local", "/app", "/home", "/tmp", "/sys", "/"):
        assert hidden not in mounted, hidden
    assert "/usr/lib" in mounted


def test_cage_argv_links_lib_dirs_that_are_links_on_the_host(setup, fake_binary):
    argv, _ = _argv(setup, str(fake_binary))
    for path in ("/lib", "/lib64", "/lib32"):
        if os.path.islink(path):
            assert ("--symlink", os.readlink(path), path) in [
                tuple(argv[i : i + 3]) for i, a in enumerate(argv) if a == "--symlink"
            ], path


def test_cage_argv_never_carries_the_environment(setup, fake_binary):
    """A proxy URL can hold a password: the environment goes through
    execve, never the command line."""
    argv, _ = _argv(
        setup,
        str(fake_binary),
        env={"HTTPS_PROXY": "http://user:proxy-pass@proxy:3128"},
    )
    assert not any("proxy-pass" in a for a in argv)


def test_cage_argv_binds_the_real_binary(setup, fake_binary, tmp_path):
    link = tmp_path / "agy-link"
    link.symlink_to(fake_binary)
    argv, _ = _argv(setup, str(link))
    sep = argv.index("--")
    assert argv[sep + 1] == str(fake_binary)
    assert str(link) not in argv


def test_cage_argv_binds_system_ca_variables(setup, fake_binary):
    present = [
        path
        for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs")
        if os.path.exists(path) and os.path.realpath(path) == path
    ]
    if len(present) < 2:
        pytest.skip("no system CA bundle and folder on this host")
    bundle, folder = present
    argv, _ = _argv(
        setup,
        str(fake_binary),
        env={"SSL_CERT_FILE": bundle, "SSL_CERT_DIR": folder},
    )
    pairs = _pairs(argv, "--ro-bind-try")
    assert (bundle, bundle) in pairs and (folder, folder) in pairs


def test_cage_argv_skips_missing_ca_paths(setup, fake_binary):
    argv, _ = _argv(setup, str(fake_binary), env={"SSL_CERT_DIR": "/etc/nonexistent"})
    assert "/etc/nonexistent" not in argv


@pytest.mark.parametrize(
    "where", ["app-data", "dotdot", "relative", "tmp", "usr-local", "etc-itself"]
)
def test_cage_argv_refuses_ca_paths_outside_the_system(
    setup, fake_binary, caplog, where
):
    """A CA variable naming the app's data (or anything outside the system's
    CA folders) would make it readable in the cage."""
    value = {
        "app-data": str(setup["app_data"]),
        "dotdot": "/etc/ssl/../.." + str(setup["app_data"]),
        "relative": "etc/ssl",
        "tmp": str(setup["tmp"]),
        "usr-local": "/usr/local/lib",
        "etc-itself": "/etc",
    }[where]

    with caplog.at_level("WARNING", logger="plugboard.agy_cage"):
        argv, _ = _argv(setup, str(fake_binary), env={"SSL_CERT_DIR": value})
        _argv(setup, str(fake_binary), env={"SSL_CERT_DIR": value})

    sep = argv.index("--")
    assert value not in argv[:sep]
    assert os.path.normpath(value) not in argv[:sep]
    assert str(setup["app_data"]) not in argv[:sep]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    ca = [w for w in warnings if "SSL_CERT_DIR" in w]
    assert len(ca) == 1, warnings
    message = ca[0].replace(", ".join(agy_cage._CA_ROOTS), "")
    assert value not in message and str(setup["tmp"]) not in message


def test_cage_argv_refuses_a_ca_path_through_a_link(
    setup, fake_binary, caplog, monkeypatch, tmp_path
):
    """Even under an allowed folder, a link would be followed by bwrap."""
    allowed = tmp_path / "certs-root"
    allowed.mkdir()
    (allowed / "real").mkdir()
    (allowed / "link").symlink_to(setup["app_data"])
    monkeypatch.setattr(agy_cage, "_CA_ROOTS", (str(allowed) + "/",))

    with caplog.at_level("WARNING", logger="plugboard.agy_cage"):
        argv, _ = _argv(
            setup,
            str(fake_binary),
            env={
                "SSL_CERT_DIR": str(allowed / "link"),
                "SSL_CERT_FILE": str(allowed / "real"),
            },
        )

    pairs = _pairs(argv, "--ro-bind-try")
    assert (str(allowed / "real"), str(allowed / "real")) in pairs
    sep = argv.index("--")
    assert str(allowed / "link") not in argv[:sep]
    assert str(setup["app_data"]) not in argv[:sep]
    assert any("SSL_CERT_DIR" in r.getMessage() for r in caplog.records)


def test_store_is_created_private_when_missing(setup, fake_binary):
    shutil.rmtree(setup["store"])
    argv, _ = _argv(setup, str(fake_binary))
    store = setup["store"]
    assert store.is_dir()
    assert stat.S_IMODE(store.stat().st_mode) == 0o700
    assert (str(store), str(store)) in _pairs(argv, "--bind")


@pytest.mark.parametrize("where", ["store", "gemini", "home"])
def test_store_behind_a_symbolic_link_is_refused(setup, fake_binary, tmp_path, where):
    elsewhere = tmp_path / "elsewhere"
    if where == "store":
        shutil.rmtree(setup["store"])
        (elsewhere / "store").mkdir(parents=True)
        setup["store"].symlink_to(elsewhere / "store")
    elif where == "gemini":
        shutil.move(str(setup["home"] / ".gemini"), str(elsewhere))
        (setup["home"] / ".gemini").symlink_to(elsewhere)
    else:
        real_home = tmp_path / "real-home"
        shutil.move(str(setup["home"]), str(real_home))
        setup["home"].symlink_to(real_home)
    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))
    assert str(info.value) == "agy store path goes through a symbolic link"


# ── agy's ~/.gemini/config and ~/.gemini/projects.json, read-only ─────────
#
# agy 1.4.4 reads config/, outside its store; without it in the cage every
# call failed on the UAT (15/11): "mkdir /home/app/.gemini/config: read-only
# file system". It is shown read-only; missing (agy never run on that
# account), the call is refused. projects.json is gemini's (BUG-115): shown
# read-only when it exists, never required. Nothing else of HOME.

_NOT_SET_UP = (
    "agy is not set up for this server account: ~/.gemini/config is missing "
    "(run agy once in a shell of the gateway user, as for its sign-in)"
)


def test_cage_argv_shows_agy_its_config_read_only(setup, fake_binary):
    argv, tmpdir = _argv(setup, str(fake_binary))
    store, config, projects = (
        str(setup["store"]),
        str(setup["config"]),
        str(setup["projects"]),
    )

    ro = _pairs(argv, "--ro-bind")
    assert (config, config) in ro and (projects, projects) in ro
    for path in (config, projects):
        for option in _BIND_OPTIONS - {"--ro-bind"}:
            assert (path, path) not in _pairs(argv, option), option
    # After the store, before the root is remounted read-only.
    i_store = argv.index("--bind")
    i_config = next(
        i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == config
    )
    i_projects = next(
        i for i, a in enumerate(argv) if a == "--ro-bind" and argv[i + 1] == projects
    )
    assert argv[i_store + 1] == store
    assert i_store < i_config < i_projects < argv.index("--remount-ro")
    assert argv[i_config : i_projects + 3] == [
        *("--ro-bind", config, config),
        *("--ro-bind", projects, projects),
    ]
    # Nothing else of HOME: the store, config/, projects.json.
    sep = argv.index("--")
    home = str(setup["home"]) + os.sep
    under_home = [
        tuple(argv[i : i + 3])
        for i, a in enumerate(argv[:sep])
        if a in _BIND_OPTIONS and argv[i + 1].startswith(home)
    ]
    assert under_home == [
        ("--bind", store, store),
        ("--ro-bind", config, config),
        ("--ro-bind", projects, projects),
    ]
    assert not [a for a in argv[:sep] if a.startswith(home + ".gemini/projects.json.")]


@pytest.mark.parametrize("remove", [("config",), ("config", "projects")])
def test_agy_not_set_up_is_refused_and_nothing_is_created(setup, fake_binary, remove):
    for name in remove:
        path = setup[name]
        shutil.rmtree(path) if path.is_dir() else path.unlink()

    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))

    assert isinstance(info.value, agy_cage.AgyNotSetUp)
    assert str(info.value) == _NOT_SET_UP
    assert str(setup["home"]) not in str(info.value)
    for name in remove:
        assert not os.path.lexists(setup[name]), "the gateway creates nothing"


@pytest.mark.parametrize("which", ["config", "projects"])
def test_agy_config_behind_a_symbolic_link_is_refused(
    setup, fake_binary, tmp_path, which
):
    """bwrap would follow the link: whatever it names would be shown."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = setup[which]
    shutil.move(str(path), str(elsewhere / path.name))
    path.symlink_to(elsewhere / path.name)

    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))

    assert str(info.value) == (
        f"agy's ~/.gemini/{path.name} goes through a symbolic link"
    )
    assert not isinstance(info.value, agy_cage.AgyNotSetUp)


def test_projects_json_is_optional(setup, fake_binary):
    """BUG-115: ~/.gemini/projects.json is gemini's project registry (agy
    1.4.4 keeps its own in its store, ``cache/projects.json``): missing, the
    cage is built without it, and nothing is created."""
    setup["projects"].unlink()
    argv, _ = _argv(setup, str(fake_binary))

    sep = argv.index("--")
    projects = str(setup["projects"])
    assert projects not in argv[:sep]
    config = str(setup["config"])
    assert (config, config) in _pairs(argv, "--ro-bind")
    assert not os.path.lexists(projects), "the gateway creates nothing"


def test_a_dangling_link_is_refused_as_a_link(setup, fake_binary, tmp_path):
    setup["projects"].unlink()
    setup["projects"].symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))
    assert str(info.value) == (
        "agy's ~/.gemini/projects.json goes through a symbolic link"
    )


@pytest.mark.parametrize("which", ["config", "projects"])
def test_agy_config_of_the_wrong_type_is_refused(setup, fake_binary, which):
    path = setup[which]
    if which == "config":
        shutil.rmtree(path)
        path.write_text("not a folder")
        expected = "agy's ~/.gemini/config is not a directory"
    else:
        path.unlink()
        path.mkdir()
        expected = "agy's ~/.gemini/projects.json is not a regular file"
    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))
    assert str(info.value) == expected


async def test_complete_refuses_agy_not_set_up(setup):
    ran = _fake_agy_recording(setup)
    _install(setup["bin"], "bwrap", f"touch {setup['tmp']}/bwrap-ran\nexit 0\n")
    shutil.rmtree(setup["config"])

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("agy", dict(_BODY))

    assert info.value.sandbox_failed is True
    assert _NOT_SET_UP in str(info.value)
    assert not ran.exists()
    assert not (setup["tmp"] / "bwrap-ran").exists(), "refused before bwrap runs"
    assert not setup["config"].exists()
    assert list(setup["spawn"].iterdir()) == [], "private TMPDIR removed"


def test_missing_bwrap_refuses(setup, fake_binary, monkeypatch):
    monkeypatch.setenv("PATH", str(setup["bin"]))
    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))
    assert str(info.value) == (
        f"bwrap not found on PATH nor at {agy_cage.GATEWAY_BWRAP}"
    )


def test_the_gateway_bwrap_is_at_a_fixed_path_off_path():
    """INC-4: in the image, the gateway's bwrap is not on PATH (codex would
    take it instead of its own); the cage names it in its code, never through
    a variable."""
    assert agy_cage.GATEWAY_BWRAP == "/usr/libexec/plugboard/bwrap"
    source = (ROOT / "plugboard" / "agy_cage.py").read_text(encoding="utf-8")
    assert 'GATEWAY_BWRAP = "/usr/libexec/plugboard/bwrap"' in source


def test_the_fixed_path_is_preferred_to_path(setup, fake_binary, tmp_path, monkeypatch):
    """The image's bwrap (fixed path) wins over any bwrap on PATH."""
    (tmp_path / "libexec").mkdir()
    fixed = _install(tmp_path / "libexec", "bwrap", "exit 0\n")
    monkeypatch.setattr(agy_cage, "GATEWAY_BWRAP", str(fixed))
    on_path = _install(setup["bin"], "bwrap", "exit 0\n")
    assert shutil.which("bwrap") == str(on_path)

    argv, _ = _argv(setup, str(fake_binary))

    assert argv[0] == os.path.realpath(fixed)


def test_without_the_fixed_path_bwrap_is_found_on_path(
    setup, fake_binary, tmp_path, monkeypatch
):
    """Installs outside the image (a workstation, a host): bwrap on PATH, by
    its real path."""
    monkeypatch.setattr(agy_cage, "GATEWAY_BWRAP", str(tmp_path / "absent" / "bwrap"))
    (tmp_path / "real").mkdir()
    target = _install(tmp_path / "real", "bwrap", "exit 0\n")
    (setup["bin"] / "bwrap").symlink_to(target)

    argv, _ = _argv(setup, str(fake_binary))

    assert argv[0] == os.path.realpath(target)


def test_a_fixed_path_that_is_not_executable_is_skipped(
    setup, fake_binary, tmp_path, monkeypatch
):
    fixed = tmp_path / "libexec" / "bwrap"
    fixed.parent.mkdir()
    fixed.write_text("not a program\n")
    fixed.chmod(0o644)
    monkeypatch.setattr(agy_cage, "GATEWAY_BWRAP", str(fixed))
    on_path = _install(setup["bin"], "bwrap", "exit 0\n")

    argv, _ = _argv(setup, str(fake_binary))

    assert argv[0] == os.path.realpath(on_path)


def test_bwrap_is_resolved_once_by_its_real_path(
    setup, fake_binary, tmp_path, monkeypatch
):
    """Like agy's binary (TM-001): a bwrap put on PATH later is not used."""
    real = shutil.which("bwrap")
    if real is None:
        pytest.skip("bwrap not installed")
    links = tmp_path / "links"
    links.mkdir()
    (links / "bwrap").symlink_to(real)
    monkeypatch.setenv("PATH", f"{links}:{os.environ['PATH']}")

    first, _ = _argv(setup, str(fake_binary))
    assert first[0] == os.path.realpath(real)

    _install(setup["bin"], "bwrap", "exit 0\n")
    monkeypatch.setenv("PATH", f"{setup['bin']}:{os.environ['PATH']}")
    second, _ = _argv(setup, str(fake_binary))
    assert second[0] == os.path.realpath(real)


def test_not_linux_refuses(setup, fake_binary, monkeypatch):
    monkeypatch.setattr(agy_cage.sys, "platform", "darwin")
    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))
    assert str(info.value) == "agy sandbox needs Linux"


def test_no_way_to_turn_the_cage_off():
    """No variable, option or fallback launches agy outside bwrap."""
    source = (ROOT / "plugboard" / "agy_cage.py").read_text(encoding="utf-8")
    assert "os.environ" not in source and "getenv" not in source
    # The test-only parameter is never passed by the gateway itself.
    for path in (ROOT / "plugboard").rglob("*.py"):
        if path.name != "agy_cage.py":
            assert "_extra_ro_for_tests" not in path.read_text(encoding="utf-8"), path


@pytest.mark.parametrize(
    ("code", "stdout", "stderr", "expected"),
    [
        (
            1,
            b"",
            b"bwrap: Creating new namespace failed: x\nmore\n",
            "bwrap: Creating new namespace failed: x",
        ),
        (1, b"", b"agy: something else\n", None),
        (1, b'{"status":"ERROR"}', b"bwrap: x\n", None),
        (0, b"", b"bwrap: x\n", None),
    ],
)
def test_setup_failure_is_told_apart(code, stdout, stderr, expected):
    assert agy_cage.setup_failure(code, stdout, stderr) == expected


def test_mode_plan_is_gone():
    """--mode plan was no guard (agy says it has no effect): removed."""
    args = cs.get_backend("agy").args
    assert "--mode" not in args and "plan" not in args
    assert "--sandbox" in args and "--disable-slash-commands" in args


def test_the_full_image_ships_and_checks_bwrap():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    full = dockerfile.split("FROM base AS full", 1)[1]
    installs = [line for line in full.splitlines() if "apt-get install" in line]
    assert any("bubblewrap" in line for line in installs), installs
    check = full.split("Installed CLI binaries", 1)[1].split("\n\n", 1)[0]
    check = check.replace("\n", " ").replace("\\", "")
    # By its fixed path, off PATH (INC-4, tests/test_ci_image_full.py).
    assert f"; {agy_cage.GATEWAY_BWRAP} --version;" in check


# ── No cage, no agy: 503 and nothing launched ───────────────────────────────


async def test_complete_without_bwrap_launches_nothing(setup, monkeypatch):
    ran = _fake_agy_recording(setup)
    monkeypatch.setenv("PATH", str(setup["bin"]))  # agy, no bwrap

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("agy", dict(_BODY))

    assert info.value.sandbox_failed is True
    assert "bwrap not found on PATH" in str(info.value)
    assert not ran.exists()
    assert list(setup["spawn"].iterdir()) == [], "private TMPDIR removed"


async def test_complete_refuses_a_store_behind_a_link(setup, tmp_path):
    ran = _fake_agy_recording(setup)
    shutil.move(str(setup["home"] / ".gemini"), str(tmp_path / "elsewhere"))
    (setup["home"] / ".gemini").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete("agy", dict(_BODY))

    assert info.value.sandbox_failed is True
    assert "symbolic link" in str(info.value)
    assert not ran.exists()


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_complete_when_bwrap_fails_to_set_up(setup, stream):
    ran = _fake_agy_recording(setup)
    _install(
        setup["bin"],
        "bwrap",
        "echo 'bwrap: Creating new namespace failed: Operation not permitted' >&2\n"
        "exit 1\n",
    )
    pieces: list = []

    with pytest.raises(cs.CliSubprocessError) as info:
        await cs.complete(
            "agy", dict(_BODY), on_delta=pieces.append if stream else None
        )

    assert info.value.sandbox_failed is True
    assert "bwrap: Creating new namespace failed" in str(info.value)
    assert not ran.exists()
    assert pieces == []


async def test_list_models_and_version_probe_without_bwrap(setup, monkeypatch):
    ran = _fake_agy_recording(setup)
    monkeypatch.setenv("PATH", str(setup["bin"]))

    assert await cs.list_agy_models() == []
    probe = await cs.probe_cli_version("agy")

    assert probe["ok"] is False
    assert "bwrap not found on PATH" in probe["error"]
    assert not ran.exists()


# ── The chat route's answer ─────────────────────────────────────────────────


def _sandbox_error() -> cs.CliSubprocessError:
    return cs.CliSubprocessError(
        "agy sandbox (bubblewrap) could not start, agy was not run: "
        "bwrap not found on PATH",
        provider_name="agy",
        model_id="gemini-test",
        sandbox_failed=True,
    )


def test_cli_error_response_for_a_client_key():
    from plugboard.app import _cli_error_response

    resp = _cli_error_response(_sandbox_error(), "some-client")
    body = __import__("json").loads(resp.body)

    assert resp.status_code == 503
    assert body["code"] == "provider_sandbox_unavailable"
    assert body["detail"] == CLIENT_DETAIL.format(ref=body["error_ref"])
    assert "bwrap not found" not in resp.body.decode()
    assert "retry-after" not in {k.lower() for k in resp.headers}


def test_cli_error_response_for_the_admin_names_the_cause():
    from plugboard.app import _cli_error_response

    resp = _cli_error_response(_sandbox_error(), "admin")
    body = __import__("json").loads(resp.body)

    assert resp.status_code == 503
    assert body["code"] == "provider_sandbox_unavailable"
    assert "bwrap not found on PATH" in body["detail"]


@pytest.fixture
def client_key(tmp_path, monkeypatch):
    import plugboard.apikeys as ak
    from plugboard import app as app_mod

    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    monkeypatch.setattr(app_mod, "_check_price_known", lambda *a, **k: None)
    rec = ak.create_key("cage-client", "test")
    return {"Authorization": f"Bearer {rec['key']}"}


_AGY_PROVIDER = {
    "id": "pbtest-agy-cli",
    "name": "agy CLI test",
    "base_url": "subprocess://agy",
    "bearer_token": "(via CLI)",
    "default_model": "gemini-test",
    "auth_source": "subprocess:agy",
    "is_active": True,
    "created_at": "2026-01-01T00:00:00+00:00",
}


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_chat_without_bwrap_answers_503(
    setup, client, client_key, _providers_file, monkeypatch, stream
):
    """The real route, router and CLI wrapper: agy is the only route."""
    import json

    import plugboard.router as router

    ran = _fake_agy_recording(setup)
    monkeypatch.setenv("PATH", str(setup["bin"]))
    _providers_file.write_text(json.dumps({"providers": [_AGY_PROVIDER]}))
    router.invalidate_models_cache()

    resp = await client.post(
        "/v1/chat/completions",
        headers=client_key,
        json={
            "model": "pbtest-agy-cli:gemini-test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
            "cache": False,
        },
    )

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "provider_sandbox_unavailable"
    assert body["detail"] == CLIENT_DETAIL.format(ref=body["error_ref"])
    assert not ran.exists()


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "stream"])
async def test_chat_with_agy_not_set_up_answers_503(
    setup, client, client_key, _providers_file, monkeypatch, stream
):
    """agy never run on this account (no ~/.gemini/config): 503, the cause
    for the admin only, nothing launched, nothing created."""
    import json

    import plugboard.router as router

    ran = _fake_agy_recording(setup)
    _install(setup["bin"], "bwrap", f"touch {setup['tmp']}/bwrap-ran\nexit 0\n")
    monkeypatch.setenv("PATH", str(setup["bin"]))
    shutil.rmtree(setup["config"])
    setup["projects"].unlink()
    _providers_file.write_text(json.dumps({"providers": [_AGY_PROVIDER]}))
    router.invalidate_models_cache()

    resp = await client.post(
        "/v1/chat/completions",
        headers=client_key,
        json={
            "model": "pbtest-agy-cli:gemini-test",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": stream,
            "cache": False,
        },
    )

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["code"] == "provider_sandbox_unavailable"
    assert body["detail"] == CLIENT_DETAIL.format(ref=body["error_ref"])
    assert ".gemini" not in resp.text and "not set up" not in resp.text
    assert not ran.exists() and not (setup["tmp"] / "bwrap-ran").exists()
    assert not setup["config"].exists() and not setup["projects"].exists()


def test_agy_not_set_up_names_the_cause_for_the_admin():
    from plugboard.app import _cli_error_response

    cause = _NOT_SET_UP
    resp = _cli_error_response(cs.agy_sandbox_error(cause, "gemini-test"), "admin")

    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["code"] == "provider_sandbox_unavailable"
    assert cause in body["detail"]


# ── Router: a sandbox failure is treated like a timeout ─────────────────────


async def test_sandbox_failure_hands_over_without_quarantine(
    client, client_key, _providers_file, monkeypatch
):
    """Like a timeout: the next route answers, and the model is never
    quarantined, even when bwrap's message reads like an unknown model."""
    import json

    from aioresponses import aioresponses

    import plugboard.model_quarantine as mq
    import plugboard.router as router
    from tests.factories import FAKE_COMPLETION, PROVIDER_A

    _providers_file.write_text(
        json.dumps({"providers": [{**_AGY_PROVIDER}, PROVIDER_A]})
    )
    router.invalidate_models_cache()
    monkeypatch.setattr(
        router.metrics,
        "get_stats",
        lambda: {
            "pbtest-agy-cli": {"latency_avg": 0.1, "requests": 100, "errors": 0},
            "test-provider": {"latency_avg": 2.0, "requests": 100, "errors": 0},
        },
    )
    calls: list = []

    async def failing(kind, body, **_kw):
        calls.append(kind)
        raise cs.CliSubprocessError(
            "agy sandbox (bubblewrap) could not start, agy was not run: "
            "bwrap: Can't find source path /x: model_not_found does not exist",
            provider_name="agy",
            model_id=body.get("model", ""),
            sandbox_failed=True,
        )

    monkeypatch.setattr(cs, "complete", failing)

    # Recorded, not raised: the router logs and swallows a quarantine error.
    quarantined: list = []
    monkeypatch.setattr(
        mq, "record_terminal_unavailable", lambda *a, **k: quarantined.append(a)
    )
    monkeypatch.setattr(
        mq, "record_account_unsupported", lambda *a, **k: quarantined.append(a)
    )

    with aioresponses() as mock:
        mock.post(
            "http://fake-llm.test/v1/chat/completions",
            payload=FAKE_COMPLETION,
            status=200,
        )
        resp = await client.post(
            "/v1/chat/completions",
            headers=client_key,
            json={
                "model": "fake:model-v1",
                "messages": [{"role": "user", "content": "hi"}],
                "cache": False,
            },
        )

    assert calls == ["agy"], "the agy route was tried first"
    assert resp.status_code == 200, resp.text
    assert quarantined == [], "a sandbox failure never quarantines a model"


def test_a_streamed_route_hands_over_on_a_sandbox_failure():
    from plugboard.router import _request_caused, _stream_hands_over

    exc = _sandbox_error()
    assert _stream_hands_over(exc) is True
    assert _request_caused(exc) is False


async def test_streamed_sandbox_failure_is_never_quarantined(monkeypatch):
    import plugboard.model_quarantine as mq
    import plugboard.router as router

    async def failing(kind, body, **_kw):
        raise cs.CliSubprocessError(
            "agy sandbox (bubblewrap) could not start: model is not available",
            provider_name="agy",
            model_id="m",
            sandbox_failed=True,
        )

    monkeypatch.setattr(cs, "complete", failing)
    monkeypatch.setattr(cs, "can_stream", lambda kind: False)

    # Recorded, not raised: the router logs and swallows a quarantine error.
    quarantined: list = []
    monkeypatch.setattr(
        mq, "record_terminal_unavailable", lambda *a, **k: quarantined.append(a)
    )
    monkeypatch.setattr(
        mq, "record_account_unsupported", lambda *a, **k: quarantined.append(a)
    )

    gen = router._stream_route(
        {"messages": [{"role": "user", "content": "hi"}]},
        dict(_AGY_PROVIDER),
        "m",
        5.0,
    )
    with pytest.raises(cs.CliSubprocessError) as info:
        await gen.__anext__()
    assert info.value.sandbox_failed is True
    assert quarantined == [], "a sandbox failure never quarantines a model"


# ── Integration: the real bwrap around a fake agy ───────────────────────────


def _probe_script(setup) -> str:
    """Shell run as agy inside the cage; one ``name=yes|no`` line per check,
    written to the store (one of the two places it may write)."""
    home = setup["home"]
    checks = {
        "see_app_data": f"ls {setup['app_data']}",
        "see_claude": f"ls {home}/.claude",
        "see_codex": f"ls {home}/.codex",
        "see_usr_local": "ls /usr/local",
        "see_gateway_proc": f"test -e /proc/{os.getpid()}",
        # agy is pid 1 of the cage's own pid namespace (--as-pid-1).
        "agy_is_pid1": 'test "$$" = 1 && grep -q agy /proc/1/cmdline',
        "write_home": f"touch {home}/x",
        "write_bin_dir": f"touch {setup['bin']}/agy.new",
        "overwrite_binary": f"echo x >> {setup['bin']}/agy",
        "write_spawn": f"touch {setup['spawn']}/x",
        "write_tmp": "touch /tmp/x",
        "write_usr": "touch /usr/x",
        "write_tmpdir": 'touch "$TMPDIR/x"',
        # agy's config, outside its store: readable, never writable.
        "read_config": f"cat {home}/.gemini/config/mcp_config.json",
        "read_projects": f"cat {home}/.gemini/projects.json",
        "write_mcp_config": f"echo x >> {home}/.gemini/config/mcp_config.json",
        "write_config": f"touch {home}/.gemini/config/x",
        "write_config_projects": f"mkdir {home}/.gemini/config/projects/x",
        "write_projects": f"echo x >> {home}/.gemini/projects.json",
        "write_gemini": f"touch {home}/.gemini/x",
        "token_read": "grep -q old-token antigravity-oauth-token",
        "token_rewrite": (
            "cp antigravity-oauth-token token.tmp && echo refreshed >> token.tmp"
            " && mv token.tmp antigravity-oauth-token"
        ),
    }
    lines = ['START="$PWD"', f'cd "{setup["store"]}"', "{"]
    for name, cmd in checks.items():
        lines.append(
            f"if {cmd} >/dev/null 2>&1; then echo {name}=yes; else echo {name}=no; fi"
        )
    lines.append('echo "cwd=$START"')
    lines.append("} > report.part")
    lines.append("env > env.txt")
    lines.append("cat /proc/1/environ > proc1-environ.txt 2>/dev/null")
    lines.append("mv report.part report.txt")
    return "\n".join(lines) + "\n"


async def test_agy_in_the_real_cage(setup, real_bwrap, shell_in_cage, monkeypatch):
    for name, value in _SECRETS.items():
        monkeypatch.setenv(name, value)
    ran = _fake_agy_recording(setup, _probe_script(setup))
    binary = setup["bin"] / "agy"
    before = binary.read_bytes()
    token = setup["store"] / "antigravity-oauth-token"
    token_inode = token.stat().st_ino

    result = await cs.complete("agy", dict(_BODY))

    assert result["choices"][0]["message"]["content"] == "OK"
    assert not ran.exists(), "the test's tmp dir is out of the cage"
    report = dict(
        line.split("=", 1)
        for line in (setup["store"] / "report.txt").read_text().splitlines()
    )
    assert report.pop("cwd").startswith(str(setup["spawn"] / "agy-tmp-"))
    assert report == {
        "see_app_data": "no",
        "see_claude": "no",
        "see_codex": "no",
        "see_usr_local": "no",
        "see_gateway_proc": "no",
        "agy_is_pid1": "yes",
        "write_home": "no",
        "write_bin_dir": "no",
        "overwrite_binary": "no",
        "write_spawn": "no",
        "write_tmp": "no",
        "write_usr": "no",
        "write_tmpdir": "yes",
        "read_config": "yes",
        "read_projects": "yes",
        "write_mcp_config": "no",
        "write_config": "no",
        "write_config_projects": "no",
        "write_projects": "no",
        "write_gemini": "no",
        "token_read": "yes",
        "token_rewrite": "yes",
    }
    assert (setup["config"] / "mcp_config.json").read_text() == "{}\n"
    assert setup["projects"].read_text() == "{}\n"
    assert sorted(p.name for p in setup["config"].iterdir()) == [
        "mcp_config.json",
        "projects",
    ]
    assert list((setup["config"] / "projects").iterdir()) == []
    # The token was rewritten by rename, in the real store.
    assert token.read_text() == "old-token\nrefreshed\n"
    assert token.stat().st_ino != token_inode
    # Nothing written outside: the binary, HOME, the launch directory.
    assert binary.read_bytes() == before
    assert not (setup["bin"] / "agy.new").exists()
    assert not (setup["home"] / "x").exists()
    assert list(setup["spawn"].iterdir()) == []
    # The child's environment holds none of the server's secrets.
    child_env = (setup["store"] / "env.txt").read_text()
    proc1_env = (
        (setup["store"] / "proc1-environ.txt").read_bytes().decode("utf-8", "replace")
    )
    for name, value in _SECRETS.items():
        for seen in (child_env, proc1_env):
            assert value not in seen, name
            assert f"{name}=" not in seen, name
    assert "PLUGBOARD_" not in child_env


async def test_list_models_and_version_run_in_the_cage(
    setup, real_bwrap, shell_in_cage
):
    """`agy models` and `agy --version` run inside the cage too: the fake
    agy can only list models when it cannot see the test's tmp dir."""
    _install(
        setup["bin"],
        "agy",
        f"if ls {setup['app_data']} >/dev/null 2>&1; then echo visible >&2; exit 3; fi\n"
        'case "$1" in\n'
        "  models) printf 'gemini-a\\tGemini A\\n' ;;\n"
        "  --version) echo 'agy 9.9.9' ;;\n"
        "esac\n",
    )

    assert await cs.list_agy_models() == ["gemini-a"]
    probe = await cs.probe_cli_version("agy")

    assert probe == {"ok": True, "version": "agy 9.9.9", "error": None}
    assert list(setup["spawn"].iterdir()) == []


# ── /proc where the container forbids a new procfs ──────────────────────────
#
# Docker masks paths of the container's /proc (/proc/kcore, /proc/keys...) by
# mounting over them; the kernel then refuses a new procfs in a user
# namespace: ``bwrap: Can't mount proc on /newroot/proc: Operation not
# permitted``. Like codex's sandbox, the cage checks once, with bwrap itself
# as the command (never agy), and goes without /proc when that is refused.
# It never binds the host's /proc: the cage would read the gateway's environ.

_PROC_REFUSED = "bwrap: Can't mount proc on /newroot/proc: Operation not permitted"


def _fake_bwrap(setup, precheck: str) -> tuple:
    """A fake bwrap recording each call (one argument per line, then
    ``--END--``); ``precheck`` is its answer to ``... -- <bwrap> --version``."""
    log = setup["tmp"] / "bwrap-calls"
    answers = {
        "ok": "echo 'bubblewrap 0.9.0'; exit 0",
        "refused": f'echo "{_PROC_REFUSED}" >&2; exit 1',
        "other": "echo 'bwrap: setting up uid map: Permission denied' >&2; exit 1",
    }
    bwrap = _install(
        setup["bin"],
        "bwrap",
        f"printf '%s\\n' \"$@\" --END-- >> {log}\n"
        'for last; do :; done\nif [ "$last" = "--version" ]; then\n'
        f"  {answers[precheck]}\nfi\nexit 0\n",
    )
    return bwrap, log


def _calls(log: Path) -> list:
    if not log.exists():
        return []
    calls, current = [], []
    for line in log.read_text().splitlines():
        if line == "--END--":
            calls.append(current)
            current = []
        else:
            current.append(line)
    return calls


def test_the_proc_check_runs_bwrap_itself_never_agy(setup, fake_binary):
    bwrap, log = _fake_bwrap(setup, "ok")

    argv, _ = _argv(setup, str(fake_binary))

    (check,) = _calls(log)
    assert str(fake_binary) not in check
    sep = check.index("--")
    assert check[sep + 1 :] == [str(bwrap), "--version"]
    assert ("--ro-bind", str(bwrap), str(bwrap)) in [
        tuple(check[i : i + 3]) for i, a in enumerate(check[:sep]) if a == "--ro-bind"
    ]
    # The cage's namespaces and system binds, with a new /proc.
    head = argv[1 : argv.index("--proc")]
    assert check[: len(head)] == head
    assert ("--proc", "/proc") in [tuple(check[i : i + 2]) for i in range(sep)]
    assert ("--proc", "/proc") in [tuple(argv[i : i + 2]) for i in range(len(argv))]


def test_the_proc_check_runs_once_per_process(setup, fake_binary):
    _, log = _fake_bwrap(setup, "ok")

    for _ in range(3):
        argv, _ = _argv(setup, str(fake_binary))
        assert "--proc" in argv

    assert len(_calls(log)) == 1


def test_the_cage_goes_without_proc_where_the_container_forbids_it(
    setup, fake_binary, caplog
):
    _, log = _fake_bwrap(setup, "refused")

    with caplog.at_level("WARNING", logger="plugboard.agy_cage"):
        first, _ = _argv(setup, str(fake_binary))
        second, _ = _argv(setup, str(fake_binary))

    for argv in (first, second):
        assert "--proc" not in argv
        # Its own pid namespace still: the rest of agy's tree dies with it.
        assert "--unshare-pid" in argv
        assert not [a for a in argv if a == "/proc" or a.startswith("/proc/")]
    assert len(_calls(log)) == 1, "checked once per process"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "without /proc" in warnings[0].getMessage()
    assert str(setup["tmp"]) not in warnings[0].getMessage()


def test_any_other_check_failure_keeps_proc(setup, fake_binary):
    """Not a procfs refusal: the cage keeps /proc (the call then fails with
    the usual 503), and the next call checks again."""
    _, log = _fake_bwrap(setup, "other")

    for _ in range(2):
        argv, _ = _argv(setup, str(fake_binary))
        assert ("--proc", "/proc") in [tuple(argv[i : i + 2]) for i in range(len(argv))]

    assert len(_calls(log)) == 2


def test_a_hanging_proc_check_is_cut_and_not_repeated(setup, fake_binary, monkeypatch):
    """bwrap hanging, with a child out of its session holding its stderr:
    the check is cut at its delay (it runs before agy's call, on the
    gateway's event loop), kills what it started, keeps /proc and does not
    run again."""
    monkeypatch.setattr(agy_cage, "_PROC_CHECK_TIMEOUT_S", 1.0)
    log = setup["tmp"] / "bwrap-calls"
    pids = setup["tmp"] / "bwrap-pids"
    _install(
        setup["bin"],
        "bwrap",
        f"echo call >> {log}\n"
        "trap '' TERM\n"
        "setsid sleep 8.4242 &\n"
        f'echo "$$ $!" > {pids}\n'
        "sleep 8.4343\n",
    )
    try:
        started = time.monotonic()
        first, _ = _argv(setup, str(fake_binary))
        elapsed = time.monotonic() - started
        second, _ = _argv(setup, str(fake_binary))

        assert elapsed < 4.0, f"the check held the caller {elapsed:.1f}s"
        for argv in (first, second):
            assert ("--proc", "/proc") in [
                tuple(argv[i : i + 2]) for i in range(len(argv))
            ]
        assert log.read_text().count("call") == 1
        shell = int(pids.read_text().split()[0])
        with pytest.raises(ProcessLookupError):
            for _ in range(20):
                os.kill(shell, 0)
                time.sleep(0.1)
    finally:
        for marker in (b"8.4242", b"8.4343"):
            for pid in _processes_with(marker):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)


_BIND_OPTIONS = {
    f"{o}{t}"
    for o in ("--bind", "--ro-bind", "--dev-bind", "--bind-data", "--ro-bind-data")
    for t in ("", "-try")
}


@pytest.mark.parametrize("precheck", ["ok", "refused", "other"])
def test_no_command_line_binds_the_hosts_proc(setup, fake_binary, precheck):
    """The host's /proc in the cage would show the gateway's environ (same
    uid): only a procfs of the cage's own pid namespace, or none."""
    _, log = _fake_bwrap(setup, precheck)

    argv, _ = _argv(setup, str(fake_binary))

    for line in [argv, *_calls(log)]:
        sep = line.index("--")
        for i, arg in enumerate(line[:sep]):
            if arg in _BIND_OPTIONS:
                for path in line[i + 1 : i + 3]:
                    assert path != "/proc" and not path.startswith("/proc/"), line
            if arg == "--symlink":
                assert not line[i + 1].startswith("/proc"), line


# The simulation of a Docker container: an outer bwrap whose /proc has a path
# masked the way Docker does it, then the gateway's code inside it.


def _outer(bwrap: str, inner: list) -> list:
    return [
        *(bwrap, "--unshare-user", "--dev-bind", "/", "/"),
        *("--ro-bind", "/dev/null", "/proc/kcore", "--", *inner),
    ]


@functools.lru_cache(maxsize=1)
def _docker_like_unusable_reason() -> str:
    """Empty when the host reproduces Docker's refusal of a new procfs."""
    reason = _bwrap_unusable_reason()
    if reason:
        return reason
    if not os.path.exists("/proc/kcore"):
        return "no /proc/kcore to mask"
    bwrap = shutil.which("bwrap")
    inner = [bwrap, "--unshare-user", "--unshare-pid", "--ro-bind", "/usr", "/usr"]
    for path in ("/bin", "/lib", "/lib64"):
        if os.path.islink(path):
            inner += ["--symlink", os.readlink(path), path]
        elif os.path.isdir(path):
            inner += ["--ro-bind", path, path]
    inner += ["--dev", "/dev"]
    true = shutil.which("true") or "/bin/true"
    try:
        bare = subprocess.run(
            _outer(bwrap, [*inner, "--", true]), capture_output=True, timeout=20
        )
        with_proc = subprocess.run(
            _outer(bwrap, [*inner, "--proc", "/proc", "--", true]),
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{type(exc).__name__}: {exc}"
    if bare.returncode != 0:
        return "nested bwrap unusable: " + bare.stderr.decode("utf-8", "replace")[:200]
    if with_proc.returncode == 0 or b"Can't mount proc" not in with_proc.stderr:
        return "a masked /proc does not forbid a new procfs on this host"
    return ""


@pytest.fixture
def docker_like(real_bwrap):
    reason = _docker_like_unusable_reason()
    if reason:
        pytest.skip(f"Docker-like /proc not reproducible: {reason}")
    return shutil.which("bwrap")


# Run inside the outer bwrap: the gateway's complete() with a fake agy (a
# shell, hence the test-only binds) that reports what it sees of /proc.
_GATEWAY_IN_CONTAINER = r"""
import asyncio, functools, json, os, sys

from plugboard import agy_cage
from plugboard import cli_subprocess as cs

p = json.loads(sys.argv[1])
cs._SPAWN_CWD_CACHE = p["spawn"]
agy_cage.cage_argv = functools.partial(
    agy_cage.cage_argv, _extra_ro_for_tests=tuple(p["shell_ro"])
)
agy = os.path.join(p["bin"], "agy")
with open(agy, "w", encoding="utf-8") as handle:
    handle.write(p["agy"].replace("@GATEWAY_PID@", str(os.getpid())))
os.chmod(agy, 0o755)
body = {"model": "gemini-test", "messages": [{"role": "user", "content": "hi"}]}
try:
    result = asyncio.run(cs.complete("agy", body))
    print(json.dumps({"content": result["choices"][0]["message"]["content"]}))
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__ + ": " + str(exc)}))
"""

_AGY_SEEING_PROC = (
    "#!/bin/sh\ncat >/dev/null\n"
    "if test -d /proc; then d=yes; else d=no; fi\n"
    "if cat /proc/1/environ >/dev/null 2>&1; then e=yes; else e=no; fi\n"
    "if cat /proc/@GATEWAY_PID@/environ >/dev/null 2>&1; then g=yes; else g=no; fi\n"
    "if env | grep -q -e adm-secret -e api-secret -e enc-secret -e pb-secret; "
    "then s=yes; else s=no; fi\n"
    'R="proc=$d proc1_environ=$e gateway_environ=$g secret=$s pid=$$"\n'
    'echo "{\\"status\\":\\"SUCCESS\\",\\"response\\":\\"$R\\",\\"usage\\":{}}"\n'
)


def test_agy_runs_where_the_container_forbids_a_new_procfs(
    setup, docker_like, tmp_path
):
    script = tmp_path / "gateway.py"
    script.write_text(_GATEWAY_IN_CONTAINER, encoding="utf-8")
    params = {
        "spawn": str(setup["spawn"]),
        "bin": str(setup["bin"]),
        "shell_ro": list(_SHELL_RO),
        "agy": _AGY_SEEING_PROC,
    }
    env = {
        **os.environ,
        "HOME": str(setup["home"]),
        "PATH": f"{setup['bin']}:/usr/bin:/bin",
        "PYTHONPATH": str(ROOT),
        **_SECRETS,
    }
    env.pop("PLUGBOARD_SUBPROCESS_CWD", None)

    proc = subprocess.run(
        _outer(docker_like, [sys.executable, str(script), json.dumps(params)]),
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    # No /proc at all, so no environ to read; still a pid namespace of its
    # own (agy is its pid 1, --as-pid-1); and no secret in its environment.
    assert out == {
        "content": "proc=no proc1_environ=no gateway_environ=no secret=no pid=1"
    }, proc.stderr
    assert "without /proc" in proc.stderr
    for value in _SECRETS.values():
        assert value not in proc.stdout + proc.stderr
    assert list(setup["spawn"].iterdir()) == []


def test_cage_argv_binds_the_loader_cache(setup, fake_binary):
    """glibc's loader finds the libraries through /etc/ld.so.cache; without
    it, it only looks in the default folders."""
    argv, _ = _argv(setup, str(fake_binary))
    assert ("/etc/ld.so.cache", "/etc/ld.so.cache") in _pairs(argv, "--ro-bind-try")


# ── agy's self-update stays in the cage ─────────────────────────────────────
#
# agy updates itself by replacing its own executable in place (on a host:
# ``agy`` next to ``agy.<number>.old``), from a program it starts in the
# background (``--bg-updater``), and records the outcome in its store
# (``updater/update_status.json``). Three layers: the variable that turns the
# updater off, a read-only binary and folder, a pid namespace that ends with
# the call.


def test_every_agy_launch_turns_the_self_update_off(monkeypatch):
    monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", "0")
    monkeypatch.setenv("PLUGBOARD_SUBPROCESS_ENV_PASSTHROUGH", "")
    assert cs.build_child_env("agy")["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"


@pytest.mark.parametrize("kind", ["gemini", "codex", "claude"])
def test_other_clis_do_not_get_the_agy_variable(monkeypatch, kind):
    monkeypatch.delenv("AGY_CLI_DISABLE_AUTO_UPDATE", raising=False)
    assert "AGY_CLI_DISABLE_AUTO_UPDATE" not in cs.build_child_env(kind)


def _answer(text: str) -> str:
    """A fake agy's last line: its JSON answer carrying ``text``."""
    return f'echo "{{\\"status\\":\\"SUCCESS\\",\\"response\\":\\"{text}\\",\\"usage\\":{{}}}}"\n'


def _content(result) -> str:
    return result["choices"][0]["message"]["content"]


async def test_agy_sees_the_self_update_turned_off(setup, real_bwrap, shell_in_cage):
    _install(
        setup["bin"],
        "agy",
        "cat >/dev/null\n" + _answer("disable=${AGY_CLI_DISABLE_AUTO_UPDATE-unset}"),
    )

    result = await cs.complete("agy", dict(_BODY))

    assert _content(result) == "disable=1"


async def test_agy_cannot_replace_its_own_binary(setup, real_bwrap, shell_in_cage):
    """What agy's updater does (current binary renamed ``.old``, the new one
    written next to it or moved over it) fails; the binary is intact."""
    body = (
        "cat >/dev/null\n"
        'self="$0"\n'
        'if mv "$self" "$self.4242.old" 2>/dev/null; then r=yes; else r=no; fi\n'
        "if printf '#!/bin/sh\\necho new\\n' > \"$self.new\" 2>/dev/null; "
        "then w=yes; else w=no; fi\n"
        "if printf '#!/bin/sh\\necho new\\n' > \"$TMPDIR/agy.next\" "
        '&& mv -f "$TMPDIR/agy.next" "$self" 2>/dev/null; then m=yes; else m=no; fi\n'
        + _answer("rename=$r write_next=$w move_over=$m")
    )
    binary = _install(setup["bin"], "agy", body)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()

    result = await cs.complete("agy", dict(_BODY))

    assert _content(result) == "rename=no write_next=no move_over=no"
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == digest
    assert sorted(p.name for p in setup["bin"].iterdir()) == ["agy"], "no .old"


def _processes_with(marker: bytes) -> list:
    """Pids, host-wide, whose command line holds ``marker``."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                if marker in fh.read():
                    found.append(int(entry))
        except OSError:
            pass
    return found


async def test_a_background_updater_ends_with_the_call(
    setup, real_bwrap, shell_in_cage
):
    """A program agy leaves running in the background, even in a session of
    its own, dies with the cage's pid namespace when agy exits."""
    markers = (b"37.4242", b"37.4343")
    _install(
        setup["bin"],
        "agy",
        "cat >/dev/null\n"
        "setsid sleep 37.4242 </dev/null >/dev/null 2>&1 &\n"
        "sleep 37.4343 &\n" + _answer("OK"),
    )
    try:
        result = await asyncio.wait_for(cs.complete("agy", dict(_BODY)), 20)

        assert _content(result) == "OK"
        left = {m: _processes_with(m) for m in markers}
        for _ in range(30):
            if not any(left.values()):
                break
            await asyncio.sleep(0.1)
            left = {m: _processes_with(m) for m in markers}
        assert left == {m: [] for m in markers}
    finally:
        for marker in markers:
            for pid in _processes_with(marker):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)


async def test_agy_records_its_update_state_in_its_store(
    setup, real_bwrap, shell_in_cage
):
    state = '{"status":"Update failed"}'
    _install(
        setup["bin"],
        "agy",
        "cat >/dev/null\n"
        'u="$HOME/.gemini/antigravity-cli/updater"\n'
        f"mkdir -p \"$u\" && printf '%s' '{state}' > \"$u/update_status.json\" "
        '&& touch "$u/last_check.timestamp"\n' + _answer("OK"),
    )

    result = await cs.complete("agy", dict(_BODY))

    assert _content(result) == "OK"
    updater = setup["store"] / "updater"
    assert (updater / "update_status.json").read_text() == state
    assert (updater / "last_check.timestamp").exists()


# ── agy's settings in its store are read-only (BUG-108) ─────────────────────
#
# The store stays writable (conversations, summaries, caches, logs, the
# sign-in token agy refreshes), but what agy keeps there and never changes
# at a call is shown read-only, each entry at its own path when it exists:
# its settings (``settings.json``: permission rules), its identity
# (``installation_id``, ``jetski_state.pbtxt``) and the programs and skills
# it unpacks (``bin/``, ``builtin/``). The gateway creates none of them.

_STORE_SETTINGS = {
    "settings.json": "{}\n",
    "installation_id": "00000000-test-install\n",
    "jetski_state.pbtxt": "state: 1\n",
}
_STORE_FOLDERS = {"bin": "agentapi", "builtin": ".manifest"}


def _store_settings(setup) -> dict:
    """agy's settings in the test store, as after its first runs."""
    store = setup["store"]
    for name, content in _STORE_SETTINGS.items():
        (store / name).write_text(content)
    for name, inside in _STORE_FOLDERS.items():
        (store / name).mkdir()
        (store / name / inside).write_text(f"{name}-content\n")
    return {name: str(store / name) for name in (*_STORE_SETTINGS, *_STORE_FOLDERS)}


def test_the_read_only_entries_of_the_store_are_named():
    assert agy_cage.STORE_READ_ONLY == (
        "settings.json",
        "installation_id",
        "jetski_state.pbtxt",
        "bin",
        "builtin",
    )
    # The sign-in token stays writable: agy refreshes it.
    assert "antigravity-oauth-token" not in agy_cage.STORE_READ_ONLY


def test_cage_argv_shows_agy_its_settings_read_only(setup, fake_binary):
    paths = _store_settings(setup)
    argv, tmpdir = _argv(setup, str(fake_binary))
    store = str(setup["store"])
    token = os.path.join(store, "antigravity-oauth-token")

    # Still writable: the store and the private TMPDIR only.
    assert _pairs(argv, "--bind") == [(store, store), (tmpdir, tmpdir)]
    i_store = argv.index("--bind")
    i_remount = argv.index("--remount-ro")
    expected = []
    for name in agy_cage.STORE_READ_ONLY:
        expected += ["--ro-bind", paths[name], paths[name]]
    i_first = i_store + 3
    # Right after the store, in this order, before the root is remounted.
    assert argv[i_first : i_first + len(expected)] == expected
    assert i_first + len(expected) < i_remount
    # The token is never bound by itself (read-only or otherwise).
    sep = argv.index("--")
    assert token not in argv[:sep]


def test_missing_settings_are_skipped_and_never_created(setup, fake_binary):
    argv, _ = _argv(setup, str(fake_binary))
    sep = argv.index("--")
    for name in agy_cage.STORE_READ_ONLY:
        path = str(setup["store"] / name)
        assert path not in argv[:sep]
        assert not os.path.lexists(path), "the gateway creates nothing"


@pytest.mark.parametrize("name", list(_STORE_SETTINGS) + list(_STORE_FOLDERS))
def test_a_setting_behind_a_symbolic_link_is_refused(
    setup, fake_binary, tmp_path, name
):
    """bwrap would follow the link: whatever it names would be shown."""
    _store_settings(setup)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = setup["store"] / name
    shutil.move(str(path), str(elsewhere / name))
    path.symlink_to(elsewhere / name)

    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))

    assert str(info.value) == f"agy's store entry {name} goes through a symbolic link"
    assert str(setup["home"]) not in str(info.value)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("settings.json", "is not a regular file"),
        ("installation_id", "is not a regular file"),
        ("bin", "is not a directory"),
        ("builtin", "is not a directory"),
    ],
)
def test_a_setting_of_the_wrong_type_is_refused(setup, fake_binary, name, expected):
    _store_settings(setup)
    path = setup["store"] / name
    if path.is_dir():
        shutil.rmtree(path)
        path.write_text("not a folder")
    else:
        path.unlink()
        path.mkdir()
    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))
    assert str(info.value) == f"agy's store entry {name} {expected}"


def test_a_setting_with_another_hard_link_is_refused(setup, fake_binary):
    """A second name of the same file, in the writable store, would change
    it behind its read-only mount."""
    _store_settings(setup)
    os.link(setup["store"] / "settings.json", setup["store"] / "elsewhere.json")
    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))
    assert str(info.value) == "agy's store entry settings.json has another hard link"


def _settings_probe(store: Path) -> str:
    """Shell run as agy: tries to change each setting in every way, and to
    do what agy does at each call (conversation, token refresh)."""
    checks = {
        "append_settings": "echo x >> settings.json",
        "replace_settings": "echo '{}' > new.json && mv -f new.json settings.json",
        "remove_settings": "rm -f settings.json && test ! -e settings.json",
        "link_settings": "ln settings.json settings.link",
        "append_installation_id": "echo x >> installation_id",
        "append_jetski_state": "echo x >> jetski_state.pbtxt",
        "write_bin": "touch bin/agentapi.new",
        "overwrite_bin": "echo x >> bin/agentapi",
        "remove_builtin": "rm -rf builtin && test ! -e builtin",
        "write_builtin": "touch builtin/.checksum",
        "write_conversation": (
            "mkdir -p conversations && echo c > conversations/c0ffee.db"
            " && rm conversations/c0ffee.db"
        ),
        "write_summaries": "echo s > conversation_summaries.db",
        "write_cache": "mkdir -p cache && echo l > cache/last_conversations.json",
        "token_rewrite": (
            "cp antigravity-oauth-token token.tmp && echo refreshed >> token.tmp"
            " && mv token.tmp antigravity-oauth-token"
        ),
    }
    lines = [f'cd "{store}"', "cat >/dev/null", "{"]
    for name, cmd in checks.items():
        lines.append(
            f"if ( {cmd} ) >/dev/null 2>&1; then echo {name}=yes; else echo {name}=no; fi"
        )
    lines.append("} > report.part")
    lines.append("mv report.part report.txt")
    return "\n".join(lines) + "\n" + _answer("OK")


async def test_agy_cannot_change_its_settings_in_the_real_cage(
    setup, real_bwrap, shell_in_cage
):
    paths = _store_settings(setup)
    before = {
        name: (
            Path(path).read_text()
            if Path(path).is_file()
            else sorted(p.name for p in Path(path).iterdir())
        )
        for name, path in paths.items()
    }
    _install(setup["bin"], "agy", _settings_probe(setup["store"]))

    result = await cs.complete("agy", dict(_BODY))

    assert _content(result) == "OK"
    report = dict(
        line.split("=", 1)
        for line in (setup["store"] / "report.txt").read_text().splitlines()
    )
    assert report == {
        "append_settings": "no",
        "replace_settings": "no",
        "remove_settings": "no",
        "link_settings": "no",
        "append_installation_id": "no",
        "append_jetski_state": "no",
        "write_bin": "no",
        "overwrite_bin": "no",
        "remove_builtin": "no",
        "write_builtin": "no",
        "write_conversation": "yes",
        "write_summaries": "yes",
        "write_cache": "yes",
        "token_rewrite": "yes",
    }
    after = {
        name: (
            Path(path).read_text()
            if Path(path).is_file()
            else sorted(p.name for p in Path(path).iterdir())
        )
        for name, path in paths.items()
    }
    assert after == before
    token = setup["store"] / "antigravity-oauth-token"
    assert token.read_text() == "old-token\nrefreshed\n"
    assert not (setup["store"] / "settings.link").exists()
