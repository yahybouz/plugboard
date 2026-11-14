"""scripts/recette_passerelle.py: the acceptance run Hermes launches after a deploy.

Tested against a local fake gateway, never a real one: no inference happens here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "recette_passerelle.py"
KEY = "pb_" + "x" * 32  # factice, sans entropie
MODEL = "anthropic-direct:claude-haiku-5-5"


class _Fake(BaseHTTPRequestHandler):
    chat_status = 200
    health_status = 200

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        return self.headers.get("Authorization") == f"Bearer {KEY}"

    def do_GET(self):
        if self.path == "/health":
            if _Fake.health_status != 200:
                self.send_response(_Fake.health_status)
                self.send_header("Location", "https://example.invalid/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            return self._send(200, {"status": "ok", "version": "4.13.0"})
        if self.path == "/health/ready":
            return self._send(200, {"status": "ready"})
        if self.path == "/v1/models":
            if not self._auth_ok():
                return self._send(401, {"detail": "unauthorized"})
            return self._send(200, {"object": "list", "data": [{"id": MODEL}]})
        return self._send(404, {"detail": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path != "/v1/chat/completions" or not self._auth_ok():
            return self._send(401, {"detail": "unauthorized"})
        if _Fake.chat_status != 200:
            return self._send(_Fake.chat_status, {"detail": "boom"})
        return self._send(
            200,
            {"choices": [{"message": {"role": "assistant", "content": "Bonjour."}}]},
        )


@pytest.fixture
def gateway():
    server = HTTPServer(("127.0.0.1", 0), _Fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Fake.chat_status = 200
    _Fake.health_status = 200
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _run(url: str, **extra_env) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PLUGBOARD_URL": url,
        "PLUGBOARD_API_KEY": KEY,
        "PLUGBOARD_RECETTE_MODEL": MODEL,
        "PLUGBOARD_RECETTE_TIMEOUT": "5",
    }
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_all_steps_pass_and_the_key_never_appears(gateway):
    proc = _run(gateway)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout
    assert KEY not in proc.stdout and KEY not in proc.stderr


def test_a_failed_chat_stops_the_run_with_exit_1(gateway):
    _Fake.chat_status = 500
    proc = _run(gateway)
    assert proc.returncode == 1
    assert "chat" in proc.stdout.lower()
    assert KEY not in proc.stdout + proc.stderr


def test_an_unreachable_gateway_fails_with_exit_1():
    proc = _run("http://127.0.0.1:9")
    assert proc.returncode == 1
    assert KEY not in proc.stdout + proc.stderr


def test_missing_key_is_a_configuration_error_without_network():
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "PLUGBOARD_API_KEY" in proc.stderr


def test_wrong_key_fails_the_models_step(gateway):
    proc = _run(gateway, PLUGBOARD_API_KEY="pb_" + "y" * 32)
    assert proc.returncode == 1
    assert "y" * 32 not in proc.stdout + proc.stderr


def test_a_successful_run_says_so(gateway):
    proc = _run(gateway)
    assert "TOUT PASSE" in proc.stdout


def test_a_key_with_a_newline_is_refused_without_echo(gateway):
    proc = _run(gateway, PLUGBOARD_API_KEY=KEY + "\nsuite")
    assert proc.returncode == 2
    assert KEY not in proc.stdout + proc.stderr
    assert "suite" not in proc.stdout + proc.stderr


def test_a_redirect_is_a_named_failure_not_a_silent_one(gateway):
    _Fake.health_status = 308
    proc = _run(gateway)
    assert proc.returncode == 1
    assert "redirection" in proc.stdout
    assert "Traceback" not in proc.stderr
