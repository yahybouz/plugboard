"""agy's cage has no network of its own but one way out (BUG-156, D-85).

Before, the cage shared the gateway's network (BUG-109): agy reached the
internet, the internal network and the cloud metadata address, the
gateway's own port on the loopback (enough to exhaust its rate limit and
fail its health check) and the sockets of other sessions. Now the cage gets
``--unshare-net``: its only network is its own loopback. Its only way out is
a unix socket the gateway creates in the request's private ``TMPDIR`` and
binds read-only into the cage, served by a CONNECT proxy limited to the
Google hosts agy needs (``plugboard.agy_egress``). Inside the cage, a small
relay (the gateway's own Python, standard library only) listens on the
cage's loopback and forwards to that socket, then becomes agy (``execv``,
agy stays the cage's pid 1); agy's ``HTTPS_PROXY`` names the relay.

The tests needing the host's bwrap are skipped, with the reason, where it
cannot create namespaces (as in tests/test_agy_cage.py).
"""

# ruff: noqa: F811 - the fixtures setup and real_bwrap of tests/test_agy_cage.py

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys

import pytest

from plugboard import agy_cage, agy_egress, netguard, web_egress
from plugboard import cli_subprocess as cs
from tests.test_agy_cage import (  # noqa: F401 - pytest fixtures
    _BODY,
    _SHELL_RO,
    _install,
    open_unix,
    real_bwrap,
    setup,
    unix_bind,
)


def _proxy_url() -> str:
    return f"http://127.0.0.1:{agy_cage.RELAY_PORT}"


def _tmpdir(setup) -> str:
    tmpdir = setup["spawn"] / "agy-tmp-net"
    tmpdir.mkdir(mode=0o700, exist_ok=True)
    return str(tmpdir)


def _listening_unix(path: str) -> socket.socket:
    sock = unix_bind(path)
    sock.listen()
    return sock


@pytest.fixture
def fake_agy(tmp_path):
    binary = tmp_path / "opt" / "agy"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    return str(binary)


# ── 1. The command line ─────────────────────────────────────────────────────


def test_cage_has_no_network_but_the_egress_socket(setup, fake_agy):
    tmpdir = _tmpdir(setup)
    egress = os.path.join(tmpdir, agy_cage.EGRESS_SOCKET)
    listener = _listening_unix(egress)
    try:
        env = {"HOME": str(setup["home"]), "TMPDIR": tmpdir, **agy_cage.proxy_env()}
        argv = agy_cage.cage_argv(fake_agy, ["models"], env=env, tmpdir=tmpdir)
    finally:
        listener.close()

    sep = argv.index("--")
    assert "--unshare-net" in argv[:sep]
    # The socket is bound read-only over itself, after the writable TMPDIR:
    # agy can neither remove nor replace it (EBUSY on a mount point).
    i_tmp = argv.index(tmpdir)
    i_sock = next(
        i
        for i, a in enumerate(argv[:sep])
        if a == "--ro-bind" and argv[i + 1 : i + 3] == [egress, egress]
    )
    assert argv[i_tmp - 1] == "--bind" and i_tmp < i_sock < argv.index("--remount-ro")
    # The relay first (the gateway's Python, isolated, no site), then agy.
    python = os.path.realpath(sys.executable)
    assert argv[sep + 1 : sep + 4] == [python, "-I", "-S"]
    assert argv[sep + 4] == "-c"
    assert argv[sep + 6 :] == [
        str(agy_cage.RELAY_PORT),
        egress,
        os.path.realpath(fake_agy),
        "models",
    ]
    assert (python, python) in [
        tuple(argv[i + 1 : i + 3]) for i, a in enumerate(argv) if a == "--ro-bind"
    ]
    # The proxy is set through execve's environment, never the command line.
    assert "--setenv" not in argv
    assert not any("PROXY" in a or _proxy_url() in a for a in argv)


