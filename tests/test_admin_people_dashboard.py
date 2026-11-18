"""The People page of the dashboard and the login screen (PB-AUTH-01). Static checks."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


async def test_the_dashboard_has_the_people_tab(admin_client):
    admin_client.cookies.clear()
    await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    shell = (await admin_client.get("/dashboard")).text
    assert 'data-tab="people"' in shell and 'id="page-people"' in shell
    assert "/dashboard/people.js?v=" in shell and "/dashboard/app.js?v=30" in shell
    for element in (
        "new-person-key",
        "new-person-secret",
        "new-person-uri",
        "btn-person-copied",
    ):
        assert f'id="{element}"' in shell
    js = await admin_client.get("/dashboard/people.js")
    assert js.status_code == 200 and "/admin/people" in js.text


def test_app_js_routes_the_people_tab():
    app_js = (STATIC / "app.js").read_text()
    assert "people: loadPeople" in app_js and "window.loadPeoplePage" in app_js


def test_people_js_writes_text_only_and_keeps_nothing():
    js = (STATIC / "people.js").read_text()
    assert "localStorage" not in js and "sessionStorage" not in js
    assert all(v.strip() == '""' for v in re.findall(r"innerHTML\s*=\s*([^;]+);", js))
    assert "btn-person-copied" in js and "confirmAction(" in js


def test_the_login_screen_names_personal_keys():
    from plugboard.app import _LOGIN_HTML_TEMPLATE

    assert "Enter your admin key or your personal key" in _LOGIN_HTML_TEMPLATE
    assert 'placeholder="Admin or personal key"' in _LOGIN_HTML_TEMPLATE
