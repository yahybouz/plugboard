"""Local login rendering regression; browser traffic is fulfilled in memory.

Set ``PLUGBOARD_RUN_REQUIRE_BROWSER=1`` to fail if a browser is unavailable.
Set ``PLUGBOARD_RUN_SCREENSHOT_DIR`` for private synthetic 390x844 PNGs.
"""

from __future__ import annotations

import json
import os
from html.parser import HTMLParser
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


def _browser_unavailable(reason: str) -> None:
    message = f"Browser unavailable; visual validation NOT run: {reason}"
    if os.environ.get("PLUGBOARD_RUN_REQUIRE_BROWSER") == "1":
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


@pytest.mark.parametrize("required", [False, True])
def test_browser_unavailable_respects_run_switch(monkeypatch, required):
    monkeypatch.setenv("PLUGBOARD_RUN_REQUIRE_BROWSER", "1" if required else "0")
    outcome = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(outcome, match="Browser unavailable.*synthetic missing browser"):
        _browser_unavailable("synthetic missing browser")


@pytest.fixture
async def login_response(monkeypatch):
    from plugboard import app as app_module

    monkeypatch.setenv("PLUGBOARD_LOCAL_AUTO_LOGIN", "0")
    monkeypatch.setattr(app_module, "_ADMIN_KEY", "synthetic-mobile-admin")
    async with AsyncClient(
        transport=ASGITransport(app=app_module.app), base_url="http://test"
    ) as client:
        response = await client.get("/login")
    assert response.status_code == 200
    return response


async def test_served_login_viewport_preserves_user_zoom(login_response):
    class Viewports(HTMLParser):
        def __init__(self):
            super().__init__()
            self.contents: list[str] = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "meta" and attrs.get("name") == "viewport":
                self.contents.append(attrs.get("content", ""))

    parser = Viewports()
    parser.feed(login_response.text)
    assert len(parser.contents) == 1, "Served /login needs a mobile viewport"
    directives = dict(
        part.strip().split("=", 1) for part in parser.contents[0].split(",")
    )
    assert directives == {"width": "device-width", "initial-scale": "1"}


async def test_served_login_allows_short_viewport_and_mobile_focus(login_response):
    # Keep these guards runnable when the host cannot launch a browser.
    css = login_response.text.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "min-height:100vh" in css
    assert "min-height:100dvh" in css
    assert ";height:100vh" not in css
    assert "margin:auto" in css
    assert "flex-shrink:0" in css
    assert "max-width:100%" in css
    assert (
        "@media(max-width:600px),(any-pointer:coarse){input{font-size:1rem}input,button{min-height:44px}}"
        in css
    )


