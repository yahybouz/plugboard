"""The global limit and the auth lockout count a key, not a proxy (BUG-119).

Behind the production proxy every request arrives from one address. Counted
per address before authentication, 60 calls without a key blocked every
client, and wrong logins or MFA codes from anyone locked the operator out.

- A request with a valid key (the admin key, an active client key) or an
  admin session is counted under that key: ``key:<key_id>``, ``key:admin``.
- Anything else (no key, a wrong, forged or revoked key) is counted under the
  client address, ``ip:<address>``; a forged key never leaves it.
- The address is ``request.client.host``. uvicorn's ProxyHeadersMiddleware
  replaces it with ``X-Forwarded-For`` only for a peer listed in
  ``FORWARDED_ALLOW_IPS``; Plugboard reads no header for it. The app is
  wrapped here in that middleware, as uvicorn runs it.
- Wrong admin keys lock the address; wrong TOTP codes, which need the admin
  key or a session, lock ``key:admin``. The right admin key passes an address
  lockout.
"""

from __future__ import annotations

import types

import pytest
from httpx import ASGITransport, AsyncClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

PROXY = "172.18.0.1"  # the reverse proxy, as the container sees it
ADMIN = "adminkey"
TOTP_HEADER = "X-Plugboard-Admin-TOTP"


@pytest.fixture
def rl(monkeypatch, _providers_file, _admin_key, tmp_path):
    """The real limit (60/min), empty tables, an isolated key store."""
    import plugboard.apikeys as ak
    import plugboard.ratelimit as ratelimit

    keys_file = tmp_path / "api_keys.json"
    keys_file.write_text('{"keys": []}')
    monkeypatch.setattr(ak, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(ak, "KEYS_FILE", keys_file)
    monkeypatch.setattr(ak, "KEYS_LOCK", tmp_path / "api_keys.json.lock")
    ak._invalidate_cache()
    monkeypatch.setattr(ratelimit, "_LIMIT", 60)
    tables = (ratelimit._buckets, ratelimit._auth_buckets, ratelimit._lockouts)
    for table in tables:
        table.clear()
    yield ratelimit
    for table in tables:
        table.clear()
    ak._invalidate_cache()


def _gateway(peer: str, trusted: str = PROXY) -> AsyncClient:
    from plugboard.app import app

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=trusted)
    return AsyncClient(
        transport=ASGITransport(app=wrapped, client=(peer, 40000)),
        base_url="http://test",
    )


