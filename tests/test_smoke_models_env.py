"""BUG-130: ``scripts/smoke_models.py`` honours PLUGBOARD_SKIP_DOTENV and
ignores the environment's proxy.

Same defect as BUG-91 (``scripts/run_mcp.py``): the script read ``.env`` even
when ``PLUGBOARD_SKIP_DOTENV=1`` (or its old ``LLM_ENABLER_`` name) asked to
ignore it. Its HTTP client also used ``HTTP_PROXY``/``HTTPS_PROXY`` from the
environment, so the gateway key went to the proxy. The opener now carries an
empty ``ProxyHandler`` (as ``smoke_consumers.py`` and ``recette_ollama_off.py``)
and still never follows a redirect (BUG-94).

No network: a fake ``dotenv`` module records whether it was called, and two
local servers stand for the gateway and the proxy.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-smoke-models-env-key-0123456789"
_OLD_SKIP = "LLM_" + "ENABLER_SKIP_DOTENV"


def _load(monkeypatch, calls: list):
    fake = types.ModuleType("dotenv")
    fake.load_dotenv = lambda *a, **k: calls.append((a, k)) or True
    monkeypatch.setitem(sys.modules, "dotenv", fake)
    spec = importlib.util.spec_from_file_location(
        "smoke_models_env_under_test", ROOT / "scripts" / "smoke_models.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("PLUGBOARD_SKIP_DOTENV", _OLD_SKIP):
        monkeypatch.delenv(name, raising=False)
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("name", ["PLUGBOARD_SKIP_DOTENV", _OLD_SKIP])
def test_skip_dotenv_leaves_env_file_unread(clean_env, name):
    clean_env.setenv(name, "1")
    calls: list = []
    _load(clean_env, calls)
    assert calls == []


def test_env_file_still_read_without_the_skip(clean_env):
    calls: list = []
    _load(clean_env, calls)
    assert len(calls) == 1


class _Gateway(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        body = b'{"object": "list", "data": []}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Proxy(_Gateway):
    seen: list = []


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_the_environment_proxy_is_ignored(clean_env):
    proxy = _serve(_Proxy)
    gateway = _serve(_Gateway)
    _Proxy.seen, _Gateway.seen = [], []
    proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        clean_env.setenv(name, proxy_url)
    clean_env.setenv("PLUGBOARD_SKIP_DOTENV", "1")
    try:
        module = _load(clean_env, [])
        clean_env.setattr(
            module, "BASE_URL", f"http://127.0.0.1:{gateway.server_address[1]}"
        )
        data = module._http_json("GET", "/v1/models", token=KEY, timeout=5)
    finally:
        for server in (proxy, gateway):
            server.shutdown()
            server.server_close()

    assert data == {"object": "list", "data": []}
    assert _Proxy.seen == []  # the key never went to the proxy
    assert _Gateway.seen == [("/v1/models", f"Bearer {KEY}")]


def test_redirects_are_still_refused(clean_env):
    """BUG-94 kept: the opener without a proxy still refuses redirects."""
    clean_env.setenv("PLUGBOARD_SKIP_DOTENV", "1")
    module = _load(clean_env, [])
    handlers = [type(h).__name__ for h in module._OPENER.handlers]
    assert "_NoRedirect" in handlers
    assert "HTTPRedirectHandler" not in handlers
    assert issubclass(module._NoRedirect, module.HTTPRedirectHandler)