def test_proxy_environment_names_the_relay_only():
    env = agy_cage.proxy_env()
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        assert env[name] == _proxy_url(), name
    for name in ("ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        assert env[name] == "", name


def test_no_egress_socket_no_cage(setup, fake_agy):
    tmpdir = _tmpdir(setup)
    env = {"HOME": str(setup["home"]), "TMPDIR": tmpdir}
    with pytest.raises(agy_cage.CageUnavailable, match="egress"):
        agy_cage.cage_argv(fake_agy, ["models"], env=env, tmpdir=tmpdir)
    # A regular file where the socket should be is no socket either.
    (setup["spawn"] / "agy-tmp-net" / agy_cage.EGRESS_SOCKET).write_text("x")
    with pytest.raises(agy_cage.CageUnavailable, match="egress"):
        agy_cage.cage_argv(fake_agy, ["models"], env=env, tmpdir=tmpdir)


async def test_agy_caged_serves_the_socket_and_sets_the_proxy(setup, fake_agy):
    async with cs.agy_caged(fake_agy, ["models"], str(setup["spawn"])) as (argv, env):
        egress = os.path.join(env["TMPDIR"], agy_cage.EGRESS_SOCKET)
        assert egress in argv
        for name, value in agy_cage.proxy_env().items():
            assert env[name] == value, name
        reader, writer = await open_unix(egress)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        assert head.startswith(b"HTTP/1.1 403")
    # Gone with the request's TMPDIR.
    assert not os.path.exists(egress)
    assert not os.path.exists(env["TMPDIR"])


# ── 2. The proxy agy goes through ───────────────────────────────────────────


@pytest.fixture
async def fake_dns(monkeypatch):
    answers: dict = {}
    loop = asyncio.get_running_loop()

    async def getaddrinfo(host, port, *args, **kwargs):
        if host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in answers[host]
        ]

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    return answers


@pytest.fixture
async def egress(tmp_path):
    path = str(tmp_path / "e.sock")
    server = await agy_egress.start(path)
    try:
        yield path
    finally:
        await server.close()


async def _ask(path: str, target: str) -> bytes:
    reader, writer = await open_unix(path)
    writer.write(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.read(200), 5)
    writer.close()
    return head


# The one listed name outside googleapis.com: agy's eligibility check fetches
# the account's profile picture there at start-up (BUG-156, seen on UAT as
# "Eligibility check failed: failed to get profile picture: Get
# https://lh3.googleusercontent.com/...", every agy call a 502).
_PICTURE_HOST = "lh3.googleusercontent.com"


def test_allowlist_is_exact_google_hosts():
    for host in agy_egress.ALLOWED_HOSTS:
        assert host.endswith(".googleapis.com") or host == _PICTURE_HOST, host
        assert agy_egress.host_allowed(host)
        assert agy_egress.host_allowed(host.upper() + ".")
    for host in (
        "example.com",
        "storage.googleapis.com",
        "evil.cloudcode-pa.googleapis.com",
        "cloudcode-pa.googleapis.com.evil.test",
        "generativelanguage.googleapis.com",
        "127.0.0.1",
        "",
    ):
        assert not agy_egress.host_allowed(host), host


def test_allowlist_lets_agy_fetch_its_profile_picture():
    assert agy_egress.host_allowed(_PICTURE_HOST)
    assert agy_egress.host_allowed("LH3.googleusercontent.com.")
    # That exact name only: not the rest of Google's user-content domain.
    for host in (
        "googleusercontent.com",
        "lh4.googleusercontent.com",
        "evil.lh3.googleusercontent.com",
        "lh3.googleusercontent.com.evil.test",
        "storage.googleusercontent.com",
        "x.apps.googleusercontent.com",
    ):
        assert not agy_egress.host_allowed(host), host


