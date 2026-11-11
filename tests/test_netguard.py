"""PLU-7: protected providers never connect to a non-public address."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import socket
import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from aioresponses import aioresponses
from yarl import URL

from plugboard import alerts, netguard
from plugboard import audit as audit_mod

NON_PUBLIC = [
    "127.0.0.1",
    "10.0.0.1",
    "172.17.0.1",
    "169.254.169.254",
    "100.64.0.1",
    "198.18.0.1",
    "::1",
    "::ffff:10.0.0.1",
    "64:ff9b::a00:1",
    "2002:a00:1::1",
    "fe80::1%eth0",
]
PUBLIC = ["1.1.1.1", "::ffff:1.1.1.1", "2606:4700::1111"]


def _family(address: str) -> socket.AddressFamily:
    return socket.AF_INET6 if ":" in address else socket.AF_INET


# -- address check and socket guard ------------------------------------------------


@pytest.mark.parametrize("address", NON_PUBLIC)
def test_non_public_is_refused(address):
    assert netguard.ip_is_public(address) is False
    with pytest.raises(netguard.EgressRefused) as exc:
        netguard.refuse_non_public(
            (_family(address), socket.SOCK_STREAM, 6, "", (address, 443))
        )
    assert exc.value.errno == errno.EACCES


@pytest.mark.parametrize("address", PUBLIC)
def test_public_gets_a_socket(address):
    family = _family(address)
    sock = netguard.refuse_non_public(
        (family, socket.SOCK_STREAM, 6, "", (address, 443))
    )
    try:
        assert sock.family == family
    finally:
        sock.close()


def test_llm_pricing_reexports_ip_is_public():
    from plugboard import llm_pricing

    assert llm_pricing.ip_is_public is netguard.ip_is_public


# -- filtering resolver and public connector ---------------------------------------


class FakeResolver(AbstractResolver):
    """Answers every name with ``answers`` and counts its calls (no real DNS)."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.calls = 0
        self.closed = False

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        self.calls += 1
        return [
            ResolveResult(
                hostname=host,
                host=address,
                port=port,
                family=_family(address),
                proto=0,
                flags=socket.AI_NUMERICHOST,
            )
            for address in self.answers
        ]

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
async def upstream():
    """A local server counting ``GET /v1/models``, answering ``{"data": []}``."""
    state = SimpleNamespace(calls=0, port=0)

    async def models(request: web.Request) -> web.Response:
        state.calls += 1
        return web.json_response({"data": []})

    app = web.Application()
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.port = site.port
    try:
        yield state
    finally:
        await runner.cleanup()


async def test_rebinding_to_loopback_is_refused(upstream):
    fake = FakeResolver(["127.0.0.1", "::1"])
    async with netguard.client_session(
        True, connector=netguard.public_connector(inner=fake)
    ) as s:
        for _ in range(2):
            with pytest.raises(aiohttp.ClientConnectorError) as exc:
                await s.get(f"http://rebind.test:{upstream.port}/v1/models")
            assert netguard.is_egress_refusal(exc.value)
            assert exc.value.host == "rebind.test"
    assert upstream.calls == 0
    assert fake.calls == 2  # a refusal is never cached


async def test_mixed_answer_is_refused(upstream, monkeypatch):
    fake = FakeResolver(["1.1.1.1", "127.0.0.1"])
    with pytest.raises(netguard.EgressRefused):
        await netguard.PublicOnlyResolver(fake).resolve("rebind.test", 443)

    # The refusal must come from the resolver: no socket is ever asked for,
    # and none is opened even if the resolver lets the public answer through.
    asked = []

    def no_socket(addr_info):
        asked.append(addr_info[4][0])
        raise netguard.EgressRefused(errno.EACCES, "test: no socket")

    monkeypatch.setattr(netguard, "refuse_non_public", no_socket)
    async with netguard.client_session(
        True, connector=netguard.public_connector(inner=fake)
    ) as s:
        with pytest.raises(aiohttp.ClientConnectorDNSError) as exc:
            await s.get(f"http://rebind.test:{upstream.port}/v1/models")
    assert netguard.is_egress_refusal(exc.value)
    assert asked == []
    assert upstream.calls == 0


