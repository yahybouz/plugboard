"""scripts/smoke_consumers.py against a fake gateway (PLU-18).

The smoke runs after a deployment with a limited client key read from
``PLUGBOARD_SMOKE_KEY``. It checks /health, /v1/models, a chat, a streamed
chat, a structured output and, only when the key may use a declared
embedding model, /v1/embeddings. Every case here runs the script as a
subprocess against a local HTTP server: no gateway, no model, no network.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_consumers.py"
KEY = "pb_fake_smoke_key_0123456789abcdef"
MODEL = "Codex:gpt-smoke"
EMBED = "ollama:qwen3-embedding:0.6b"


def _default_behavior() -> dict:
    return {
        # /health as 4.12.0 publishes it: the status and the version only.
        "health": {"ok": True, "version": "9.9.9"},
        # /health/ready (4.12.0); None: an older gateway without the route.
        "ready": {"ready": True, "checks": {"auth": "ok", "usage": "ok"}},
        "ready_status": 200,
        "models": [MODEL, "ollama:other"],
        # account_compatible per model, as 4.4.0 publishes it (absent: not sent)
        "compatible": {},
        "me_client": "smoke",
        "me_allowed": [MODEL],
        "redirect_to": None,
        "chat_status": 200,
        "chat_content": "pong",
        "chat_error_body": None,
        "stream_chunks": ["po", "ng"],
        "stream_done": True,
        "stream_error": False,
        # None: answer what the schema asks (a gateway honouring
        # response_format); a string: that content whatever was sent.
        "structured_content": None,
        "drop_response_format": False,
        # (status, body) answered to a chat carrying response_format, or None.
        "structured_error": None,
        "embedding_models": [],
        "embedding_route": True,
        "embedding_vector": [0.1, 0.2, 0.3],
        "delay": 0.0,
    }


class _Gateway(BaseHTTPRequestHandler):
    behavior: dict = {}
    seen: list = []

    def log_message(self, *args):  # keep pytest output clean
        pass

    def _send(self, status: int, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {KEY}":
            return True
        self._send(401, {"detail": "Invalid API key"})
        return False

    def do_GET(self):  # noqa: N802 (http.server API)
        b = self.behavior
        time.sleep(b["delay"])
        self.seen.append(("GET", self.path, None))
        if self.path == "/health":
            return self._send(200, b["health"])
        if self.path == "/health/ready":
            if b["ready"] is None:
                return self._send(404, {"detail": "Not Found"})
            return self._send(b["ready_status"], b["ready"])
        if b["redirect_to"] and self.path == "/v1/models":
            self.send_response(302)
            self.send_header("Location", b["redirect_to"] + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if not self._authorized():
            return None
        if self.path == "/v1/models":
            data = []
            for m in b["models"]:
                row = {"id": m, "object": "model"}
                if m in b["compatible"]:
                    row["account_compatible"] = b["compatible"][m]
                data.append(row)
            return self._send(200, {"object": "list", "data": data})
        if self.path == "/v1/me":
            if b["me_client"] == "admin":  # the admin key has no key record
                return self._send(200, {"client_id": "admin", "usage": {}})
            return self._send(
                200, {"client_id": b["me_client"], "allowed_models": b["me_allowed"]}
            )
        if self.path == "/v1/embeddings/models":
            if not b["embedding_route"]:
                return self._send(404, {"detail": "Not Found"})
            return self._send(200, {"object": "list", "data": b["embedding_models"]})
        return self._send(404, {"detail": "Not Found"})

    def do_POST(self):  # noqa: N802
        b = self.behavior
        time.sleep(b["delay"])
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.seen.append(("POST", self.path, body))
        if not self._authorized():
            return None
        if self.path == "/v1/embeddings":
            vec = b["embedding_vector"]
            return self._send(
                200, {"object": "list", "data": [{"index": 0, "embedding": vec}]}
            )
        if self.path != "/v1/chat/completions":
            return self._send(404, {"detail": "Not Found"})
        if b["chat_status"] != 200:
            detail = b["chat_error_body"] or {"detail": "boom"}
            return self._send(b["chat_status"], detail)
        if body.get("stream"):
            return self._stream(b)
        if b["structured_error"] and body.get("response_format"):
            return self._send(*b["structured_error"])
        fmt = None if b["drop_response_format"] else body.get("response_format")
        if b["structured_content"] is not None and body.get("response_format"):
            content = b["structured_content"]
        elif fmt:
            # A model under the schema's grammar: the only value allowed.
            schema = fmt["json_schema"]["schema"]
            content = json.dumps({"answer": schema["properties"]["answer"]["enum"][0]})
        else:
            content = b["chat_content"]
        return self._send(
            200,
            {
                "object": "chat.completion",
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    def _stream(self, b):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece in b["stream_chunks"]:
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        if b["stream_error"]:
            err = {"error": {"message": "upstream failed", "type": "provider_error"}}
            self.wfile.write(f"data: {json.dumps(err)}\n\n".encode())
        if b["stream_done"]:
            self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


class _Recorder(BaseHTTPRequestHandler):
    """Another host: records whatever reaches it."""

    headers_seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        _Recorder.headers_seen.append(dict(self.headers))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # The deadline case drops connections on purpose: a broken pipe on
        # the fake side is expected, not a test failure.
        pass


@pytest.fixture()
def gateway():
    _Gateway.behavior = _default_behavior()
    _Gateway.seen = []
    server = _Server(("127.0.0.1", 0), _Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, _Gateway
    server.shutdown()
    server.server_close()


def _smoke(server, *args, key=KEY, model=MODEL, timeout=30):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGBOARD_SMOKE")}
    if key is not None:
        env["PLUGBOARD_SMOKE_KEY"] = key
    if model is not None:
        env["PLUGBOARD_SMOKE_MODEL"] = model
    url = f"http://127.0.0.1:{server.server_address[1]}"
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--url", url, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _lines(proc, status):
    return [line for line in proc.stdout.splitlines() if line.startswith(status)]


def _check_names(proc, status):
    return [line.split()[1].rstrip(":") for line in _lines(proc, status)]


def test_a_healthy_gateway_passes_and_skips_absent_embeddings(gateway):
    server, gw = gateway

    proc = _smoke(server)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _check_names(proc, "PASS") == [
        "health",
        "models",
        "chat",
        "stream",
        "structured",
    ]
    assert _check_names(proc, "SKIP") == ["embeddings"]
    assert _lines(proc, "FAIL") == []
    assert KEY not in proc.stdout + proc.stderr


def test_requests_bypass_the_cache_and_stay_small(gateway):
    server, gw = gateway

    _smoke(server)

    chats = [b for m, p, b in gw.seen if p == "/v1/chat/completions"]
    assert len(chats) == 3
    for body in chats:
        assert body["model"] == MODEL
        assert body["cache"] is False
        assert body["max_tokens"] <= 64
    structured = [b for b in chats if b.get("response_format")]
    assert structured[0]["response_format"]["type"] == "json_schema"
    # Only the schema tells the model what to answer.
    prompt = structured[0]["messages"][0]["content"].lower()
    assert "json" not in prompt and "answer" not in prompt
    # Only reads and chat calls: no admin route, nothing written.
    assert not [p for m, p, b in gw.seen if p.startswith(("/admin", "/providers"))]


def test_declared_embedding_model_is_checked(gateway):
    server, gw = gateway
    gw.behavior["embedding_models"] = [{"id": EMBED, "available": True}]

    proc = _smoke(server)

    assert proc.returncode == 0, proc.stdout
    assert "embeddings" in _check_names(proc, "PASS")
    calls = [b for m, p, b in gw.seen if p == "/v1/embeddings"]
    assert calls and calls[0]["model"] == EMBED


def test_embedding_model_from_the_environment_wins(gateway):
    server, gw = gateway
    gw.behavior["embedding_models"] = [
        {"id": "ollama:other-embed", "available": True},
        {"id": EMBED, "available": True},
    ]

    proc = _smoke(server, "--embedding-model", EMBED)

    assert proc.returncode == 0, proc.stdout
    calls = [b for m, p, b in gw.seen if p == "/v1/embeddings"]
    assert [c["model"] for c in calls] == [EMBED]


def test_empty_embedding_vector_fails(gateway):
    server, gw = gateway
    gw.behavior["embedding_models"] = [{"id": EMBED, "available": True}]
    gw.behavior["embedding_vector"] = []

    proc = _smoke(server)

    assert proc.returncode == 1
    assert _check_names(proc, "FAIL") == ["embeddings"]


def test_a_declared_but_unavailable_embedding_model_fails(gateway):
    server, gw = gateway
    gw.behavior["embedding_models"] = [{"id": EMBED, "available": False}]

    proc = _smoke(server)

    assert proc.returncode == 1, proc.stdout
    assert _check_names(proc, "FAIL") == ["embeddings"]
    assert not [p for m, p, b in gw.seen if p == "/v1/embeddings"]


def test_no_declared_embedding_model_fails_only_when_required(gateway):
    server, gw = gateway

    proc = _smoke(server, "--require-embeddings")

    assert proc.returncode == 1
    assert _check_names(proc, "FAIL") == ["embeddings"]


def test_an_embedding_model_asked_for_but_not_declared_fails(gateway):
    server, gw = gateway
    gw.behavior["embedding_models"] = [{"id": "ollama:other-embed", "available": True}]

    proc = _smoke(server, "--embedding-model", EMBED)

    assert proc.returncode == 1
    assert _check_names(proc, "FAIL") == ["embeddings"]


def test_a_missing_embeddings_route_fails(gateway):
    # The route exists since 4.4.0: a 404 means the routing is broken.
    server, gw = gateway
    gw.behavior["embedding_route"] = False

    proc = _smoke(server)

    assert proc.returncode == 1, proc.stdout
    assert _check_names(proc, "FAIL") == ["embeddings"]


@pytest.mark.parametrize(
    ("change", "failed"),
    [
        ({"chat_status": 500}, ["chat", "stream", "structured"]),
        ({"chat_content": ""}, ["chat"]),
        ({"stream_done": False}, ["stream"]),
        ({"stream_error": True}, ["stream"]),
        ({"stream_chunks": []}, ["stream"]),
        ({"structured_content": "pong"}, ["structured"]),
        ({"structured_content": '```json\n{"answer": "pong"}\n```'}, ["structured"]),
        ({"structured_content": '{"other": 1}'}, ["structured"]),
        ({"structured_content": '{"answer": "pong"}'}, ["structured"]),
        ({"drop_response_format": True}, ["structured"]),
        ({"models": ["ollama:other"]}, ["models"]),
        ({"compatible": {MODEL: False}}, ["models"]),
        ({"me_allowed": []}, ["models"]),
        ({"me_client": "admin"}, ["models"]),
        ({"health": {"ok": False, "version": "9.9.9"}}, ["health"]),
        (
            {
                "ready": {"ready": False, "checks": {"auth": "error"}},
                "ready_status": 503,
            },
            ["health"],
        ),
        # A gateway before 4.12.0 has no /health/ready: its /health field.
        (
            {
                "ready": None,
                "health": {"ok": True, "version": "9.9.9", "auth_disabled": True},
            },
            ["health"],
        ),
    ],
)
def test_each_broken_check_fails_the_smoke(gateway, change, failed):
    server, gw = gateway
    gw.behavior.update(change)

    proc = _smoke(server)

    assert proc.returncode == 1, proc.stdout
    assert _check_names(proc, "FAIL") == failed


def test_expected_version_is_enforced(gateway):
    server, gw = gateway

    assert _smoke(server, "--expect-version", "9.9.9").returncode == 0
    proc = _smoke(server, "--expect-version", "4.12.0")
    assert proc.returncode == 1
    assert _check_names(proc, "FAIL") == ["health"]


def test_an_unlimited_key_can_be_accepted_explicitly(gateway):
    server, gw = gateway
    gw.behavior["me_allowed"] = []

    proc = _smoke(server, "--allow-unlimited-key")

    assert proc.returncode == 0, proc.stdout
    assert any("not limited" in line for line in _lines(proc, "WARN"))


def test_a_4_4_0_model_list_with_the_model_allowed_passes(gateway):
    server, gw = gateway
    gw.behavior["compatible"] = {MODEL: True, "ollama:other": False}

    proc = _smoke(server)

    assert proc.returncode == 0, proc.stdout


def test_a_redirect_is_not_followed_and_the_key_stays_here(gateway):
    server, gw = gateway
    other = _Server(("127.0.0.1", 0), _Recorder)
    _Recorder.headers_seen = []
    thread = threading.Thread(target=other.serve_forever, daemon=True)
    thread.start()
    try:
        gw.behavior["redirect_to"] = f"http://127.0.0.1:{other.server_address[1]}"
        proc = _smoke(server)
    finally:
        other.shutdown()
        other.server_close()

    assert proc.returncode == 1
    assert _check_names(proc, "FAIL")[0] == "models"
    assert "redirect" in proc.stdout
    assert _Recorder.headers_seen == []


def test_the_key_never_appears_even_when_echoed(gateway):
    server, gw = gateway
    gw.behavior["chat_status"] = 403
    gw.behavior["chat_error_body"] = {"detail": f"Key {KEY} not allowed"}

    proc = _smoke(server)

    assert proc.returncode == 1
    assert KEY not in proc.stdout + proc.stderr
    assert "***" in proc.stdout


def test_a_rejected_key_fails_without_showing_it(gateway):
    server, gw = gateway

    proc = _smoke(server, key="pb_wrong_key_for_this_gateway_000")

    assert proc.returncode == 1
    assert "pb_wrong_key_for_this_gateway_000" not in proc.stdout + proc.stderr
    assert "401" in proc.stdout


def test_missing_key_is_a_usage_error(gateway):
    server, gw = gateway

    proc = _smoke(server, key=None)
    assert proc.returncode == 2
    assert "PLUGBOARD_SMOKE_KEY" in proc.stderr
    assert gw.seen == []


def test_without_a_model_the_first_cli_model_of_the_key_is_chosen(gateway):
    """The default never needs Ollama: a CLI provider's model is picked."""
    server, gw = gateway
    gw.behavior["models"] = ["ollama:other", "gpt-x", MODEL]

    proc = _smoke(server, model=None)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    chats = [b for m, p, b in gw.seen if p == "/v1/chat/completions"]
    assert chats and {b["model"] for b in chats} == {MODEL}
    assert f"model={MODEL}" in proc.stdout


