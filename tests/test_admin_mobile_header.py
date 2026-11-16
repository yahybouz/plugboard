"""BUG-126: on a phone the in-flight pill and the page bar leave the content alone.

At 390 px the floating "0 in flight" pill (``live_requests.js``) sat over the
cards in the bottom-right corner, and the page bar of Providers (title and two
buttons on two lines, 155 px) stayed stuck under the phone bar while scrolling.
Below 640 px the pill now sits in the phone bar (sticky, empty on the right),
as a 44 px target, and the page bars scroll with the page. Above 640 px
nothing changes. Static checks on ``static/style.css``: no browser, no server
(the browser case lives in ``tests/e2e/test_responsive.py``).
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"


def _css() -> str:
    return re.sub(
        r"/\*.*?\*/", "", (STATIC / "style.css").read_text("utf-8"), flags=re.S
    )


def _media_blocks(css: str, query: str) -> list[str]:
    """Bodies of every ``@media <query>`` block."""
    bodies = []
    for m in re.finditer(rf"@media {re.escape(query)}\s*\{{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        bodies.append(css[m.end() : i - 1])
    return bodies


def _top_level(css: str) -> str:
    """The stylesheet without its @media blocks."""
    out, i = [], 0
    for m in re.finditer(r"@media [^{]*\{", css):
        if m.start() < i:
            continue
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        out.append(css[i : m.start()])
        i = j
    out.append(css[i:])
    return "".join(out)


def _decls(block: str, selector: str) -> dict[str, str]:
    """Merged declarations of every rule whose selector list holds ``selector``."""
    found: dict[str, str] = {}
    for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
        if selector in {s.strip() for s in sels.split(",")}:
            for prop, value in re.findall(r"([\w-]+)\s*:\s*([^;]+)", body):
                found[prop.strip()] = value.strip()
    return found


def _phone(selector: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for block in _media_blocks(_css(), "(max-width: 640px)"):
        found.update(_decls(block, selector))
    return found


def _z(selector: str, block: str) -> int:
    return int(_decls(block, selector)["z-index"])


def test_page_bar_scrolls_with_the_page_on_a_phone():
    """A two-line page bar no longer stays stuck under the phone bar."""
    assert _phone(".topbar").get("position") == "static"


def test_page_bar_stays_sticky_on_a_wide_screen():
    assert _decls(_top_level(_css()), ".topbar").get("position") == "sticky"


def test_pill_sits_in_the_phone_bar_not_over_the_cards():
    pill = _phone("#live-pill")
    # Out of the bottom-right corner, where the cards are...
    assert pill.get("bottom") == "auto", pill
    # ...and inside the phone bar: same top padding as its menu button.
    assert pill.get("top") == "var(--space-2)", pill
    assert re.fullmatch(r"44px", pill.get("min-height", "")), pill
    mobile_bar = _decls(_media_blocks(_css(), "(max-width: 768px)")[0], ".mobile-bar")
    assert mobile_bar.get("padding", "").startswith("var(--space-2)"), mobile_bar


def test_pill_panel_opens_below_the_phone_bar():
    panel = _phone("#live-panel")
    assert panel.get("bottom") == "auto", panel
    assert "44px" in panel.get("top", "") and "var(--space-2)" in panel.get("top", "")


def test_pill_stacks_above_the_phone_bar_and_under_menus_and_dialogs():
    """Above the sticky phone bar (z 40) so it shows; under the open sidebar
    (z 50), toasts (z 300) and dialogs (z 200+), which it no longer covers."""
    css = _css()
    top = _top_level(css)
    bar = _z(".mobile-bar", _media_blocks(css, "(max-width: 768px)")[0])
    sidebar = _z(".sidebar", top)
    for selector in ("#live-pill", "#live-panel"):
        z = int(_phone(selector)["z-index"])
        assert bar < z < sidebar, (selector, z)


@pytest.mark.parametrize("selector", ["#live-pill", "#live-panel"])
def test_pill_keeps_its_corner_on_a_wide_screen(selector):
    rule = _decls(_top_level(_css()), selector)
    assert rule["position"] == "fixed"
    assert rule["bottom"] != "auto"
    assert rule["z-index"] == "900"


def test_stylesheet_is_cache_busted():
    """A phone that cached the old stylesheet would keep the pill on the cards."""
    for page in ("index.html", "portal.html"):
        html = (STATIC / page).read_text("utf-8")
        version = re.search(r"/dashboard/style\.css\?v=(\d+)", html)
        assert version and int(version.group(1)) >= 18, page