async def test_proxy_tunnels_the_profile_picture_host_on_443_only(
    egress, fake_dns, monkeypatch
):
    upstream, up_port = await _echo_server()
    fake_dns[_PICTURE_HOST] = ["127.0.0.1"]
    monkeypatch.setattr(netguard, "ip_is_public", lambda _ip: True)
    monkeypatch.setattr(web_egress, "_TLS_PORT", up_port)
    try:
        assert (await _ask(egress, f"{_PICTURE_HOST}:80")).startswith(b"HTTP/1.1 403")
        head = await _ask(egress, f"{_PICTURE_HOST}:{up_port}")
        assert head.startswith(b"HTTP/1.1 200"), head
    finally:
        upstream.close()
        await upstream.wait_closed()


async def test_proxy_refuses_a_host_off_the_list(egress, fake_dns, caplog):
    fake_dns["example.com"] = ["93.184.216.34"]
    with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
        head = await _ask(egress, "example.com:443")
    assert head.startswith(b"HTTP/1.1 403")
    refused = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert refused and "example.com" in refused[-1]


async def test_proxy_refuses_a_listed_name_resolving_inside(egress, fake_dns):
    for internal in ("127.0.0.1", "10.1.2.3", "100.64.0.1", "169.254.169.254"):
        fake_dns["cloudcode-pa.googleapis.com"] = [internal]
        head = await _ask(egress, "cloudcode-pa.googleapis.com:443")
        assert head.startswith(b"HTTP/1.1 403"), internal


async def test_proxy_refuses_another_port(egress, fake_dns, monkeypatch):
    monkeypatch.setattr(netguard, "ip_is_public", lambda _ip: True)
    fake_dns["oauth2.googleapis.com"] = ["127.0.0.1"]
    assert (await _ask(egress, "oauth2.googleapis.com:80")).startswith(b"HTTP/1.1 403")


async def test_proxy_refuses_an_internal_address_literal(egress):
    for target in ("127.0.0.1:443", "169.254.169.254:443", "[::1]:443"):
        assert (await _ask(egress, target)).startswith(b"HTTP/1.1 403"), target


async def _echo_server():
    async def echo(reader, writer):
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_proxy_tunnels_a_listed_host(egress, fake_dns, monkeypatch):
    upstream, up_port = await _echo_server()
    # No network: the listed name resolves to a local echo server, checked
    # public by force, whose port stands for 443.
    fake_dns["cloudcode-pa.googleapis.com"] = ["127.0.0.1"]
    monkeypatch.setattr(netguard, "ip_is_public", lambda _ip: True)
    monkeypatch.setattr(web_egress, "_TLS_PORT", up_port)
    try:
        reader, writer = await open_unix(egress)
        writer.write(
            f"CONNECT cloudcode-pa.googleapis.com:{up_port} HTTP/1.1\r\n\r\n".encode()
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(10), 5) == b"ping"
        writer.close()
    finally:
        upstream.close()
        await upstream.wait_closed()


async def test_agy_tunnels_are_not_web_tunnels(egress, fake_dns):
    before = web_egress.stats()
    fake_dns["example.com"] = ["93.184.216.34"]
    await _ask(egress, "example.com:443")
    assert web_egress.stats() == before


# ── 3. The real cage ────────────────────────────────────────────────────────

# Stands for agy (the relay execs it): from inside the cage, the gateway's
# port on the host loopback and an internal address must be unreachable;
# through HTTPS_PROXY (the relay, then the socket), a listed host must answer.
_PROBE = r"""
import json, os, socket, sys
p = json.loads(sys.argv[1])
out = {"pid": os.getpid()}
def try_connect(addr):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(addr)
        return "connected"
    except OSError as exc:
        return type(exc).__name__
    finally:
        s.close()
out["gateway"] = try_connect(("127.0.0.1", p["gateway_port"]))
out["private"] = try_connect(("10.0.0.1", 443))
out["metadata"] = try_connect(("169.254.169.254", 80))
proxy = os.environ["HTTPS_PROXY"].rsplit(":", 1)
s = socket.create_connection((proxy[0].split("//")[1], int(proxy[1])), timeout=5)
s.sendall(("CONNECT %s HTTP/1.1\r\n\r\n" % p["target"]).encode())
head = b""
while b"\r\n\r\n" not in head:
    chunk = s.recv(100)
    if not chunk:
        break
    head += chunk
out["proxy"] = head.split(b"\r\n")[0].decode()
if out["proxy"].startswith("HTTP/1.1 200"):
    s.sendall(b"ping")
    out["echo"] = s.recv(10).decode()
s.close()
print(json.dumps(out))
"""


