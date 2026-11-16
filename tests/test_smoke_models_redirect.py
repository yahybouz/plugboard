"""BUG-94: ``scripts/smoke_models.py`` never sends the key across a redirect.

``urlopen`` follows a redirect and copies every header but the content ones,
``Authorization`` included: a gateway answering 302 to another address handed
the key to it. The script now follows the recette scripts' rule
(``scripts/recette_acces.py``): a redirect is never followed.

Two local servers: the "gateway" redirects, the "other" records what it gets.
python-dotenv is hidden while the script is imported, so no ``.env`` is read.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-smoke-models-key-0123456789"


class _Recorder(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def _answer(self):
        type(self).seen.append(
            (self.command, self.path, self.headers.get("Authorization"))
        )
        body = b'{"object": "list", "data": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer  # noqa: N815


class _Redirector(BaseHTTPRequestHandler):
    target = ""

    def log_message(self, *args):
        pass

    def _redirect(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(302)
        self.send_header("Location", type(self).target + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = _redirect  # noqa: N815


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def smoke(monkeypatch):
    monkeypatch.setitem(sys.modules, "dotenv", None)  # never read a .env
    spec = importlib.util.spec_from_file_location(
        "smoke_models_under_test", ROOT / "scripts" / "smoke_models.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    other = _serve(_Recorder)
    _Recorder.seen = []
    _Redirector.target = f"http://127.0.0.1:{other.server_address[1]}"
    gateway = _serve(_Redirector)
    monkeypatch.setattr(
        module, "BASE_URL", f"http://127.0.0.1:{gateway.server_address[1]}"
    )
    monkeypatch.setattr(module, "ADMIN_KEY", KEY)
    monkeypatch.setattr(module, "API_KEY", KEY)
    yield module
    for server in (gateway, other):
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "method, path, body",
    [("GET", "/v1/models", None), ("POST", "/v1/chat/completions", {"model": "m"})],
)
def test_a_redirect_is_not_followed(smoke, method, path, body):
    with pytest.raises(HTTPError) as caught:
        smoke._http_json(method, path, token=KEY, body=body, timeout=5)

    assert caught.value.code == 302
    assert _Recorder.seen == []  # the other address never got the key


def test_listing_models_behind_a_redirect_stops_and_says_why(smoke, capsys):
    with pytest.raises(SystemExit) as stopped:
        smoke._fetch_models(None)

    assert stopped.value.code == 2
    out = capsys.readouterr().out
    assert "redirect" in out and "not followed" in out
    assert KEY not in out
    assert _Recorder.seen == []