def _bearer(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _new_key(client_id: str) -> dict:
    import plugboard.apikeys as ak

    return ak.create_key(client_id=client_id, label="bug-119")


async def _health(client, headers=None) -> int:
    return (await client.get("/health", headers=headers or {})).status_code


# -- Global limit: whose bucket ----------------------------------------------


async def test_keyless_calls_fill_the_address_but_a_key_still_passes(rl):
    key = _new_key("alpha")
    async with _gateway(PROXY) as client:
        codes = [await _health(client) for _ in range(61)]
        assert codes[:60] == [200] * 60 and codes[60] == 429
        # Same proxy address, but a valid key: its own bucket.
        assert await _health(client, _bearer(key["key"])) == 200
        assert await _health(client, _bearer(ADMIN)) == 200
        r = await client.get("/v1/models", headers=_bearer(key["key"]))
        assert r.status_code == 200
    assert len(rl._buckets[f"ip:{PROXY}"]) == 60
    assert len(rl._buckets[f"key:{key['key_id']}"]) == 2
    assert len(rl._buckets["key:admin"]) == 1


async def test_two_valid_keys_from_one_address_have_their_own_buckets(rl):
    first, second = _new_key("alpha"), _new_key("beta")
    async with _gateway(PROXY) as client:
        assert await _health(client, _bearer(first["key"])) == 200
        assert await _health(client, _bearer(second["key"])) == 200
        assert await _health(client, _bearer(second["key"])) == 200
    assert len(rl._buckets[f"key:{first['key_id']}"]) == 1
    assert len(rl._buckets[f"key:{second['key_id']}"]) == 2
    assert f"ip:{PROXY}" not in rl._buckets


async def test_a_key_is_limited_in_its_own_bucket(rl, monkeypatch):
    monkeypatch.setattr(rl, "_LIMIT", 3)
    key = _new_key("alpha")
    async with _gateway(PROXY) as client:
        codes = [await _health(client, _bearer(key["key"])) for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        assert await _health(client) == 200  # the address is not spent


@pytest.mark.parametrize(
    "bearer",
    [
        "pb_" + "0" * 64,  # the shape of a client key, unknown
        "lle_forged",  # the old prefix, unknown
        "adminkey-but-not-quite",
        "not-a-key",
    ],
)
async def test_a_wrong_or_forged_key_stays_in_the_address_bucket(rl, bearer):
    async with _gateway(PROXY) as client:
        for _ in range(60):
            assert await _health(client) == 200
        # Sending any key does not buy a new bucket.
        assert await _health(client, _bearer(bearer)) == 429
    assert set(rl._buckets) == {f"ip:{PROXY}"}


async def test_a_revoked_key_counts_against_the_address(rl):
    import plugboard.apikeys as ak

    key = _new_key("alpha")
    assert ak.revoke_key(key["key_id"])
    async with _gateway(PROXY) as client:
        assert await _health(client, _bearer(key["key"])) == 200
        r = await client.get("/v1/models", headers=_bearer(key["key"]))
        assert r.status_code == 401
    assert set(rl._buckets) == {f"ip:{PROXY}"}
    assert len(rl._buckets[f"ip:{PROXY}"]) == 2


async def test_a_key_refused_for_its_client_id_counts_against_the_address(rl):
    import json

    import plugboard.apikeys as ak

    key = _new_key("alpha")
    data = json.loads(ak.KEYS_FILE.read_text())
    data["keys"][0]["client_id"] = "admin"  # D-40: refused when used
    ak._write(data)
    async with _gateway(PROXY) as client:
        assert await _health(client, _bearer(key["key"])) == 200
    assert set(rl._buckets) == {f"ip:{PROXY}"}


async def test_an_admin_session_counts_under_the_admin_key(rl):
    async with _gateway(PROXY) as client:
        r = await client.post("/auth/admin/login", json={"key": ADMIN})
        assert r.status_code == 204
        raw = r.headers["set-cookie"]
        value = raw.split("plugboard_admin_session=", 1)[1].split(";", 1)[0]
        client.cookies.clear()
        cookie = {"Cookie": f"plugboard_admin_session={value}"}
        assert await _health(client, cookie) == 200
    # The login itself carried no bearer: the address. Then the session.
    assert len(rl._buckets[f"ip:{PROXY}"]) == 1
    assert len(rl._buckets["key:admin"]) == 1


async def test_a_forged_session_cookie_counts_against_the_address(rl):
    async with _gateway(PROXY) as client:
        cookie = {"Cookie": "plugboard_admin_session=1800000000.nonce.deadbeef"}
        assert await _health(client, cookie) == 200
    assert set(rl._buckets) == {f"ip:{PROXY}"}


# -- The address: uvicorn's, never a header read by Plugboard ------------------


async def test_forwarded_for_from_an_untrusted_peer_does_not_change_the_bucket(rl):
    async with _gateway("198.51.100.9") as client:
        for n in range(1, 4):
            headers = {"X-Forwarded-For": f"203.0.113.{n}"}
            assert await _health(client, headers) == 200
        # Even a value shaped like a key bucket.
        assert await _health(client, {"X-Forwarded-For": "key:admin"}) == 200
    assert set(rl._buckets) == {"ip:198.51.100.9"}
    assert len(rl._buckets["ip:198.51.100.9"]) == 4


async def test_without_the_proxy_middleware_the_header_is_ignored(rl):
    from plugboard.app import app

    transport = ASGITransport(app=app, client=("198.51.100.9", 40000))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _health(client, {"X-Forwarded-For": "203.0.113.1"}) == 200
    assert set(rl._buckets) == {"ip:198.51.100.9"}


async def test_a_trusted_proxy_gives_the_rightmost_untrusted_address(rl):
    # uvicorn 0.54 reads X-Forwarded-For from the right and keeps the first
    # address that is not a trusted proxy: what a client puts on the left of
    # the header is not believed.
    async with _gateway(PROXY) as client:
        headers = {"X-Forwarded-For": "192.0.2.66, 100.64.0.7"}
        assert await _health(client, headers) == 200
    assert set(rl._buckets) == {"ip:100.64.0.7"}


# -- Auth lockout ---------------------------------------------------------------


@pytest.fixture
def mfa(rl, tmp_path, monkeypatch):
    """admin_mfa enforced on a private file with a fixed clock."""
    from plugboard import admin_mfa, admin_sessions

    clock = types.SimpleNamespace(t=1_800_000_000.0)
    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.setattr(admin_mfa, "time", types.SimpleNamespace(time=lambda: clock.t))
    monkeypatch.setattr(
        admin_sessions, "_path", lambda: tmp_path / "admin_sessions.json"
    )
    secret = admin_mfa.generate_secret()
    admin_mfa.enroll(secret)
    monkeypatch.setenv("PLUGBOARD_ADMIN_MFA_REQUIRED", "1")
    assert admin_mfa.is_required()
    valid = {admin_mfa._generate_code(secret, at=clock.t + 30 * s) for s in (-1, 0, 1)}
    wrong = next(f"{n:06d}" for n in range(1_000_000) if f"{n:06d}" not in valid)
    return types.SimpleNamespace(
        mod=admin_mfa,
        code=lambda: admin_mfa._generate_code(secret, at=clock.t),
        wrong=wrong,
    )


@pytest.fixture
def audited(monkeypatch):
    from plugboard import app as app_mod

    seen = []
    monkeypatch.setattr(
        app_mod,
        "_audit",
        lambda action, details="", ip="", **kw: seen.append((action, details, ip)),
    )
    return seen


def _from(address: str) -> dict:
    return {"X-Forwarded-For": address}


async def test_wrong_totp_codes_lock_the_admin_key_from_any_address(rl, mfa, audited):
    async with _gateway(PROXY) as client:
        for n in range(1, 6):  # five addresses, one wrong code each
            r = await client.get(
                "/admin/keys",
                headers={
                    **_bearer(ADMIN),
                    TOTP_HEADER: mfa.wrong,
                    **_from(f"100.64.0.{n}"),
                },
            )
            assert r.status_code == 401
        # A sixth address, with a good code: the key is locked.
        r = await client.get(
            "/admin/keys",
            headers={**_bearer(ADMIN), TOTP_HEADER: mfa.code(), **_from("100.64.0.9")},
        )
        assert r.status_code == 429
    assert set(rl._lockouts) == {"key:admin"}
    failures = [a for a in audited if a[0] == "auth.mfa_failed"]
    assert len(failures) == 5
    # The audit still says where each code came from.
    assert all(f"100.64.0.{n}" in failures[n - 1][1] for n in range(1, 6))
    assert [a[2] for a in failures] == [f"100.64.0.{n}" for n in range(1, 6)]


async def test_wrong_totp_codes_at_login_lock_the_admin_key(rl, mfa, audited):
    async with _gateway(PROXY) as client:
        codes = []
        for n in range(1, 6):
            r = await client.post(
                "/auth/admin/login",
                json={"key": ADMIN, "totp_code": mfa.wrong},
                headers=_from(f"100.64.0.{n}"),
            )
            codes.append(r.status_code)
        assert codes == [401, 401, 401, 401, 429]
        r = await client.post(
            "/auth/admin/login",
            json={"key": ADMIN, "totp_code": mfa.code()},
            headers=_from("100.64.0.9"),
        )
        assert r.status_code == 429 and r.headers.get("Retry-After")
    assert set(rl._lockouts) == {"key:admin"}


async def test_wrong_step_up_codes_lock_the_admin_key(rl, mfa, audited):
    from plugboard import app as app_mod

    request = types.SimpleNamespace(
        state=types.SimpleNamespace(),
        headers={TOTP_HEADER: mfa.wrong},
        client=types.SimpleNamespace(host="100.64.0.1"),
    )
    for _ in range(5):
        with pytest.raises(app_mod.HTTPException) as refused:
            app_mod._require_mfa_step_up(request)
        assert refused.value.status_code == 403
    request.client.host = "100.64.0.2"
    with pytest.raises(app_mod.HTTPException) as refused:
        app_mod._require_mfa_step_up(request, code=mfa.code())
    assert refused.value.status_code == 429
    assert set(rl._lockouts) == {"key:admin"}


async def test_wrong_keys_lock_the_address_but_not_the_right_admin_key(rl, audited):
    async with _gateway(PROXY) as client:
        headers = _from("100.64.0.1")
        codes = []
        for _ in range(6):
            r = await client.post(
                "/auth/admin/login", json={"key": "wrong-key"}, headers=headers
            )
            codes.append(r.status_code)
        assert codes == [401, 401, 401, 401, 429, 429]
        # The right admin key from the locked address still signs in...
        r = await client.post("/auth/admin/login", json={"key": ADMIN}, headers=headers)
        assert r.status_code == 204
        # ...and works with the admin API, while a wrong key is still refused.
        client.cookies.clear()
        r = await client.get("/admin/keys", headers={**_bearer(ADMIN), **headers})
        assert r.status_code == 200
        r = await client.post(
            "/auth/admin/login", json={"key": "wrong-key"}, headers=headers
        )
        assert r.status_code == 429
        # Another client behind the same proxy is not locked out.
        r = await client.post(
            "/auth/admin/login", json={"key": "wrong-key"}, headers=_from("100.64.0.2")
        )
        assert r.status_code == 401
    assert set(rl._lockouts) == {"ip:100.64.0.1"}
    blocked = [a for a in audited if a[0] == "auth.login_blocked"]
    assert blocked and all(a[2] == "100.64.0.1" for a in blocked)


async def test_an_address_lockout_does_not_lock_the_admin_key_with_mfa(
    rl, mfa, audited
):
    async with _gateway(PROXY) as client:
        headers = _from("100.64.0.1")
        for _ in range(5):
            await client.post(
                "/auth/admin/login", json={"key": "wrong-key"}, headers=headers
            )
        assert set(rl._lockouts) == {"ip:100.64.0.1"}
        r = await client.post(
            "/auth/admin/login",
            json={"key": ADMIN, "totp_code": mfa.code()},
            headers=headers,
        )
        assert r.status_code == 204
