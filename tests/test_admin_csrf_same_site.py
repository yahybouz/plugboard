"""PB-SEC-06: same-site CSRF on requests made with the dashboard session.

The session cookie is ``SameSite=Strict``, which keeps it off cross-site
requests only: a page served on another port of the same host is same-site,
so the browser still attaches the cookie to the forms and fetches it sends.
A state-changing request authenticated by that cookie must come from the
dashboard's own origin; the admin key in a header, a client key, a request
without browser headers and the login itself are not affected.
"""

from __future__ import annotations

import pytest

_REFUSED = "Cross-site request refused"
# The admin_client fixture talks to http://test: the dashboard's own origin.
_OWN_ORIGIN = "http://test"


@pytest.fixture
async def session(admin_client, tmp_path, monkeypatch):
    """Cookie header of a fresh dashboard session, MFA and sessions isolated."""
    from plugboard import admin_mfa, admin_sessions, ratelimit

    monkeypatch.setattr(admin_mfa, "_MFA_FILE", tmp_path / "admin_mfa.json")
    monkeypatch.setattr(
        admin_sessions, "_path", lambda: tmp_path / "admin_sessions.json"
    )
    monkeypatch.delenv("PLUGBOARD_ADMIN_MFA_REQUIRED", raising=False)
    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()
    admin_client.cookies.clear()
    resp = await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    assert resp.status_code == 204
    raw = resp.headers.get("set-cookie", "")
    admin_client.cookies.clear()
    value = raw.split("plugboard_admin_session=", 1)[1].split(";", 1)[0]
    yield {"Cookie": f"plugboard_admin_session={value}"}
    ratelimit._auth_buckets.clear()
    ratelimit._lockouts.clear()


async def _verify(client, headers):
    """POST /admin/mfa/verify with a malformed code: 400 once admitted."""
    return await client.post(
        "/admin/mfa/verify", json={"code": "not-a-code"}, headers=headers
    )


async def _playground_chat(client, headers):
    """POST /v1/chat/completions with a non-object body: 400 once admitted,
    before any provider is called."""
    return await client.post(
        "/v1/chat/completions",
        content=b"[]",
        headers={"Content-Type": "application/json", **headers},
    )


# ── Refused: the session cookie sent by another origin ─────────────────────


