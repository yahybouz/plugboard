"""Create, list and revoke personal dashboard keys over the admin API (PB-AUTH-01)."""

import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from plugboard import audit
from tests import people_helpers as ph


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


async def _create(client, name="person-one", headers=ph.ADMIN):
    client.cookies.clear()
    return await client.post("/admin/people", json={"name": name}, headers=headers)


async def test_creation_answers_the_key_and_secret_once(admin_client, env):
    r = await _create(admin_client)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"person", "key", "totp_secret", "otpauth_uri"}
    assert (body["person"]["status"], body["person"]["created_by"]) == (
        "pending",
        "admin-key",
    )
    listed = (await admin_client.get("/admin/people", headers=ph.ADMIN)).json()
    assert set(listed) == {"people"}
    assert [p["id"] for p in listed["people"]] == [body["person"]["id"]]
    text = json.dumps(listed)
    assert (
        body["key"] not in text
        and body["totp_secret"] not in text
        and "key_hash" not in text
    )


async def test_a_person_creates_and_revokes_keys(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    r = await _create(
        admin_client, "person-two", ph.bearer(made, ph.code_now(env, made))
    )
    assert r.json()["person"]["created_by"] == f"person:{pid}"
    ph.next_step(env)
    admin_client.cookies.clear()
    gone = await admin_client.delete(
        f"/admin/people/{r.json()['person']['id']}",
        headers=ph.bearer(made, ph.code_now(env, made)),
    )
    assert gone.status_code == 200 and gone.json()["revoked_by"] == f"person:{pid}"


@pytest.mark.parametrize(
    "name,status", [("two\nlines", 422), ("x" * 65, 422), ("", 422)]
)
async def test_invalid_names_are_422(admin_client, env, name, status):
    r = await _create(admin_client, name)
    assert r.status_code == status
    assert r.json()["detail"] == "Name must be 1 to 64 printable characters"


async def test_a_duplicate_name_is_409(admin_client, env):
    await _create(admin_client)
    r = await _create(admin_client, " PERSON-ONE ")
    assert (
        r.status_code == 409
        and r.json()["detail"] == "A live personal key already has this name"
    )


async def test_revoke_is_idempotent_and_audited_once(admin_client, env):
    pid = (await _create(admin_client)).json()["person"]["id"]
    for _ in range(2):
        r = await admin_client.delete(f"/admin/people/{pid}", headers=ph.ADMIN)
        assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert len(audit.get_audit_log(action="admin.person.revoked")) == 1


@pytest.mark.parametrize("pid", ["ap_000000000000", "nope"])
async def test_revoking_an_unknown_id_is_404(admin_client, env, pid):
    r = await admin_client.delete(f"/admin/people/{pid}", headers=ph.ADMIN)
    assert r.status_code == 404 and r.json()["detail"] == "Personal key not found"


async def test_a_revoked_person_is_refused_everywhere(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    assert (
        await admin_client.delete(f"/admin/people/{pid}", headers=ph.ADMIN)
    ).status_code == 200
    assert await ph.keys_status(admin_client, cookie) == 401
    ph.next_step(env)
    assert (
        await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made)))
        == 401
    )
    ph.next_step(env)
    r = await admin_client.post(
        "/auth/admin/login",
        json={"key": made["key"], "totp_code": ph.code_now(env, made)},
    )
    assert r.status_code == 401
    admin_client.cookies.clear()
    assert (await admin_client.get("/v1/models", headers=cookie)).status_code == 401
    page = await admin_client.get("/dashboard", headers=cookie)
    assert page.status_code in (302, 307) and "/login" in page.headers["location"]


async def test_a_person_can_revoke_themselves(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    cookie = await ph.login(admin_client, made["key"], ph.code_now(env, made))
    admin_client.cookies.clear()
    r = await admin_client.delete(f"/admin/people/{pid}", headers=cookie)
    assert r.status_code == 200 and r.json()["revoked_by"] == f"person:{pid}"
    assert await ph.keys_status(admin_client, cookie) == 401


async def test_no_key_or_secret_is_readable_after_creation(admin_client, env, caplog):
    caplog.set_level(logging.DEBUG)
    body = (await _create(admin_client)).json()
    pid = body["person"]["id"]
    made = {"key": body["key"], "totp_secret": body["totp_secret"]}
    await ph.login(admin_client, body["key"], ph.code_now(env, made))
    ph.next_step(env)
    await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made)))
    listed = (await admin_client.get("/admin/people", headers=ph.ADMIN)).text
    await admin_client.delete(f"/admin/people/{pid}", headers=ph.ADMIN)
    blobs = [
        json.dumps(audit.get_audit_log(limit=1000)),
        caplog.text,
        listed,
        env.store.read_text(),
        (env.dir / "admin_sessions.json").read_text(),
    ]
    for blob in blobs:
        assert body["key"] not in blob and body["totp_secret"] not in blob
    created = audit.get_audit_log(action="admin.person.created")[0]
    assert (created["target_type"], created["target_id"], created["details"]) == (
        "admin_person",
        pid,
        "person-one",
    )


