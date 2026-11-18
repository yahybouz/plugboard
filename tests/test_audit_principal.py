"""Audit entries name the admin credential behind them (PB-AUTH-01)."""

import asyncio
import sqlite3
import threading

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route

from plugboard import audit
from tests import people_helpers as ph

OLD_TABLE = """CREATE TABLE audit_log (id TEXT PRIMARY KEY, ts TEXT NOT NULL,
    action TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'admin',
    target_type TEXT NOT NULL DEFAULT '', target_id TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '')"""


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "_DB_PATH", tmp_path / "audit.db")


def test_an_entry_outside_a_request_has_an_empty_principal(audit_db):
    entry = audit.log_action("x.test")
    assert (entry["principal"], entry["principal_name"]) == ("", "")
    assert audit.get_audit_log()[0]["principal"] == ""


def test_the_request_principal_is_recorded_then_forgotten(audit_db):
    token = audit.begin_request()
    try:
        audit.set_principal("person:ap_0123456789ab", "person-one")
        audit.log_action("x.test")
    finally:
        audit.end_request(token)
    row = audit.get_audit_log(action="x.test")[0]
    assert (row["principal"], row["principal_name"]) == (
        "person:ap_0123456789ab",
        "person-one",
    )
    audit.log_action("y.test")
    assert audit.get_audit_log(action="y.test")[0]["principal"] == ""


def test_an_explicit_principal_wins(audit_db):
    token = audit.begin_request()
    try:
        audit.set_principal("person:ap_0123456789ab", "person-one")
        entry = audit.log_action("x.test", principal="admin-key")
    finally:
        audit.end_request(token)
    assert (entry["principal"], entry["principal_name"]) == ("admin-key", "")


def test_set_principal_outside_a_request_does_nothing(audit_db):
    audit.set_principal("admin-key")
    assert audit.current_principal() == ("", "")


async def test_a_worker_thread_fills_the_request_principal(audit_db):
    token = audit.begin_request()
    try:
        await asyncio.to_thread(audit.set_principal, "admin-key", "")
        audit.log_action("z.test")
    finally:
        audit.end_request(token)
    assert audit.get_audit_log(action="z.test")[0]["principal"] == "admin-key"


