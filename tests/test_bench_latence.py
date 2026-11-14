"""scripts/bench_latence.py against a fake gateway and a fake CLI (no inference)."""

import importlib.util
import json
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KEY = "pb_bench_fake_key_0123456789"
TARGET = "claude=fake-gw-model=haiku"


def _load():
    spec = importlib.util.spec_from_file_location(
        "bench_latence", ROOT / "scripts" / "bench_latence.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load()


class _Handler(BaseHTTPRequestHandler):
    status = 200
    gate = None  # set per test: first word waits for the end of the "model"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        assert self.headers.get("Authorization") == f"Bearer {KEY}"
        if self.status != 200:
            raw = f'{{"error":"nope {KEY}"}}'.encode()
            self.send_response(self.status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if not body.get("stream"):
            raw = b'{"choices":[{"message":{"content":"PONG"}}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for text in ("PO", "NG"):
            evt = {"choices": [{"delta": {"content": text}}]}
            self.wfile.write(f"data: {json.dumps(evt)}\n\n".encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def gateway(monkeypatch):
    handler = type("H", (_Handler,), {"status": 200})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PLUGBOARD_BENCH_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("PLUGBOARD_BENCH_KEY", KEY)
    yield handler
    server.shutdown()
    server.server_close()


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    exe = tmp_path / "claude"
    exe.write_text("#!/bin/sh\ncat >/dev/null\necho PONG\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")


def test_full_run_prints_table_and_writes_json_without_the_key(
    gateway, fake_claude, tmp_path, capsys
):
    out = tmp_path / "r.json"
    rc = bench.main(
        [
            "--target",
            TARGET,
            "--rounds",
            "2",
            "--concurrency",
            "1,3",
            "--json",
            str(out),
        ]
    )
    shown = capsys.readouterr()
    assert rc == 0, shown.err
    report = json.loads(out.read_text())
    modes = {(r["mode"], r["concurrency"]) for r in report["rows"]}
    assert modes == {(m, c) for m in bench.MODES for c in (1, 3)}
    sse = next(
        r
        for r in report["rows"]
        if r["mode"] == "gateway-sse" and r["concurrency"] == 3
    )
    assert sse["summary"]["calls"] == 6
    assert sse["summary"]["first_word"]["med"] <= sse["summary"]["total"]["med"]
    assert "gateway-sse" in shown.out and "1st word" in shown.out
    assert KEY not in shown.out + shown.err + out.read_text()


def test_a_refusal_is_a_failure_and_the_key_is_scrubbed(gateway, tmp_path, capsys):
    gateway.status = 503
    out = tmp_path / "r.json"
    rc = bench.main(
        [
            "--target",
            TARGET,
            "--no-cli",
            "--rounds",
            "1",
            "--concurrency",
            "1",
            "--json",
            str(out),
        ]
    )
    shown = capsys.readouterr()
    assert rc == 1
    assert "failed" in shown.err
    assert KEY not in shown.out + shown.err + out.read_text()


def test_check_flags_a_slow_first_word(gateway, monkeypatch, capsys):
    monkeypatch.setattr(
        bench,
        "run_gateway",
        lambda *a, **k: {"ok": True, "first_word": 20.0, "total": 21.0},
    )
    rc = bench.main(
        [
            "--target",
            TARGET,
            "--no-cli",
            "--rounds",
            "1",
            "--concurrency",
            "1",
            "--check",
        ]
    )
    assert rc == 1
    assert "first word" in capsys.readouterr().err


def test_usage_errors_exit_2(monkeypatch, gateway):
    assert bench.main(["--target", "claude=onlytwo"]) == 2
    monkeypatch.delenv("PLUGBOARD_BENCH_KEY")
    assert bench.main(["--target", TARGET]) == 2


def test_plain_http_to_another_machine_needs_the_flag(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_BENCH_URL", "http://192.0.2.1:8765")
    monkeypatch.setenv("PLUGBOARD_BENCH_KEY", KEY)
    assert bench.main(["--target", TARGET, "--no-cli"]) == 2


def test_an_interrupted_stream_is_a_failure(gateway, monkeypatch, capsys):
    def cut(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        if not body.get("stream"):
            raw = b'{"choices":[{"message":{"content":"PONG"}}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"PO"}}]}\n\n')
        self.wfile.write(b'data: {"error":{"code":"stream_interrupted"}}\n\n')
        self.wfile.write(b"data: [DONE]\n\n")

    monkeypatch.setattr(gateway, "do_POST", cut)
    assert (
        bench.main(
            ["--target", TARGET, "--no-cli", "--rounds", "1", "--concurrency", "1"]
        )
        == 1
    )
    assert "failed" in capsys.readouterr().err


def test_busy_refusals_at_3_calls_are_not_failures(gateway):
    gateway.status = 503
    rc = bench.main(
        ["--target", TARGET, "--no-cli", "--rounds", "1", "--concurrency", "3"]
    )
    assert rc == 0


@pytest.mark.parametrize("kind", ["claude", "codex", "agy"])
def test_the_bare_cli_is_the_command_plugboard_spawns(kind):
    """Without Plugboard's isolation options the bare CLI loads the host's
    settings, MCP servers and commands: on the UAT, ``claude -p --tools ""``
    exits 1 with a 295 000-token prompt. The bench takes the options from
    plugboard/cli_subprocess.py, so both always run the same command."""
    from plugboard import cli_subprocess as cs

    backend = cs.CLI_BACKENDS[kind]
    argv, cwd, env = bench.bare_command(kind, "the-model")
    assert argv[0] == backend.command
    assert argv[1 : 1 + len(backend.args)] == backend.args
    assert argv[argv.index("--model") + 1] == "the-model"
    assert cwd == cs._clean_spawn_cwd() and cwd != "/tmp"
    assert env == cs.build_child_env(kind)
    if kind == "codex":
        isolation = cs.codex_isolation_args(cwd)
        assert isolation and all(opt in argv for opt in isolation)
        assert argv[-1] == "-"  # the operand that makes codex read stdin
    else:
        assert argv[-1] == "the-model"


def test_the_bare_claude_carries_every_isolation_option():
    argv, _, _ = bench.bare_command("claude", "haiku")
    for option, value in (
        ("--permission-mode", "default"),
        ("--tools", ""),
        ("--setting-sources", ""),
    ):
        assert argv[argv.index(option) + 1] == value
    assert "--disable-slash-commands" in argv and "--strict-mcp-config" in argv


def test_the_bare_agy_keeps_its_sandbox():
    argv, _, _ = bench.bare_command("agy", "flash")
    assert "--sandbox" in argv and "--disable-slash-commands" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_run_cli_spawns_the_isolated_command(monkeypatch, tmp_path):
    """The options reach the process, and the server's secrets do not."""
    seen = tmp_path / "argv.txt"
    exe = tmp_path / "claude"
    exe.write_text(
        "#!/bin/sh\ncat >/dev/null\n"
        f'for a in "$@"; do printf \'[%s]\\n\' "$a"; done > {seen}\n'
        f'printf \'cwd=%s\\nadmin=%s\\n\' "$PWD" "$ADMIN_KEY" >> {seen}\n'
        "echo PONG\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("ADMIN_KEY", "not-for-the-cli")
    result = bench.run_cli("claude", "haiku", 10)
    assert result["ok"], result
    got = seen.read_text()
    for option in (
        "[--strict-mcp-config]",
        "[--disable-slash-commands]",
        "[--setting-sources]\n[]",
        "[--permission-mode]\n[default]",
        "[--model]\n[haiku]",
    ):
        assert option in got, got
    assert "cwd=/tmp\n" not in got and "not-for-the-cli" not in got