async def _run_caged(binary: str, args: list, spawn: str):
    async with cs.agy_caged(binary, args, spawn) as (argv, env):
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=spawn,
            start_new_session=True,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 30)
    return proc.returncode, stdout, stderr


async def test_real_cage_reaches_only_the_egress(
    setup, real_bwrap, fake_dns, monkeypatch
):
    # The gateway's port, simulated: a listener on the host loopback.
    gateway = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    gateway_port = gateway.sockets[0].getsockname()[1]
    upstream, up_port = await _echo_server()
    fake_dns["cloudcode-pa.googleapis.com"] = ["127.0.0.1"]
    monkeypatch.setattr(netguard, "ip_is_public", lambda _ip: True)
    monkeypatch.setattr(web_egress, "_TLS_PORT", up_port)
    params = {
        "gateway_port": gateway_port,
        "target": f"cloudcode-pa.googleapis.com:{up_port}",
    }
    try:
        # Outside the cage, the same listener answers: the refusal inside is
        # the cage's doing.
        _r, w = await asyncio.open_connection("127.0.0.1", gateway_port)
        w.close()
        code, stdout, stderr = await _run_caged(
            sys.executable,
            ["-I", "-S", "-c", _PROBE, json.dumps(params)],
            str(setup["spawn"]),
        )
    finally:
        gateway.close()
        upstream.close()
    assert code == 0, stderr
    seen = json.loads(stdout)
    assert seen["gateway"] == "ConnectionRefusedError"
    assert seen["private"] != "connected"
    assert seen["metadata"] != "connected"
    assert seen["proxy"].startswith("HTTP/1.1 200")
    assert seen["echo"] == "ping"
    # The relay became agy: agy is still the cage's pid 1.
    assert seen["pid"] == 1


async def test_real_cage_proxy_refuses_off_list(setup, real_bwrap, fake_dns):
    fake_dns["example.com"] = ["93.184.216.34"]
    params = {"gateway_port": 9, "target": "example.com:443"}
    code, stdout, stderr = await _run_caged(
        sys.executable,
        ["-I", "-S", "-c", _PROBE, json.dumps(params)],
        str(setup["spawn"]),
    )
    assert code == 0, stderr
    assert json.loads(stdout)["proxy"].startswith("HTTP/1.1 403")


# ── 4. No relay, no socket: no agy ──────────────────────────────────────────


async def test_no_egress_socket_means_agy_is_never_launched(setup, monkeypatch):
    ran = setup["tmp"] / "agy-ran"
    _install(setup["bin"], "agy", f"touch {ran}\necho '{{}}'\n")
    launched = []
    real_exec = asyncio.create_subprocess_exec

    async def spy(*argv, **kw):
        launched.append(argv)
        return await real_exec(*argv, **kw)

    async def broken(path):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    monkeypatch.setattr(agy_egress, "start", broken)
    with pytest.raises(cs.CliSubprocessError) as caught:
        await cs.complete("agy", dict(_BODY))
    assert caught.value.sandbox_failed is True
    assert "egress" in str(caught.value)
    assert launched == []
    assert not ran.exists()
    assert list(setup["spawn"].iterdir()) == []


def test_a_relay_failure_is_a_cage_failure():
    line = agy_cage.RELAY_ERROR_PREFIX + "cannot listen on the cage's loopback"
    assert agy_cage.setup_failure(1, b"", (line + "\nmore\n").encode()) == line
    # agy's own output means agy ran: not a cage failure.
    assert agy_cage.setup_failure(1, b"{}", line.encode()) is None


