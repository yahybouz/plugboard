"""Personal keys in the global limit, /health/ready and the client plane (PB-AUTH-01)."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import admin_people, provider_watchdog, readiness, usage
from tests import people_helpers as ph

PROXY = "172.18.0.1"


@pytest.fixture
def env(tmp_path, monkeypatch, _providers_file, _admin_key):
    """Real global limit (60/min), readiness isolated as in test_readiness_visibility."""
    import plugboard.ratelimit as rl

    monkeypatch.setattr(rl, "_LIMIT", 60)
    rl._buckets.clear()
    monkeypatch.setattr(usage, "_DB_PATH", tmp_path / "usage.db")
    monkeypatch.setattr(usage, "_schema_ready", {})
    monkeypatch.setattr(
        provider_watchdog,
        "get_watchdog_status",
        lambda: {"running": True, "check_interval_s": 30, "providers": {}},
    )
    readiness.reset_cache()
    yield from ph.isolate(tmp_path, monkeypatch)
    rl._buckets.clear()


def _client():
    from plugboard.app import app

    return AsyncClient(
        transport=ASGITransport(app=app, client=(PROXY, 40000)),
        base_url="http://test",
    )


async def test_a_valid_personal_key_has_its_own_bucket(env):
    import plugboard.ratelimit as rl

    made = ph.new_person()
    async with _client() as c:
        assert (await c.get("/health", headers=ph.bearer(made))).status_code == 200
    assert len(rl._buckets[f"key:admin:{made['person']['id']}"]) == 1
    assert f"ip:{PROXY}" not in rl._buckets


@pytest.mark.parametrize("revoked", [True, False])
async def test_a_revoked_or_unknown_personal_key_stays_on_the_address(env, revoked):
    import plugboard.ratelimit as rl

    made = ph.new_person()
    key = made["key"] if revoked else "pba_" + "C" * 43
    if revoked:
        admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
    async with _client() as c:
        await c.get("/health", headers={"Authorization": f"Bearer {key}"})
    assert set(rl._buckets) == {f"ip:{PROXY}"}


async def test_a_person_session_counts_under_the_person(env):
    import plugboard.ratelimit as rl

    made = ph.new_person()
    async with _client() as c:
        cookie = await ph.login(c, made["key"], ph.code_now(env, made))
        assert (await c.get("/health", headers=cookie)).status_code == 200
    assert len(rl._buckets[f"key:admin:{made['person']['id']}"]) == 1


async def test_ready_checks_need_a_session_not_a_bare_personal_key(env):
    made = ph.new_person()
    async with _client() as c:
        bare = await c.get("/health/ready", headers=ph.bearer(made))
        assert set(bare.json()) == {"ready", "version"}
        cookie = await ph.login(c, made["key"], ph.code_now(env, made))
        c.cookies.clear()
        assert "checks" in (await c.get("/health/ready", headers=cookie)).json()
        admin_people.revoke(made["person"]["id"], revoked_by="admin-key")
        c.cookies.clear()
        assert set((await c.get("/health/ready", headers=cookie)).json()) == {
            "ready",
            "version",
        }


async def test_a_personal_key_is_refused_on_the_client_plane(env):
    made = ph.new_person()
    async with _client() as c:
        r = await c.get("/v1/models", headers=ph.bearer(made, ph.code_now(env, made)))
    assert r.status_code == 401


# -- Additions: the client plane of a person's session, hand-edited records ---


async def test_a_person_session_is_the_admin_client_until_revoked(env):
    """A person is an admin: their session is the admin client on /v1
    (Playground), counted under their own bucket; revoked, it is refused."""
    import plugboard.ratelimit as rl

    made = ph.new_person()
    pid = made["person"]["id"]
    async with _client() as c:
        cookie = await ph.login(c, made["key"], ph.code_now(env, made))
        c.cookies.clear()
        me = await c.get("/v1/me", headers=cookie)
        assert me.status_code == 200 and me.json()["client_id"] == "admin"
        admin_people.revoke(pid, revoked_by="admin-key")
        c.cookies.clear()
        assert (await c.get("/v1/me", headers=cookie)).status_code == 401
    assert len(rl._buckets[f"key:admin:{pid}"]) == 1
    assert "key:admin" not in rl._buckets


async def test_a_hand_edited_personal_record_stays_on_the_address(env):
    import plugboard.ratelimit as rl

    made = ph.new_person()
    data = json.loads(env.store.read_text())
    data["people"][0]["id"] = "not-an-id"
    env.store.write_text(json.dumps(data))
    async with _client() as c:
        assert (await c.get("/health", headers=ph.bearer(made))).status_code == 200
    assert set(rl._buckets) == {f"ip:{PROXY}"}


async def test_an_admin_key_with_the_personal_prefix_keeps_the_admin_bucket(
    env, monkeypatch
):
    import plugboard.ratelimit as rl

    admin_key = "pba_" + "Z" * 43
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", admin_key)
    async with _client() as c:
        await c.get("/health", headers={"Authorization": f"Bearer {admin_key}"})
    assert set(rl._buckets) == {"key:admin"}


# -- An unreadable store: personal keys and their sessions fall back ---------


@pytest.mark.parametrize("live", [True, False])
async def test_an_unreadable_store_counts_any_personal_key_on_the_address(env, live):
    import plugboard.ratelimit as rl

    made = ph.new_person()
    key = made["key"] if live else "pba_" + "C" * 43
    env.store.write_text("{not json")
    async with _client() as c:
        await c.get("/health", headers={"Authorization": f"Bearer {key}"})
    assert set(rl._buckets) == {f"ip:{PROXY}"}


async def test_a_person_session_is_refused_once_the_store_is_unreadable(env):
    import plugboard.ratelimit as rl

    made = ph.new_person()
    async with _client() as c:
        cookie = await ph.login(c, made["key"], ph.code_now(env, made))
        assert await ph.keys_status(c, cookie) == 200
        env.store.write_text("{not json")
        rl._buckets.clear()
        assert await ph.keys_status(c, cookie) == 401
        c.cookies.clear()
        assert (await c.get("/v1/me", headers=cookie)).status_code == 401
    assert set(rl._buckets) == {f"ip:{PROXY}"}