def test_an_agy_model_is_chosen_under_the_id_the_gateway_publishes(gateway):
    """The agy preset is published as ``antigravity-via-agy-cli:<model>``
    (the provider's name, slugified), never as ``Antigravity:``."""
    server, gw = gateway
    agy = "antigravity-via-agy-cli:gemini-3.8-flash-low"
    gw.behavior["models"] = ["ollama:other", agy]
    gw.behavior["me_allowed"] = [agy]

    proc = _smoke(server, model=None)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"model={agy}" in proc.stdout


def test_an_ollama_model_is_never_chosen_by_default(gateway):
    server, gw = gateway
    gw.behavior["models"] = ["ollama:other", "ollama:qwen3:8b"]
    gw.behavior["me_allowed"] = ["ollama:other"]

    proc = _smoke(server, model=None)

    assert proc.returncode == 2
    assert "--model" in proc.stderr and "PLUGBOARD_SMOKE_MODEL" in proc.stderr
    assert [p for m, p, b in gw.seen if p == "/v1/chat/completions"] == []


def test_a_model_the_key_may_not_use_is_not_chosen(gateway):
    server, gw = gateway
    gw.behavior["models"] = ["Codex:gpt-denied", MODEL]
    gw.behavior["compatible"] = {"Codex:gpt-denied": False, MODEL: True}

    proc = _smoke(server, model=None)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"model={MODEL}" in proc.stdout