async def test_real_relay_failure_never_runs_agy(setup, real_bwrap, monkeypatch):
    # A port the relay cannot listen on: it stops before agy.
    monkeypatch.setattr(agy_cage, "RELAY_PORT", 70000)
    monkeypatch.setattr(
        agy_cage,
        "cage_argv",
        __import__("functools").partial(
            agy_cage.cage_argv, _extra_ro_for_tests=_SHELL_RO
        ),
    )
    _install(setup["bin"], "agy", "echo agy-ran\n")
    code, stdout, stderr = await _run_caged(
        str(setup["bin"] / "agy"), ["models"], str(setup["spawn"])
    )
    assert code != 0
    assert stdout == b""
    failure = agy_cage.setup_failure(code, stdout, stderr)
    assert failure is not None and failure.startswith(agy_cage.RELAY_ERROR_PREFIX)


# ── A long TMPDIR (an operator's launch directory, a test's tmp path) ──────


def _long_dir(base) -> str:
    path = base / ("d" * 60) / ("e" * 60)
    path.mkdir(parents=True)
    return str(path)


async def test_egress_on_a_path_longer_than_a_unix_socket_name(tmp_path, fake_dns):
    path = os.path.join(_long_dir(tmp_path), agy_cage.EGRESS_SOCKET)
    assert len(path.encode()) > 108
    server = await agy_egress.start(path)
    try:
        assert os.path.exists(path)
        cwd = os.getcwd()
        os.chdir(os.path.dirname(path))
        try:
            head = await _ask(agy_cage.EGRESS_SOCKET, "example.com:443")
        finally:
            os.chdir(cwd)
        assert head.startswith(b"HTTP/1.1 403")
    finally:
        await server.close()
    assert not os.path.exists(path)


async def test_real_cage_with_a_long_tmpdir(setup, real_bwrap, fake_dns, tmp_path):
    spawn = _long_dir(tmp_path)
    os.chmod(spawn, 0o700)
    # The egress the relay joins runs past a socket's 107-byte name.
    egress = os.path.join(spawn, "agy-tmp-xxxxxxxx", agy_cage.EGRESS_SOCKET)
    assert len(egress.encode()) > 108
    fake_dns["example.com"] = ["93.184.216.34"]
    params = {"gateway_port": 9, "target": "example.com:443"}
    code, stdout, stderr = await _run_caged(
        sys.executable, ["-I", "-S", "-c", _PROBE, json.dumps(params)], spawn
    )
    assert code == 0, stderr
    assert json.loads(stdout)["proxy"].startswith("HTTP/1.1 403")


# ── 5. A bounded egress: connections and refusal logs ───────────────────────


async def _wait_for(predicate, what: str) -> None:
    for _ in range(250):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def test_egress_refuses_the_connection_past_its_cap(tmp_path, fake_dns, caplog):
    path = str(tmp_path / "e.sock")
    server = await agy_egress.start(path)
    held = []
    try:
        for _ in range(web_egress.MAX_CONNECTIONS):
            held.append(await open_unix(path))
        await _wait_for(
            lambda: server.active_connections == web_egress.MAX_CONNECTIONS,
            "every connection accepted",
        )
        with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
            for _ in range(5):
                reader, writer = await open_unix(path)
                head = await asyncio.wait_for(reader.read(100), 5)
                writer.close()
                assert head.startswith(b"HTTP/1.1 503"), head
        full = [r for r in caplog.records if "connections open" in r.getMessage()]
        assert len(full) == 1, [r.getMessage() for r in full]
        # A slot freed: the next request is served again.
        held.pop()[1].close()
        await _wait_for(
            lambda: server.active_connections < web_egress.MAX_CONNECTIONS,
            "a slot freed",
        )
        fake_dns["example.com"] = ["93.184.216.34"]
        assert (await _ask(path, "example.com:443")).startswith(b"HTTP/1.1 403")
    finally:
        for _reader, writer in held:
            writer.close()
        await server.close()