@pytest.mark.parametrize("site", ["same-site", "cross-site", "none"])
async def test_session_post_from_another_origin_is_refused(admin_client, session, site):
    resp = await _verify(admin_client, {**session, "Sec-Fetch-Site": site})
    assert resp.status_code == 403
    assert resp.json()["detail"] == _REFUSED


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PUT", "/providers/missing", {}),
        ("PATCH", "/admin/suivi/tickets/not-a-number", {}),
        ("DELETE", "/providers/missing", None),
    ],
)
async def test_every_state_changing_method_is_checked(
    admin_client, session, method, path, body
):
    resp = await admin_client.request(
        method,
        path,
        json=body,
        headers={**session, "Sec-Fetch-Site": "same-site"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == _REFUSED


async def test_playground_chat_from_another_origin_is_refused(admin_client, session):
    resp = await _playground_chat(
        admin_client, {**session, "Sec-Fetch-Site": "same-site"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == _REFUSED


@pytest.mark.parametrize(
    "origin", ["http://localhost:9999", "http://test:8080", "https://evil.test", "null"]
)
async def test_without_fetch_metadata_a_foreign_origin_is_refused(
    admin_client, session, origin
):
    resp = await _verify(admin_client, {**session, "Origin": origin})
    assert resp.status_code == 403
    assert resp.json()["detail"] == _REFUSED


# ── Accepted ────────────────────────────────────────────────────────────────


async def test_session_post_from_the_dashboard_origin_is_accepted(
    admin_client, session
):
    resp = await _verify(
        admin_client,
        {**session, "Sec-Fetch-Site": "same-origin", "Origin": _OWN_ORIGIN},
    )
    assert resp.status_code == 400


async def test_playground_chat_from_the_dashboard_origin_is_accepted(
    admin_client, session
):
    resp = await _playground_chat(
        admin_client, {**session, "Sec-Fetch-Site": "same-origin"}
    )
    assert resp.status_code == 400


async def test_without_fetch_metadata_the_own_origin_is_accepted(admin_client, session):
    resp = await _verify(admin_client, {**session, "Origin": _OWN_ORIGIN})
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "host,origin",
    [
        ("localhost:8765", "http://localhost:8765"),
        ("[::1]:8765", "http://[::1]:8765"),
        # Behind a TLS proxy that keeps Host, the app itself sees plain HTTP.
        ("llm.example.com", "https://llm.example.com"),
    ],
)
async def test_without_fetch_metadata_the_host_origin_is_accepted(
    admin_client, session, host, origin
):
    resp = await _verify(admin_client, {**session, "Host": host, "Origin": origin})
    assert resp.status_code == 400


async def test_without_fetch_metadata_another_port_is_refused(admin_client, session):
    resp = await _verify(
        admin_client,
        {**session, "Host": "localhost:8765", "Origin": "http://localhost:3000"},
    )
    assert resp.status_code == 403


async def test_session_post_without_browser_headers_is_accepted(admin_client, session):
    resp = await _verify(admin_client, session)
    assert resp.status_code == 400


async def test_admin_key_header_from_another_origin_is_accepted(admin_client, session):
    cross_site = {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.test"}
    bearer = {"Authorization": "Bearer adminkey"}
    alone = await _verify(admin_client, {**bearer, **cross_site})
    with_cookie = await _verify(admin_client, {**bearer, **session, **cross_site})
    assert [alone.status_code, with_cookie.status_code] == [400, 400]


async def test_client_key_from_another_origin_is_accepted(
    admin_client, session, monkeypatch
):
    token = "pb_" + "c" * 40
    record = {"key_id": "k-test", "client_id": "client-test", "is_active": True}
    monkeypatch.setattr(
        "plugboard.app.get_key_record", lambda t: record if t == token else None
    )
    resp = await _playground_chat(
        admin_client,
        {
            **session,
            "Authorization": f"Bearer {token}",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert resp.status_code == 400


async def test_read_only_request_from_another_origin_is_accepted(admin_client, session):
    resp = await admin_client.get(
        "/admin/mfa/status", headers={**session, "Sec-Fetch-Site": "same-site"}
    )
    assert resp.status_code == 200


async def test_login_from_another_origin_is_not_checked(admin_client, session):
    resp = await admin_client.post(
        "/auth/admin/login",
        json={"key": "adminkey"},
        headers={**session, "Sec-Fetch-Site": "same-site"},
    )
    assert resp.status_code == 204


# ── Inventory: every state-changing route goes through a checked dependency ──


def _routes(routes):
    """Routes of the app, including those of included routers (FastAPI keeps
    them behind a wrapper since 0.13x; older versions flatten them)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _routes(inner.routes)
        elif hasattr(route, "dependant"):
            yield route


def _dependency_calls(dependant):
    stack, calls = [dependant], set()
    while stack:
        current = stack.pop()
        calls.add(current.call)
        stack.extend(current.dependencies)
    return calls


def test_every_state_changing_route_is_covered():
    import plugboard.app as app_module

    checked = {app_module._require_admin, app_module._resolve_client}
    # Never authenticated by the session cookie: the login itself, the
    # logout (no state but the caller's own session) and the client-key APIs.
    exempt = {
        "/auth/admin/login",
        "/auth/admin/logout",
        "/v1/decisions",
        "/v1/embeddings",
    }
    seen, uncovered = set(), []
    for route in _routes(app_module.app.routes):
        if not route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        seen.add(route.path)
        if route.path in exempt:
            continue
        if not checked & _dependency_calls(route.dependant):
            uncovered.append(route.path)
    assert uncovered == []
    # The routers included from other modules were walked too.
    assert {"/admin/suivi/tickets", "/admin/mfa", "/v1/decisions"} <= seen
