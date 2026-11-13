"""Behind a reverse proxy, limits count the real client (BUG-56).

run.py keeps uvicorn's proxy handling: X-Forwarded-For gives the client IP
only when the connection comes from an address in FORWARDED_ALLOW_IPS. The
app is wrapped here in the same middleware, as uvicorn runs it, so the per-IP
rate limit and the admin-login lockout are checked end to end.
"""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

PROXY = "172.18.0.1"  # the reverse proxy, as the container sees it


@pytest.fixture
def limits(monkeypatch, _providers_file, _admin_key):
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 2)
    for table in (rl._buckets, rl._auth_buckets, rl._lockouts):
        table.clear()
    yield rl
    for table in (rl._buckets, rl._auth_buckets, rl._lockouts):
        table.clear()


def _gateway(peer: str, trusted: str = PROXY) -> AsyncClient:
    from plugboard.app import app

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    return AsyncClient(
        transport=ASGITransport(app=wrapped, client=(peer, 40000)),
        base_url="http://test",
    )


async def _health(client, forwarded_for: str) -> int:
    r = await client.get("/health", headers={"X-Forwarded-For": forwarded_for})
    return r.status_code


async def test_rate_limit_counts_each_client_behind_a_trusted_proxy(limits):
    async with _gateway(PROXY) as client:
        assert [await _health(client, "203.0.113.1") for _ in range(3)] == [
            200,
            200,
            429,
        ]
        # Another client behind the same proxy has its own counter.
        assert await _health(client, "203.0.113.2") == 200


async def test_forwarded_for_from_an_untrusted_peer_is_ignored(limits):
    # The peer is not the listed proxy: its header is not believed, so a
    # client cannot pick a new IP for each request.
    async with _gateway("198.51.100.9") as client:
        codes = [await _health(client, f"203.0.113.{n}") for n in range(1, 4)]
    assert codes == [200, 200, 429]


async def _login(client, forwarded_for: str) -> int:
    r = await client.post(
        "/auth/admin/login",
        json={"key": "not-the-admin-key"},
        headers={"X-Forwarded-For": forwarded_for},
    )
    return r.status_code


async def test_login_lockout_hits_the_real_client_only(limits):
    limits._LIMIT = 0  # the per-IP request limit is not under test here
    async with _gateway(PROXY) as client:
        codes = [await _login(client, "203.0.113.1") for _ in range(6)]
        assert codes[-1] == 429  # locked out
        # The next user behind the proxy is not locked out with it.
        assert await _login(client, "203.0.113.2") == 401
    assert set(limits._lockouts) == {"203.0.113.1"}


async def test_login_lockout_cannot_be_dodged_with_a_forged_header(limits):
    limits._LIMIT = 0  # the per-IP request limit is not under test here
    async with _gateway("198.51.100.9") as client:
        codes = [await _login(client, f"203.0.113.{n}") for n in range(1, 7)]
    assert codes[-1] == 429
    assert set(limits._lockouts) == {"198.51.100.9"}


def test_stale_login_counters_are_purged(monkeypatch):
    # With the real client counted, every address that fails once leaves a
    # counter: they are dropped once their window or lockout is over.
    import plugboard.ratelimit as rl

    now = [1000.0]
    monkeypatch.setattr(rl.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(rl, "_auth_buckets", {})
    monkeypatch.setattr(rl, "_lockouts", {})
    for n in range(300):
        rl.record_auth_failure(f"client-{n}")
    for _ in range(rl._AUTH_LIMIT):
        rl.record_auth_failure("198.51.100.1")  # locked out
    assert len(rl._auth_buckets) == 300 and "198.51.100.1" in rl._lockouts
    now[0] += rl._LOCKOUT_DURATION + 1
    # Purged every _AUTH_PURGE_INTERVAL failures, not on each one.
    for _ in range(rl._AUTH_PURGE_INTERVAL):
        rl.record_auth_failure("192.0.2.1")
        rl._lockouts.pop("192.0.2.1", None)  # only the older entries matter here
    assert set(rl._auth_buckets) <= {"192.0.2.1"}
    assert rl._lockouts == {}


@pytest.mark.parametrize(
    "value, warned",
    [("*", True), ("172.18.0.1, *", True), ("172.18.0.1", False), ("", False)],
)
def test_trusting_every_peer_is_reported(value, warned):
    from plugboard.app import trusted_proxy_warning

    message = trusted_proxy_warning(value)
    assert bool(message) is warned
    if warned:
        assert "FORWARDED_ALLOW_IPS" in message and "lockout" in message


def test_trusting_every_peer_is_logged_at_boot(monkeypatch, caplog):
    from plugboard import app as app_mod

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    with caplog.at_level(logging.WARNING, logger=app_mod.log.name):
        app_mod._warn_trusted_proxies()
    assert any("FORWARDED_ALLOW_IPS" in r.getMessage() for r in caplog.records)