async def test_same_request_without_guard_reaches_server(upstream):
    connector = aiohttp.TCPConnector(resolver=FakeResolver(["127.0.0.1"]))
    async with aiohttp.ClientSession(connector=connector) as s:
        async with s.get(f"http://rebind.test:{upstream.port}/v1/models") as r:
            assert r.status == 200
    assert upstream.calls == 1


async def test_ip_literal_is_refused_by_socket_factory(upstream):
    async with netguard.client_session(True) as s:
        with pytest.raises(aiohttp.ClientConnectorError) as exc:
            await s.get(f"http://127.0.0.1:{upstream.port}/v1/models")
    assert netguard.is_egress_refusal(exc.value)
    assert upstream.calls == 0


async def test_client_session_public_ignores_proxy_and_plain_is_untouched():
    async with netguard.client_session(True) as s:
        assert s.trust_env is False
        assert s.connector._socket_factory is netguard.refuse_non_public
    async with netguard.client_session(False) as s:
        assert s.connector._socket_factory is None


async def test_other_connection_errors_are_not_refusals():
    with socket.socket() as probe:  # a local port nobody listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    async with aiohttp.ClientSession() as s:
        with pytest.raises(aiohttp.ClientConnectorError) as exc:
            await s.get(f"http://127.0.0.1:{port}/v1/models")
    assert not netguard.is_egress_refusal(exc.value)
    assert not netguard.is_egress_refusal(ValueError("not a connection error"))


async def test_resolver_close_closes_inner():
    fake = FakeResolver(["1.1.1.1"])
    resolver = netguard.PublicOnlyResolver(fake)
    assert await resolver.resolve("public.test", 443) == await fake.resolve(
        "public.test", 443
    )
    await resolver.close()
    assert fake.closed is True


@pytest.mark.parametrize(
    "provider,forced,protected",
    [
        (
            {"preset_key": "catalog-acme", "base_url": "https://api.acme.test/v1"},
            True,
            True,
        ),
        ({"preset_key": "openai", "base_url": "https://api.openai.com/v1"}, True, True),
        (
            {"preset_key": "anthropic", "base_url": "https://api.anthropic.com/v1"},
            True,
            True,
        ),
        (
            {
                "preset_key": "github-broker",
                "base_url": "https://models.github.ai/inference",
            },
            True,
            True,
        ),
        (
            {
                "preset_key": "chatgpt-oauth",
                "base_url": "https://chatgpt.com/backend-api/codex",
            },
            True,
            True,
        ),
        (
            {"preset_key": "agy-cli-subprocess", "base_url": "subprocess://agy"},
            False,
            False,
        ),
        ({"preset_key": "", "base_url": "https://x.test"}, False, False),
        ({"base_url": "http://fake-llm.test"}, False, False),
        (
            {"base_url": "http://localhost:11434", "provider_type": "sovereign"},
            False,
            False,
        ),
        ({"base_url": "https://x.test", "egress_guard": True}, False, True),
        # aiohttp lowercases the scheme, so an upper-case one is still HTTP(S).
        ({"base_url": "HTTPS://x.test", "egress_guard": True}, False, True),
        ({"base_url": "subprocess://agy", "egress_guard": True}, False, False),
    ],
)
def test_who_is_protected(provider, forced, protected):
    assert netguard.guard_is_forced(provider) is forced
    assert netguard.is_protected(provider) is protected


# -- refusal report (D-17) ---------------------------------------------------------


@pytest.fixture
def sent(monkeypatch, tmp_path):
    """Fresh report state, a private audit database and a recording fake alert."""
    calls: list[tuple[str, str]] = []

    async def fake_alert(provider_id: str, host: str) -> None:
        calls.append((provider_id, host))

    monkeypatch.setattr(netguard, "_last_report", {})
    monkeypatch.setattr(netguard, "_tasks", set())
    monkeypatch.setattr(audit_mod, "_DB_PATH", tmp_path / "audit.db")
    monkeypatch.setattr(alerts, "send_egress_refused_alert", fake_alert)
    return calls


