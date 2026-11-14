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
