"""PLU-7: protected providers never connect to a non-public address."""

from __future__ import annotations

import errno
import socket
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult

from plugboard import netguard

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


async def test_mixed_answer_is_refused(upstream):
    fake = FakeResolver(["1.1.1.1", "127.0.0.1"])
    async with netguard.client_session(
        True, connector=netguard.public_connector(inner=fake)
    ) as s:
        with pytest.raises(aiohttp.ClientConnectorError) as exc:
            await s.get(f"http://rebind.test:{upstream.port}/v1/models")
    assert netguard.is_egress_refusal(exc.value)
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