def test_an_old_database_gains_the_columns(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute(OLD_TABLE)
        conn.execute(
            "INSERT INTO audit_log (id, ts, action) VALUES ('old1', '2026-01-01T00:00:00.000Z', 'key.create')"
        )
    conn.close()
    monkeypatch.setattr(audit, "_DB_PATH", db)
    audit.log_action("new.test", principal="admin-key")
    rows = {r["action"]: r for r in audit.get_audit_log()}
    assert (rows["key.create"]["principal"], rows["key.create"]["principal_name"]) == (
        "",
        "",
    )
    assert rows["new.test"]["principal"] == "admin-key"


@pytest.fixture
def env(tmp_path, monkeypatch):
    yield from ph.isolate(tmp_path, monkeypatch)


async def test_an_admin_key_action_names_the_admin_key(admin_client, env):
    admin_client.cookies.clear()
    r = await admin_client.post(
        "/admin/keys", json={"client_id": "acme-test"}, headers=ph.ADMIN
    )
    assert r.status_code == 201
    entry = audit.get_audit_log(action="key.create")[0]
    assert (entry["actor"], entry["principal"], entry["principal_name"]) == (
        "admin",
        "admin-key",
        "",
    )


async def test_a_personal_key_action_names_the_person(admin_client, env):
    made = ph.new_person()
    pid = made["person"]["id"]
    admin_client.cookies.clear()
    r = await admin_client.post(
        "/admin/keys",
        json={"client_id": "acme-test"},
        headers=ph.bearer(made, ph.code_now(env, made)),
    )
    assert r.status_code == 201
    entry = audit.get_audit_log(action="key.create")[0]
    assert (entry["principal"], entry["principal_name"]) == (
        f"person:{pid}",
        "person-one",
    )


async def test_a_sync_route_names_the_person_too(admin_client, env, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_DATA", str(env.dir / "data"))
    made = ph.new_person()
    admin_client.cookies.clear()
    r = await admin_client.post(
        "/admin/suivi/tickets",
        json={"titre": "ticket de test"},
        headers=ph.bearer(made, ph.code_now(env, made)),
    )
    assert r.status_code == 201
    entry = audit.get_audit_log(action="suivi.ticket.create")[0]
    assert entry["principal"] == f"person:{made['person']['id']}"


async def test_the_principal_does_not_leak_into_the_next_request(admin_client, env):
    made = ph.new_person()
    assert (
        await ph.keys_status(admin_client, ph.bearer(made, ph.code_now(env, made)))
        == 200
    )
    r = await admin_client.post("/auth/admin/login", json={"key": "not-the-key"})
    assert r.status_code == 401
    assert audit.get_audit_log(action="auth.login_failed")[0]["principal"] == ""


async def test_two_concurrent_requests_keep_their_own_principal(
    admin_client, env, monkeypatch
):
    from plugboard import app as app_mod

    made = ph.new_person()
    pid = made["person"]["id"]
    both_in = threading.Barrier(2, timeout=10)
    real_auth = app_mod._admin_request_auth

    def auth_then_wait(request):
        outcome = real_auth(request)
        both_in.wait()  # both principals are set before either request writes
        return outcome

    monkeypatch.setattr(app_mod, "_admin_request_auth", auth_then_wait)
    admin_client.cookies.clear()
    r1, r2 = await asyncio.gather(
        admin_client.post(
            "/admin/keys", json={"client_id": "acme-admin"}, headers=ph.ADMIN
        ),
        admin_client.post(
            "/admin/keys",
            json={"client_id": "acme-person"},
            headers=ph.bearer(made, ph.code_now(env, made)),
        ),
    )
    assert (r1.status_code, r2.status_code) == (201, 201)
    by_client = {e["details"]: e for e in audit.get_audit_log(action="key.create")}
    admin, person = by_client["acme-admin"], by_client["acme-person"]
    assert (admin["principal"], admin["principal_name"]) == ("admin-key", "")
    assert (person["principal"], person["principal_name"]) == (
        f"person:{pid}",
        "person-one",
    )


def _probe_app() -> Starlette:
    """The request middleware alone, with a route that answers, one that
    raises and one that streams (writing an entry while it streams)."""
    from plugboard.app import _RequestIdMiddleware

    async def ok(request):
        audit.set_principal("admin-key")
        audit.log_action("probe.ok")
        return PlainTextResponse("ok")

    async def boom(request):
        audit.set_principal("admin-key")
        raise RuntimeError("probe failure")

    async def stream(request):
        audit.set_principal("person:ap_0123456789ab", "person-one")

        async def body():
            yield b"a"
            audit.log_action("probe.stream")
            yield b"b"

        return StreamingResponse(body())

    return Starlette(
        routes=[Route("/ok", ok), Route("/boom", boom), Route("/stream", stream)],
        middleware=[Middleware(_RequestIdMiddleware)],
    )


@pytest.fixture
def holder_calls(monkeypatch):
    """Record each opening and closing of the request's principal holder."""
    calls = []
    real_begin, real_end = audit.begin_request, audit.end_request

    def begin():
        token = real_begin()
        calls.append(("begin", token))
        return token

    def end(token):
        try:
            real_end(token)  # ValueError if not in the context that opened it
        except Exception as exc:
            calls.append(("end", token, repr(exc)))
            raise
        calls.append(("end", token, audit._principal_var.get()))

    monkeypatch.setattr(audit, "begin_request", begin)
    monkeypatch.setattr(audit, "end_request", end)
    return calls


@pytest.mark.parametrize(
    "path, status", [("/ok", 200), ("/boom", 500), ("/stream", 200)]
)
async def test_the_holder_is_closed_in_the_context_that_opened_it(
    audit_db, holder_calls, path, status
):
    transport = ASGITransport(app=_probe_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get(path)
    assert r.status_code == status
    assert [call[0] for call in holder_calls] == ["begin", "end"]
    (_, opened), (_, closed, left) = holder_calls
    assert closed is opened
    assert left is None  # reset in the same context: nothing left behind


async def test_an_entry_written_while_streaming_keeps_the_principal(audit_db):
    transport = ASGITransport(app=_probe_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/stream")
    assert r.text == "ab"
    entry = audit.get_audit_log(action="probe.stream")[0]
    assert (entry["principal"], entry["principal_name"]) == (
        "person:ap_0123456789ab",
        "person-one",
    )


def test_a_name_without_a_principal_is_never_written(audit_db):
    entry = audit.log_action("x.test", principal_name="person-one")
    assert (entry["principal"], entry["principal_name"]) == ("", "")
    entry = audit.log_action("y.test", principal="", principal_name="person-one")
    assert (entry["principal"], entry["principal_name"]) == ("", "")
    rows = audit.get_audit_log()
    assert [r["principal_name"] for r in rows] == ["", ""]


def test_a_system_entry_never_takes_the_request_principal(audit_db):
    token = audit.begin_request()
    try:
        audit.set_principal("person:ap_0123456789ab", "person-one")
        entry = audit.log_action("x.test", actor="system")
    finally:
        audit.end_request(token)
    assert (entry["principal"], entry["principal_name"]) == ("", "")


async def test_a_background_task_started_by_a_person_writes_without_them(
    admin_client, env, monkeypatch
):
    from plugboard import app as app_mod

    class Scheduler:
        running_since = None

        async def run_now(self, reason):
            await asyncio.sleep(0)  # runs on after the request has answered
            audit.log_action("probe.system", actor="system")

    state = app_mod.app.state
    monkeypatch.setattr(state, "pricing_scheduler", Scheduler(), raising=False)
    monkeypatch.setattr(state, "model_availability_scheduler", None, raising=False)
    made = ph.new_person()
    admin_client.cookies.clear()
    before = set(app_mod._background_tasks)
    r = await admin_client.post(
        "/admin/auto-update/run", headers=ph.bearer(made, ph.code_now(env, made))
    )
    assert r.status_code == 202
    await asyncio.gather(*(set(app_mod._background_tasks) - before))
    started = audit.get_audit_log(action="auto_update.run")[0]
    assert started["principal"] == f"person:{made['person']['id']}"
    entry = audit.get_audit_log(action="probe.system")[0]
    assert (entry["principal"], entry["principal_name"]) == ("", "")
