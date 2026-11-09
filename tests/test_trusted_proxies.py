"""Client IP behind a reverse proxy: uvicorn's FORWARDED_ALLOW_IPS.

run.py keeps uvicorn's proxy handling (see test_auth_auto_login), so the
operator only lists the proxy in FORWARDED_ALLOW_IPS. These tests pin the
behaviour the README documents.
"""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from uvicorn.config import Config
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


def test_forwarded_allow_ips_read_from_environment(monkeypatch):
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "172.18.0.1")
    cfg = Config("plugboard.app:app")
    assert cfg.proxy_headers is True
    assert cfg.forwarded_allow_ips == "172.18.0.1"


def test_default_trusts_loopback_only(monkeypatch):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    assert Config("plugboard.app:app").forwarded_allow_ips == "127.0.0.1,::1"


def _client_ip_app(trusted):
    async def ip(request):
        return PlainTextResponse(request.client.host)

    inner = Starlette(routes=[Route("/", ip)])
    return ProxyHeadersMiddleware(inner, trusted_hosts=trusted)


def test_forwarded_ip_used_only_from_trusted_proxy():
    # TestClient connects as "testclient": trusted => forwarded IP is the client.
    c = TestClient(_client_ip_app("testclient"))
    assert c.get("/", headers={"X-Forwarded-For": "100.64.1.2"}).text == "100.64.1.2"
    # Untrusted peer (e.g. Docker bridge not listed): header ignored, so every
    # client behind the proxy shares the proxy address.
    c = TestClient(_client_ip_app("172.18.0.1"))
    assert c.get("/", headers={"X-Forwarded-For": "100.64.1.2"}).text == "testclient"
