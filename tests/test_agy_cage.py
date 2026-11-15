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
import os
import shutil
import signal
import stat
import subprocess
import sys
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
    """A throwaway HOME (agy's store holds a sign-in token), the private
    launch directory, a ``bin`` holding the fake agy, and PATH on it."""
    home = tmp_path / "home"
    store = home / ".gemini" / "antigravity-cli"
    store.mkdir(parents=True)
    (store / "antigravity-oauth-token").write_text("old-token\n")
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
    return {
        "home": home,
        "store": store,
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


def test_cage_argv_binds_the_ssl_variables_when_they_exist(
    setup, fake_binary, tmp_path
):
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    argv, _ = _argv(
        setup,
        str(fake_binary),
        env={"SSL_CERT_FILE": str(bundle), "SSL_CERT_DIR": "/nonexistent/certs"},
    )
    assert (str(bundle), str(bundle)) in _pairs(argv, "--ro-bind-try")
    assert not any("/nonexistent/certs" in a for a in argv)


def test_store_is_created_private_when_missing(setup, fake_binary):
    shutil.rmtree(setup["home"] / ".gemini")
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


def test_missing_bwrap_refuses(setup, fake_binary, monkeypatch):
    monkeypatch.setenv("PATH", str(setup["bin"]))
    with pytest.raises(agy_cage.CageUnavailable) as info:
        _argv(setup, str(fake_binary))
    assert str(info.value) == "bwrap not found on PATH"


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
    assert "; bwrap --version;" in check.replace("\n", " ").replace("\\", "")


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
        "proc1_is_cage": "grep -q bwrap /proc/1/cmdline",
        "write_home": f"touch {home}/x",
        "write_bin_dir": f"touch {setup['bin']}/agy.new",
        "overwrite_binary": f"echo x >> {setup['bin']}/agy",
        "write_spawn": f"touch {setup['spawn']}/x",
        "write_tmp": "touch /tmp/x",
        "write_usr": "touch /usr/x",
        "write_tmpdir": 'touch "$TMPDIR/x"',
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
        "proc1_is_cage": "yes",
        "write_home": "no",
        "write_bin_dir": "no",
        "overwrite_binary": "no",
        "write_spawn": "no",
        "write_tmp": "no",
        "write_usr": "no",
        "write_tmpdir": "yes",
        "token_read": "yes",
        "token_rewrite": "yes",
    }
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