def test_the_embeddings_skip_says_there_is_no_embedding_provider(gateway):
    server, gw = gateway

    proc = _smoke(server)

    (line,) = _lines(proc, "SKIP")
    assert "no embedding provider" in line


def test_model_can_be_given_on_the_command_line(gateway):
    server, gw = gateway

    proc = _smoke(server, "--model", MODEL, model=None)

    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_whole_run_is_bounded_by_the_deadline(gateway):
    server, gw = gateway
    gw.behavior["delay"] = 0.6

    started = time.monotonic()
    proc = _smoke(server, "--deadline", "1.5")
    elapsed = time.monotonic() - started

    assert proc.returncode == 1
    assert elapsed < 6
    assert "deadline" in proc.stdout


def test_an_unreachable_gateway_fails_fast():
    env = {k: v for k, v in os.environ.items() if not k.startswith("PLUGBOARD_SMOKE")}
    env.update(PLUGBOARD_SMOKE_KEY=KEY, PLUGBOARD_SMOKE_MODEL=MODEL)

    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--url", "http://127.0.0.1:9"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1
    assert _check_names(proc, "FAIL")[0] == "health"


def test_a_rejected_key_while_choosing_the_model_fails_without_showing_it(gateway):
    server, gw = gateway

    proc = _smoke(server, model=None, key="pb_wrong_key_for_this_gateway_000")

    assert proc.returncode == 1
    assert "pb_wrong_key_for_this_gateway_000" not in proc.stdout + proc.stderr
    assert "401" in proc.stderr


