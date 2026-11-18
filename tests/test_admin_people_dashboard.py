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
    assert not re.search(r"\bconsole\.[a-z]+\s*\(", js)
    for sink in (
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "createContextualFragment",
        "DOMParser",
        "srcdoc",
    ):
        assert sink not in js, sink
    # Every innerHTML in the file only empties an element, whatever the spacing.
    every = re.findall(r"innerHTML", js)
    emptying = re.findall(r"\.innerHTML\s*=\s*(?:\"\"|'')\s*;", js)
    assert len(every) == len(emptying)
    assert "btn-person-copied" in js and "confirmAction(" in js


def test_people_js_keeps_the_one_time_values_until_they_are_stored():
    js = (STATIC / "people.js").read_text()
    # Only two things clear them: "I have stored them" and leaving the page.
    # A tab change does not: a key that lands while another tab is open
    # must still be there when its tab comes back.
    assert 'el("btn-person-copied").addEventListener("click", forget);' in js
    assert 'window.addEventListener("pagehide", forget);' in js
    assert (
        len(re.findall(r"\bforget\b", js)) == 3
    )  # the definition and the two listeners
    assert "hashchange" not in js


def test_the_dashboard_relogin_takes_a_personal_key_and_its_code():
    html = (STATIC / "index.html").read_text()
    modal = html[html.index('id="login-modal"') : html.index("<!-- App shell -->")]
    assert "Enter your admin key or your personal key" in modal
    assert 'placeholder="Admin or personal key"' in modal
    code = re.search(r"<input[^>]*\bid=\"login-code\"[^>]*>", modal)
    assert code and 'autocomplete="one-time-code"' in code.group(0)
    assert 'inputmode="numeric"' in code.group(0) and "aria-label=" in code.group(0)
    app_js = (STATIC / "app.js").read_text()
    login = app_js[
        app_js.index("async function tryLogin") : app_js.index("function showApp")
    ]
    # The code travels in the JSON body, never in the URL.
    assert re.findall(r"fetch\(([^,]+),", login) == ['"/auth/admin/login"']
    assert "body.totp_code = code" in login and "JSON.stringify(body)" in login
    assert "mfa_required" in login and 'startsWith("pba_")' in login


def test_the_fallback_error_fits_every_key():
    from plugboard.app import _LOGIN_HTML_TEMPLATE

    app_js = (STATIC / "app.js").read_text()
    for text in (_LOGIN_HTML_TEMPLATE, app_js):
        assert "Invalid admin key" not in text
        assert "Invalid key or code" in text


def test_the_login_screen_names_personal_keys():
    from plugboard.app import _LOGIN_HTML_TEMPLATE

    assert "Enter your admin key or your personal key" in _LOGIN_HTML_TEMPLATE
    assert 'placeholder="Admin or personal key"' in _LOGIN_HTML_TEMPLATE