@pytest.mark.parametrize(
    ("width", "height", "mobile"),
    [
        (390, 844, True),
        (320, 568, True),
        (390, 320, True),
        (844, 390, True),
        (1024, 768, True),
        (1280, 900, False),
    ],
)
async def test_login_and_mfa_layout(login_response, width, height, mobile):
    try:
        from playwright import async_api as playwright
    except ModuleNotFoundError as exc:
        if exc.name not in ("playwright", "playwright.async_api"):
            raise
        _browser_unavailable("Playwright is not installed; no download allowed")
    async with playwright.async_playwright() as pw:
        executable = Path(pw.chromium.executable_path)
        if not executable.exists():
            executable = Path(
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            )
        if not executable.exists():
            _browser_unavailable("No installed Chromium/Chrome; no download allowed")
        try:
            browser = await pw.chromium.launch(
                executable_path=str(executable),
                args=["--disable-background-networking", "--disable-component-update"],
            )
        except playwright.Error as exc:
            # A generic TargetClosedError can also signal an unexpected crash.
            # Only known host restrictions or missing dependencies permit a skip.
            if not any(
                marker in str(exc)
                for marker in (
                    "Operation not permitted",
                    "Permission denied",
                    "Host system is missing dependencies",
                    "error while loading shared libraries",
                    "Executable doesn't exist",
                )
            ):
                raise
            _browser_unavailable(
                f"Host cannot launch browser: {str(exc).splitlines()[0]}"
            )
        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
                is_mobile=mobile,
                has_touch=mobile,
                device_scale_factor=3 if mobile else 1,
                service_workers="block",
            )
            submitted = []

            async def intercept(route):
                if route.request.url == "http://test/login":
                    await route.fulfill(
                        status=200,
                        headers=dict(login_response.headers),
                        body=login_response.content,
                    )
                elif route.request.url == "http://test/auth/admin/login":
                    submitted.append(route.request.post_data_json)
                    await route.fulfill(
                        status=401,
                        content_type="application/json",
                        body=json.dumps(
                            {"mfa_required": True}
                            if len(submitted) == 1
                            else {"detail": "Synthetic MFA code rejected"}
                        ),
                    )
                else:
                    await route.abort()

            await context.route("**/*", intercept)
            page = await context.new_page()
            await page.goto("http://test/login")
            for mfa in (False, True):
                if mfa:
                    await page.locator("#k").fill("synthetic-mobile-admin")
                    await page.locator("#b").click()
                    await page.locator("#m").wait_for(state="visible")
                    assert await page.locator("#b").inner_text() == "Verify MFA"
                    assert await page.locator("#m").evaluate(
                        "el => el === document.activeElement"
                    )
                metrics = await page.evaluate("""() => ({
                    width: innerWidth, scale: visualViewport.scale,
                    scrollWidth: document.documentElement.scrollWidth,
                    box: (() => { const r = document.querySelector('.box')
                        .getBoundingClientRect();
                        return {left:r.left, right:r.right, top:r.top + scrollY}; })(),
                    fontSize: parseFloat(getComputedStyle(
                        document.querySelector('#k')).fontSize)
                })""")
                print(f"layout {width}x{height} mobile={mobile} mfa={mfa}: {metrics}")
                assert metrics["width"] == width
                assert metrics["scale"] == pytest.approx(1)
                assert metrics["scrollWidth"] <= width
                assert 0 <= metrics["box"]["left"] < metrics["box"]["right"] <= width
                # Offscreen content must remain reachable by vertical scrolling.
                assert metrics["box"]["top"] >= 0
                if mobile:
                    assert metrics["fontSize"] >= 16
                else:
                    assert metrics["fontSize"] == 14
                for selector in ("#k", "#b", "#m") if mfa else ("#k", "#b"):
                    element = page.locator(selector)
                    await element.scroll_into_view_if_needed()
                    rect = await element.bounding_box()
                    assert rect and 0 <= rect["y"]
                    assert rect["y"] + rect["height"] <= height + 1
                    assert rect["width"] >= 180
                    if mobile:
                        assert rect["height"] >= 44
                        if selector != "#b":
                            assert (
                                await element.evaluate(
                                    "el => parseFloat(getComputedStyle(el).fontSize)"
                                )
                                >= 16
                            )
                screenshot_dir = os.environ.get("PLUGBOARD_RUN_SCREENSHOT_DIR")
                if screenshot_dir and (width, height) == (390, 844):
                    directory = Path(screenshot_dir)
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                    # Write bytes through a private descriptor, including on reruns.
                    png = await page.screenshot(full_page=True)
                    target = directory / ("mfa.png" if mfa else "login.png")
                    fd = os.open(
                        target,
                        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                        0o600,
                    )
                    with os.fdopen(fd, "wb") as output:
                        os.fchmod(output.fileno(), 0o600)
                        output.write(png)
            await page.locator("#m").fill("123456")
            await page.locator("#b").click()
            await playwright.expect(page.locator("#e")).to_have_text(
                "Synthetic MFA code rejected"
            )
            assert submitted == [
                {"key": "synthetic-mobile-admin"},
                {"key": "synthetic-mobile-admin", "totp_code": "123456"},
            ]
        finally:
            await browser.close()