def test_a_model_list_flagged_compatible_is_trusted_over_allowed_models(gateway):
    server, gw = gateway
    gw.behavior["models"] = [MODEL]
    gw.behavior["compatible"] = {MODEL: True}
    gw.behavior["me_allowed"] = ["Codex:*"]

    proc = _smoke(server, model=None)

    assert proc.returncode == 0, proc.stdout + proc.stderr


_UNSUPPORTED_ON_CLI = {
    "detail": "Model route 'claude-code' is a CLI subscription: it cannot serve "
    "response_format. Nothing was called.",
    "code": "unsupported_on_cli",
    "unsupported": ["response_format"],
}


def test_a_cli_only_model_skips_the_structured_step(gateway):
    """With Ollama off the smoke model is a CLI subscription's, and a CLI route
    refuses response_format by contract (400 unsupported_on_cli, nothing
    called): the step does not apply, it is not a failed deployment."""
    server, gw = gateway
    gw.behavior["structured_error"] = (400, _UNSUPPORTED_ON_CLI)

    proc = _smoke(server)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _check_names(proc, "PASS") == ["health", "models", "chat", "stream"]
    assert _check_names(proc, "SKIP") == ["structured", "embeddings"]
    assert _lines(proc, "FAIL") == []
    assert "unsupported_on_cli" in "".join(_lines(proc, "SKIP"))


@pytest.mark.parametrize(
    "error",
    [
        (400, {"detail": "too long", "code": "context_length_exceeded"}),
        (400, {"detail": "bad request"}),
        (400, dict(_UNSUPPORTED_ON_CLI, unsupported=["tools"])),
        (503, _UNSUPPORTED_ON_CLI),
    ],
)
def test_any_other_refusal_of_the_structured_step_is_a_failure(gateway, error):
    server, gw = gateway
    gw.behavior["structured_error"] = error

    proc = _smoke(server)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _check_names(proc, "FAIL") == ["structured"]