async def test_report_refusal_once_per_window(monkeypatch, sent, caplog):
    caplog.set_level(logging.DEBUG, logger="plugboard.netguard")
    now = [1000.0]
    monkeypatch.setattr(netguard, "_clock", lambda: now[0])
    assert netguard.report_refusal("acme", "api.acme.test", "forward") is True
    assert netguard.report_refusal("acme", "api.acme.test", "forward") is False
    assert netguard.report_refusal("other", "o.test", "forward") is True
    now[0] += netguard.REPORT_WINDOW_S - 1
    assert netguard.report_refusal("acme", "api.acme.test", "probe") is False
    now[0] += 2
    assert netguard.report_refusal("acme", "api.acme.test", "watchdog") is True
    await asyncio.gather(*list(netguard._tasks))

    events = audit_mod.get_audit_log(action="provider.egress_refused")
    details = [(e["target_id"], json.loads(e["details"])) for e in events]
    assert {(e["actor"], e["target_type"]) for e in events} == {("system", "provider")}
    assert sorted((pid, d["host"], d["source"]) for pid, d in details) == [
        ("acme", "api.acme.test", "forward"),
        ("acme", "api.acme.test", "watchdog"),
        ("other", "o.test", "forward"),
    ]
    # The audit runs in threads, so the alerts may land in any order.
    assert sorted(sent) == [
        ("acme", "api.acme.test"),
        ("acme", "api.acme.test"),
        ("other", "o.test"),
    ]
    assert netguard._tasks == set()  # finished tasks drop their reference
    levels = [r.levelno for r in caplog.records if r.name == "plugboard.netguard"]
    assert levels.count(logging.ERROR) == 3
    assert levels.count(logging.DEBUG) == 2
    assert (
        "[acme] egress refused: api.acme.test resolves to a non-public address"
        in caplog.text
    )


async def test_report_refusal_does_not_block(monkeypatch, sent):
    monkeypatch.setattr(audit_mod, "log_action", lambda *a, **k: time.sleep(0.3))
    t0 = time.perf_counter()
    assert netguard.report_refusal("slow", "s.test", "forward") is True
    assert time.perf_counter() - t0 < 0.05
    await asyncio.gather(*list(netguard._tasks))
    assert sent == [("slow", "s.test")]


async def test_report_refusal_survives_audit_and_alert_failures(monkeypatch, sent):
    def broken_audit(*a, **k):
        raise RuntimeError("audit down")

    alerted = []

    async def broken_alert(provider_id, host):
        alerted.append((provider_id, host))
        raise RuntimeError("hook down")

    monkeypatch.setattr(audit_mod, "log_action", broken_audit)
    monkeypatch.setattr(alerts, "send_egress_refused_alert", broken_alert)
    assert netguard.report_refusal("acme", "api.acme.test", "forward") is True
    results = await asyncio.gather(*list(netguard._tasks), return_exceptions=True)
    assert results == [None]
    assert alerted == [("acme", "api.acme.test")]  # a failed audit still alerts


async def test_egress_alert_payload(monkeypatch):
    monkeypatch.setattr(alerts, "_WEBHOOK_URL", "http://hook.test/alerts")
    with aioresponses() as m:
        m.post("http://hook.test/alerts", status=200)
        await alerts.send_egress_refused_alert("acme", "api.acme.test")
        (call,) = m.requests[("POST", URL("http://hook.test/alerts"))]
    assert call.kwargs["json"] == {
        "type": "provider_egress_refused",
        "provider_id": "acme",
        "host": "api.acme.test",
        "message": (
            "Provider 'acme': api.acme.test resolves to a non-public address; "
            "requests are refused"
        ),
    }
    assert call.kwargs["timeout"].total == 5


async def test_egress_alert_without_webhook_sends_nothing(monkeypatch):
    monkeypatch.setattr(alerts, "_WEBHOOK_URL", "")
    with aioresponses() as m:
        await alerts.send_egress_refused_alert("acme", "api.acme.test")
        assert m.requests == {}