async def test_web_egress_refuses_the_connection_past_its_cap():
    port = await web_egress.ensure_started()
    held = []
    try:
        for _ in range(web_egress.MAX_CONNECTIONS):
            held.append(await asyncio.open_connection("127.0.0.1", port))
        await _wait_for(
            lambda: web_egress.active_connections() == web_egress.MAX_CONNECTIONS,
            "every connection accepted",
        )
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        head = await asyncio.wait_for(reader.read(100), 5)
        writer.close()
        assert head.startswith(b"HTTP/1.1 503"), head
    finally:
        for _reader, writer in held:
            writer.close()
        await web_egress.stop()


async def test_refusal_logs_are_one_per_host_and_window(
    egress, fake_dns, caplog, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(web_egress, "_clock", lambda: now[0])
    fake_dns["example.com"] = ["93.184.216.34"]
    fake_dns["example.org"] = ["93.184.216.35"]
    with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
        for _ in range(20):
            assert (await _ask(egress, "example.com:443")).startswith(b"HTTP/1.1 403")
        assert (await _ask(egress, "example.org:443")).startswith(b"HTTP/1.1 403")
        refused = [
            r.getMessage() for r in caplog.records if "refused" in r.getMessage()
        ]
        assert len(refused) == 2, refused
        assert "host=example.com" in refused[0] and "host=example.org" in refused[1]
        now[0] += web_egress.REFUSAL_LOG_WINDOW_S + 1
        assert (await _ask(egress, "example.com:443")).startswith(b"HTTP/1.1 403")
    refused = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert len(refused) == 3, refused
    assert "host=example.com" in refused[2] and "19 more" in refused[2]


async def test_refusal_logs_stay_bounded_with_many_hosts(egress, caplog, monkeypatch):
    monkeypatch.setattr(web_egress, "_clock", lambda: 1000.0)
    with caplog.at_level(logging.INFO, logger="plugboard.web_egress"):
        for i in range(web_egress.REFUSAL_LOG_HOSTS + 50):
            await _ask(egress, f"h{i}.example.com:443")
    refused = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert len(refused) <= web_egress.REFUSAL_LOG_HOSTS + 1, len(refused)


# A malformed port or a client gone early never escapes as a trace: asyncio
# would log one per connection, around the bounded refusal log (BUG-156).


@pytest.fixture
async def loop_errors():
    loop = asyncio.get_running_loop()
    seen: list = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    try:
        yield seen
    finally:
        loop.set_exception_handler(previous)


def test_a_port_in_unicode_digits_is_malformed():
    for port in ("²", "³", "¹", "44³", "٤٤٣"):
        assert web_egress._parse_target(f"CONNECT h.example:{port} HTTP/1.1") is None
        assert (
            web_egress._parse_target(f"CONNECT [2001:db8::1]:{port} HTTP/1.1") is None
        )


async def test_a_port_in_unicode_digits_is_refused_without_a_trace(
    egress, loop_errors, caplog
):
    with caplog.at_level(logging.INFO):
        reader, writer = await open_unix(egress)
        # The head is read as latin-1: the byte 0xb2 is "²".
        writer.write("CONNECT example.com:² HTTP/1.1\r\n\r\n".encode("latin-1"))
        await writer.drain()
        head = await asyncio.wait_for(reader.read(200), 5)
        writer.close()
        await asyncio.sleep(0.05)
    assert head.startswith(b"HTTP/1.1 405"), head
    assert loop_errors == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


class _GoneWriter:
    """A client writer whose peer left: every drain fails, as asyncio's does."""

    def __init__(self) -> None:
        self.written: list = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        raise ConnectionResetError("Connection lost")

    def close(self) -> None:
        self.closed = True


def _request(target: str) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    return reader


async def test_a_client_gone_before_the_refusal_raises_nothing():
    for target, allowed in (
        ("example.com:443", lambda _h: False),  # 403, off the list
        ("example.com:80", None),  # 403, not 443
        ("example.com", None),  # 405, malformed
    ):
        writer = _GoneWriter()
        outcome = await web_egress.serve_connect(
            _request(target), writer, host_allowed=allowed
        )
        assert outcome in (False, None), target
        assert writer.closed, target


async def test_a_client_reset_while_sending_its_request_raises_nothing():
    reader = asyncio.StreamReader()
    reader.feed_data(b"CONNECT example.com:443 HT")
    reader.set_exception(ConnectionResetError("Connection lost"))
    writer = _GoneWriter()
    assert await web_egress.serve_connect(reader, writer) is None
    assert writer.closed


async def test_a_client_gone_before_the_200_closes_the_upstream(fake_dns, monkeypatch):
    upstream_eof = asyncio.Event()

    async def upstream_side(reader, writer):
        await reader.read()  # until the egress closes its side
        upstream_eof.set()
        writer.close()

    upstream = await asyncio.start_server(upstream_side, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    fake_dns["example.com"] = ["127.0.0.1"]
    monkeypatch.setattr(netguard, "ip_is_public", lambda _ip: True)
    monkeypatch.setattr(web_egress, "_TLS_PORT", up_port)
    try:
        writer = _GoneWriter()
        await web_egress.serve_connect(_request(f"example.com:{up_port}"), writer)
        assert writer.written == [b"HTTP/1.1 200 Connection Established\r\n\r\n"]
        assert writer.closed
        await asyncio.wait_for(upstream_eof.wait(), 5)
    finally:
        upstream.close()
        # wait_closed waits for the upstream connections: bounded, so a
        # socket left open fails the test instead of hanging it.
        await asyncio.wait_for(upstream.wait_closed(), 5)


# ── 6. The host's abstract unix sockets, out of the cage's reach ────────────


async def test_real_cage_cannot_reach_an_abstract_socket(setup, real_bwrap):
    name = "plugboard-test-" + os.urandom(6).hex()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind("\0" + name)
    listener.listen()
    probe = (
        "import socket, sys\n"
        "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
        "try:\n"
        "    s.connect('\\0' + sys.argv[1])\n"
        "    print('connected')\n"
        "except OSError as exc:\n"
        "    print(type(exc).__name__)\n"
    )
    try:
        # Outside the cage, the same socket answers.
        outside = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        outside.connect("\0" + name)
        outside.close()
        code, stdout, stderr = await _run_caged(
            sys.executable, ["-I", "-S", "-c", probe, name], str(setup["spawn"])
        )
    finally:
        listener.close()
    assert code == 0, stderr
    assert stdout.decode().strip() == "ConnectionRefusedError"


# ── 7. A cancelled call leaves no egress behind ─────────────────────────────


def _open_sockets() -> int:
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}").startswith("socket:"):
                count += 1
        except OSError:
            pass
    return count


@pytest.mark.parametrize("cancels", [1, 2], ids=["once", "twice"])
async def test_a_cancelled_call_removes_its_egress(setup, agy_without_cage, cancels):
    ran = setup["tmp"] / "agy-tmpdir"
    # Records its TMPDIR, then hangs, deaf to SIGTERM: the gateway's cleanup
    # takes its grace period, during which a second cancel arrives.
    _install(
        setup["bin"],
        "agy",
        f"trap '' TERM\necho \"$TMPDIR\" > {ran}.part\nmv {ran}.part {ran}\nsleep 30\n",
    )
    before = _open_sockets()
    task = asyncio.ensure_future(cs.complete("agy", dict(_BODY), timeout_s=30.0))
    await _wait_for(ran.exists, "agy started")
    tmpdir = ran.read_text().strip()
    egress = os.path.join(tmpdir, agy_cage.EGRESS_SOCKET)
    assert os.path.exists(egress)
    # A connection open on the egress, as agy's would be.
    _reader, writer = await open_unix(egress)
    writer.write(b"CONNECT oauth2.googleapis.com:443 HTTP/1.1\r\n")
    await writer.drain()

    task.cancel()
    if cancels == 2:
        await asyncio.sleep(0.2)  # the cleanup is under way
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    writer.close()
    await asyncio.sleep(0.05)

    assert not os.path.exists(egress)
    assert not os.path.exists(tmpdir)
    assert _open_sockets() == before