async def test_the_routes_need_an_admin(admin_client, env):
    admin_client.cookies.clear()
    assert (await admin_client.get("/admin/people")).status_code == 401
    assert (
        await admin_client.post("/admin/people", json={"name": "person-one"})
    ).status_code == 401


async def test_without_admin_key_the_routes_answer_503(admin_client, env, monkeypatch):
    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")
    admin_client.cookies.clear()
    assert (await admin_client.get("/admin/people")).status_code == 503
    assert (
        await admin_client.post("/admin/people", json={"name": "person-one"})
    ).status_code == 503


async def test_with_auth_fully_off_creation_is_refused(
    env, monkeypatch, _providers_file
):
    from plugboard.app import app

    monkeypatch.setattr("plugboard.app._ADMIN_KEY", "")
    monkeypatch.setattr("plugboard.app._API_KEY", "")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as c:
        r = await c.post("/admin/people", json={"name": "person-one"})
    assert r.status_code == 503 and r.json()["detail"] == "Personal keys need ADMIN_KEY"


async def test_an_unreadable_store_answers_503_never_500(admin_client, env):
    env.store.write_text("{not json")
    detail = "The personal key store cannot be read or written: see the server log"
    for r in (
        await admin_client.get("/admin/people", headers=ph.ADMIN),
        await _create(admin_client),
        await admin_client.delete("/admin/people/ap_000000000000", headers=ph.ADMIN),
    ):
        assert r.status_code == 503 and r.json()["detail"] == detail
    assert await ph.keys_status(admin_client, ph.ADMIN) == 200


# -- Additions: write failures, the author of each audit entry ---------------

_STORE_DOWN = "The personal key store cannot be read or written: see the server log"


def _failing_write(exc):
    def write(*_args, **_kwargs):
        raise exc

    return write


@pytest.mark.parametrize(
    "exc",
    [OSError(28, "No space left on device"), PermissionError(13, "denied")],
    ids=["enospc", "eacces"],
)
async def test_a_store_that_cannot_be_written_answers_503_and_keeps_nothing(
    admin_client, env, monkeypatch, exc
):
    from plugboard.storage import StoreUnreadableError

    pid = (await _create(admin_client)).json()["person"]["id"]
    before = env.store.read_text()
    for failure in (exc, StoreUnreadableError("refused")):
        with monkeypatch.context() as m:
            m.setattr(
                "plugboard.admin_people.atomic_write_json", _failing_write(failure)
            )
            made = await _create(admin_client, "person-two")
            gone = await admin_client.delete(f"/admin/people/{pid}", headers=ph.ADMIN)
        for r in (made, gone):
            assert r.status_code == 503 and r.json() == {"detail": _STORE_DOWN}
        assert env.store.read_text() == before
    listed = (await admin_client.get("/admin/people", headers=ph.ADMIN)).json()
    assert [(p["id"], p["status"]) for p in listed["people"]] == [(pid, "pending")]
    assert len(audit.get_audit_log(action="admin.person.created")) == 1
    assert not audit.get_audit_log(action="admin.person.revoked")


async def test_the_audit_names_the_caller_never_the_key(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    body = (await _create(admin_client, "person-two")).json()
    ph.next_step(env)
    await admin_client.delete(
        f"/admin/people/{body['person']['id']}",
        headers=ph.bearer(made, ph.code_now(env, made)),
    )
    created = audit.get_audit_log(action="admin.person.created")[0]
    revoked = audit.get_audit_log(action="admin.person.revoked")[0]
    assert (created["principal"], created["principal_name"]) == ("admin-key", "")
    assert (revoked["principal"], revoked["principal_name"]) == (
        f"person:{pid}",
        "person-one",
    )
    assert (revoked["target_id"], revoked["details"]) == (
        body["person"]["id"],
        "person-two",
    )
    text = json.dumps([created, revoked])
    for secret in (made["key"], made["totp_secret"], body["key"], body["totp_secret"]):
        assert secret not in text
