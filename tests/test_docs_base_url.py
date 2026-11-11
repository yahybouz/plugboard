"""PLUGBOARD_DOCS_BASE_URL turns the dashboard's docs links into URLs (PB-REL-04).

The provider connection test answers with a ``docs`` field such as
``docs/guide/providers-and-subscriptions.md#test-the-connection``: a path in
the repository, which a dashboard user cannot open. With the optional
variable set to where the repository's files are published, the field is an
absolute URL and the dashboard shows it as a link. No /guide route is served.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from aioresponses import aioresponses

DOC = "docs/guide/providers-and-subscriptions.md#test-the-connection"
ADMIN = {"Authorization": "Bearer adminkey"}
APP_JS = Path(__file__).resolve().parent.parent / "static" / "app.js"


async def _failed_test(admin_client) -> dict:
    """A connection test that fails (timeout), so the answer names the docs."""
    with aioresponses(passthrough=["http://test"]) as mock:
        mock.get("http://custom.test/v1/models", exception=asyncio.TimeoutError())
        resp = await admin_client.post(
            "/admin/direct-providers/test",
            headers=ADMIN,
            json={"base_url": "http://custom.test/v1", "api_key": "k"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "unreachable"
    return body


@pytest.mark.asyncio
async def test_docs_link_stays_a_repository_path_by_default(admin_client, monkeypatch):
    monkeypatch.delenv("PLUGBOARD_DOCS_BASE_URL", raising=False)
    assert (await _failed_test(admin_client))["docs"] == DOC


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base",
    [
        "https://gitlab.example/acme/plugboard/-/blob/main",
        "https://gitlab.example/acme/plugboard/-/blob/main/",
        "  https://gitlab.example/acme/plugboard/-/blob/main  ",
    ],
)
async def test_docs_link_is_absolute_under_the_base_url(
    admin_client, monkeypatch, base
):
    monkeypatch.setenv("PLUGBOARD_DOCS_BASE_URL", base)
    body = await _failed_test(admin_client)
    assert body["docs"] == "https://gitlab.example/acme/plugboard/-/blob/main/" + DOC


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base",
    ["javascript:alert(1)", "ftp://docs.example/plugboard", "docs.example", "https://"],
)
async def test_a_base_url_that_is_not_http_is_ignored(
    admin_client, monkeypatch, caplog, base
):
    monkeypatch.setenv("PLUGBOARD_DOCS_BASE_URL", base)
    assert (await _failed_test(admin_client))["docs"] == DOC
    assert "PLUGBOARD_DOCS_BASE_URL" in caplog.text


# ── The dashboard shows an absolute link as a link ──────────────────────────


def _js_function(source: str, name: str) -> str:
    """The text of a top-level ``function name(...) {...}`` of app.js."""
    match = re.search(rf"^function {name}\(.*?^}}$", source, re.M | re.S)
    assert match, f"{name} not found in static/app.js"
    return match.group(0)


def _render_docs(value: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    source = APP_JS.read_text(encoding="utf-8")
    script = "\n".join(
        [
            'const window = {location: {href: "http://localhost:8765/dashboard"}};',
            _js_function(source, "_escape"),
            _js_function(source, "_safeHref"),
            _js_function(source, "_docsHtml"),
            f"process.stdout.write(_docsHtml({json.dumps(value)}));",
        ]
    )
    run = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_dashboard_shows_an_absolute_docs_link_as_a_link():
    url = "https://gitlab.example/acme/plugboard/-/blob/main/" + DOC
    html = _render_docs(url)
    assert html.startswith(f'<a href="{url}"')
    assert 'target="_blank"' in html and 'rel="noopener noreferrer"' in html


def test_dashboard_keeps_a_repository_path_as_text():
    assert _render_docs(DOC) == DOC


def test_dashboard_never_links_another_scheme():
    html = _render_docs('javascript:alert("x")')
    assert "<a" not in html
    assert html == "javascript:alert(&quot;x&quot;)"
