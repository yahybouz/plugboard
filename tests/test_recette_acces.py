"""Lot B: scripts/recette_acces.py against a fake gateway (no provider, no CLI)."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mcp")
ROOT = Path(__file__).resolve().parents[1]
KEY = "pb_recette_key_not_real_0123456789"

spec = importlib.util.spec_from_file_location(
    "recette_acces", ROOT / "scripts" / "recette_acces.py"
)
recette = importlib.util.module_from_spec(spec)
sys.modules["recette_acces"] = recette
spec.loader.exec_module(recette)

CATALOGUE = [
    {"id": "claude-code:claude-sonnet-4-6", "owned_by": "claude-code"},
    {"id": "Codex:gpt-5.6-luna", "owned_by": "Codex", "connected": True},
    {"id": "Codex:gpt-5.6-mini", "owned_by": "Codex"},
    {"id": "agy:gemini-3-flash", "owned_by": "agy"},
]


def make_gateway(fail=None, catalogue=None):
    """fail: {(model, 'plain'|'stream'): (status, body)}"""
    fail = fail or {}
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                return self._json(401, {"detail": "Invalid API key"})
            if self.path == "/v1/models":
                return self._json(
                    200,
                    {
                        "object": "list",
                        "data": CATALOGUE if catalogue is None else catalogue,
                    },
                )
            if self.path == "/v1/me":
                return self._json(200, {"client_id": "cust", "usage": {}})
            self._json(404, {"detail": "nope"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(("POST", self.path, self.headers.get("Authorization")))
            kind = "stream" if body.get("stream") else "plain"
            if (body["model"], kind) in fail:
                status, err = fail[(body["model"], kind)]
                return self._json(status, err)
            if not body.get("stream"):
                return self._json(200, {"choices": [{"message": {"content": "pong"}}]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in (
                'data: {"choices":[{"delta":{"content":"po"}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"ng"}}]}\n\n',
                "data: [DONE]\n\n",
            ):
                self.wfile.write(chunk.encode())
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def gateway():
    servers = []

    def start(**kw):
        server, seen = make_gateway(**kw)
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", seen

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def run_cli(url, tmp_path, capsys, *extra, key=KEY):
    out = tmp_path / "out.json"
    env = {"PLUGBOARD_URL": url, "PLUGBOARD_KEY": key}
    code = recette.main([str(out), "--mcp-python", sys.executable, *extra], env=env)
    captured = capsys.readouterr()
    data = json.loads(out.read_text()) if out.exists() else None
    return code, captured.out, data


def test_all_surfaces_pass_one_model_per_provider(gateway, tmp_path, capsys):
    url, seen = gateway()
    code, out, data = run_cli(url, tmp_path, capsys)
    assert code == 0, out
    rows = data["results"]
    assert data["summary"]["ok"] is True
    assert set(data["summary"]["providers"]) == {"claude-code", "Codex", "agy"}
    by = {(r["provider"], r["surface"]): r for r in rows}
    for provider in ("claude-code", "Codex", "agy"):
        for surface in ("api", "api-stream", "mcp"):
            assert by[(provider, surface)]["status"] == "OK", by[(provider, surface)]
    # default pick: the connected model, else the first listed
    assert by[("Codex", "api")]["model"] == "Codex:gpt-5.6-luna"
    assert by[("claude-code", "api")]["model"] == "claude-code:claude-sonnet-4-6"
    assert by[("agy", "api-stream")]["first_byte_s"] is not None
    assert by[("-", "mcp-same-ids")]["status"] == "OK"
    assert "surface" in out and "1st byte" in out
    # the MCP server called the gateway with the same client key
    assert all(auth == f"Bearer {KEY}" for _, _, auth in seen)
    assert KEY not in out and KEY not in (tmp_path / "out.json").read_text()


def test_model_option_overrides_the_pick(gateway, tmp_path, capsys):
    url, _ = gateway()
    code, _, data = run_cli(
        url, tmp_path, capsys, "--model", "Codex=gpt-5.6-mini", "--skip-mcp"
    )
    assert code == 0
    assert {r["model"] for r in data["results"] if r["provider"] == "Codex"} == {
        "Codex:gpt-5.6-mini"
    }
    assert not any(r["surface"].startswith("mcp") for r in data["results"])


def test_a_failing_surface_gives_a_nonzero_exit_and_the_code(gateway, tmp_path, capsys):
    quota = {
        "detail": "The quota of the Codex provider is exhausted: retry later.",
        "code": "quota_exhausted",
    }
    url, _ = gateway(fail={("Codex:gpt-5.6-luna", "stream"): (429, quota)})
    code, out, data = run_cli(url, tmp_path, capsys)
    assert code == 1
    row = next(
        r
        for r in data["results"]
        if r["surface"] == "api-stream" and r["provider"] == "Codex"
    )
    assert row["status"] == "FAIL"
    assert row["detail"].startswith("quota_exhausted:")
    assert data["summary"]["ok"] is False and data["summary"]["failed"] == 1
    # the other surfaces were still tried and passed
    assert any(r["surface"] == "mcp" and r["status"] == "OK" for r in data["results"])


def test_mcp_failure_shows_the_same_code_as_the_api(gateway, tmp_path, capsys):
    refused = {
        "detail": "subscription does not support the model",
        "code": "model_not_supported_by_subscription",
    }
    url, _ = gateway(
        fail={
            ("agy:gemini-3-flash", "plain"): (400, refused),
            ("agy:gemini-3-flash", "stream"): (400, refused),
        }
    )
    code, _, data = run_cli(url, tmp_path, capsys)
    assert code == 1
    agy = {r["surface"]: r for r in data["results"] if r["provider"] == "agy"}
    for surface in ("api", "api-stream", "mcp"):
        assert agy[surface]["status"] == "FAIL"
        assert agy[surface]["detail"].startswith("model_not_supported_by_subscription")


def test_wrong_key_fails_without_printing_it(gateway, tmp_path, capsys):
    url, _ = gateway()
    wrong = "pb_wrong_key_not_real_9876543210"
    code, out, data = run_cli(url, tmp_path, capsys, key=wrong)
    assert code == 1
    assert wrong not in out and wrong not in json.dumps(data)


def test_no_visible_provider_is_a_failure(gateway, tmp_path, capsys):
    url, _ = gateway(catalogue=[])
    code, _, data = run_cli(url, tmp_path, capsys, "--skip-mcp")
    assert code == 1
    assert data["summary"]["ok"] is False


def test_missing_environment_is_a_usage_error(tmp_path, capsys):
    assert recette.main([str(tmp_path / "o.json")], env={}) == 2
    assert (
        recette.main(
            [str(tmp_path / "o.json"), "--model", "oops"],
            env={"PLUGBOARD_URL": "http://x", "PLUGBOARD_KEY": KEY},
        )
        == 2
    )


def test_rate_limiter_never_exceeds_60_in_a_minute():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    limiter = recette.RateLimiter(60, clock=lambda: now[0], sleep=sleep)
    stamps = []
    for _ in range(130):
        limiter.acquire()
        stamps.append(now[0])
    for i, t in enumerate(stamps):
        window = [s for s in stamps if t - 60.0 < s <= t]
        assert len(window) <= 60
    assert slept  # it did wait


def test_unreachable_gateway_exits_one(tmp_path, capsys):
    code, _, data = run_cli("http://127.0.0.1:9", tmp_path, capsys, "--skip-mcp")
    assert code == 1
    assert data["results"][0]["status"] == "FAIL"


def test_each_run_sends_a_prompt_the_cache_has_not_seen():
    assert recette.RUN_ID in recette.PROMPT
    assert len(recette.RUN_ID) == 8


def test_a_truncated_answer_is_a_failure_not_a_crash(tmp_path, capsys):
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conn.recv(4096)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b'Content-Length: 500\r\n\r\n{"data":'
            )
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    url = f"http://127.0.0.1:{srv.getsockname()[1]}"
    code, _, data = run_cli(url, tmp_path, capsys, "--skip-mcp")
    srv.close()
    assert code == 1
    assert data["results"][0]["status"] == "FAIL"
    assert data["summary"]["mcp_skipped"] is True


def _redirecting_server(target: str, only_post: bool = False):
    """A gateway that answers a redirect to ``target`` (302), optionally for
    the chat calls only; ``seen`` lists what it received."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _redirect(self):
            self.send_response(302)
            self.send_header("Location", target + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            seen.append(("GET", self.path))
            if not only_post:
                return self._redirect()
            body = {"object": "list", "data": CATALOGUE[:1]}
            if self.path == "/v1/me":
                body = {"client_id": "cust", "usage": {}}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(("POST", self.path))
            self._redirect()

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.mark.parametrize("only_post", [False, True])
def test_a_redirect_is_a_named_failure_and_the_key_never_follows_it(
    gateway, tmp_path, capsys, only_post
):
    """A redirect must fail by name: followed, it would hand the key
    (``Authorization``) to whatever address the answer names."""
    elsewhere, seen_elsewhere = gateway()
    server, seen = _redirecting_server(elsewhere, only_post=only_post)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        code, out, data = run_cli(url, tmp_path, capsys, "--skip-mcp")
    finally:
        server.shutdown()
        server.server_close()
    assert code == 1
    assert seen, "the first address was called"
    assert seen_elsewhere == [], "nothing, and no key, reached the redirect target"
    failed = [r for r in data["results"] if r["status"] == "FAIL"]
    assert failed and all("redirect" in r["detail"] for r in failed)
    assert all("302" in r["detail"] for r in failed)
    assert KEY not in out and KEY not in json.dumps(data)


def test_a_control_character_inside_the_key_is_refused_without_echo(
    gateway, tmp_path, capsys
):
    """Python would quote the whole ``Authorization`` value in its refusal, and
    the masked table would still show the end of the key."""
    url, seen = gateway()
    out = tmp_path / "out.json"
    bad = KEY[:8] + "\r" + KEY[8:]

    code = recette.main([str(out)], env={"PLUGBOARD_URL": url, "PLUGBOARD_KEY": bad})

    captured = capsys.readouterr()
    shown = captured.out + captured.err
    assert code == 2
    assert "PLUGBOARD_KEY" in captured.err and "not shown" in captured.err
    assert KEY[:8] not in shown and KEY[8:] not in shown and "Traceback" not in shown
    assert seen == [] and not out.exists()
