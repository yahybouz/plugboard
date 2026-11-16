"""BUG-125: on a phone the admin tables read as cards, not as overflowing grids.

Below 640 px every table row becomes a card: each cell shows its column title
(``data-label``, set from the table head by ``cardTable``), long ids wrap
without being cut at every hyphen, text stays at 14 px or more, and buttons
are 44 px targets. Above 640 px nothing changes. Static checks on
``static/``: no browser, no server (the browser case lives in
``tests/e2e/test_responsive.py``).
"""

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
TABLE_SCRIPTS = ("app.js", "auto_update.js", "portal.js")


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _phone_block() -> str:
    """The body of the ``@media (max-width: 640px)`` block holding the cards."""
    css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)
    for m in re.finditer(r"@media \(max-width: 640px\)\s*\{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        body = css[m.end() : i - 1]
        if ".table-cards" in body:
            return body
    pytest.fail("no @media (max-width: 640px) block styles .table-cards")


def _rule(block: str, selector: str) -> str:
    """Declarations of the first rule whose selector list contains ``selector``."""
    for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block):
        if selector in {s.strip() for s in sels.split(",")}:
            return body
    pytest.fail(f"no rule for {selector!r} in the 640 px block")


def _root_px() -> float:
    m = re.search(r"--fs-root:\s*([\d.]+)px", _read("style.css"))
    return float(m.group(1))


def _token_px(token: str) -> float:
    m = re.search(rf"{token}:\s*([\d.]+)rem", _read("style.css"))
    return float(m.group(1)) * _root_px()


def test_rows_become_cards_with_their_column_title():
    block = _phone_block()
    assert "display: block" in _rule(block, ".table-cards thead")
    assert "display: flex" in _rule(block, ".table-cards tbody tr")
    assert "content: attr(data-label)" in _rule(block, ".table-cards td::before")
    # The 680 px minimum width of the 768 px rule must not survive on a phone.
    assert "min-width: 0" in _rule(block, ".table-wrap table.table-cards")


def test_ids_and_labels_wrap_without_hyphen_cuts():
    td = _rule(_phone_block(), ".table-cards td")
    assert "overflow-wrap: anywhere" in td
    assert "hyphens: none" in td


@pytest.mark.parametrize(
    "selector", [".table-cards td code", ".table-cards td.cell-title strong"]
)
def test_ids_and_card_titles_are_not_cut_at_hyphens(selector):
    """A hyphen is a line-break opportunity: ``overflow-wrap`` alone still
    wraps ``client-de-demo`` after each hyphen. Ids (``code``) and the card
    title (the first cell's ``strong``, a client_id or a provider name, review
    B5) fill the line and break only where it ends."""
    rule = _rule(_phone_block(), selector)
    assert "word-break: break-all" in rule, selector
    assert "hyphens: none" in rule, selector


@pytest.mark.parametrize(
    "selector",
    [
        ".table-cards td",
        ".table-cards td::before",
        ".table-cards td code",
        ".table-cards td .muted",
        ".table-cards td .small",
    ],
)
def test_card_text_is_at_least_14px(selector):
    rule = _rule(_phone_block(), selector)
    size = re.search(r"font-size:\s*var\((--fs-[\w-]+)\)", rule).group(1)
    assert _token_px(size) >= 14, f"{selector}: {size}"


def test_buttons_are_44px_targets_on_a_phone():
    block = _phone_block()
    for selector in (".table-cards .btn", ".topbar-actions .btn"):
        assert re.search(r"min-height:\s*44px", _rule(block, selector)), selector


def test_card_helper_labels_every_cell_and_keeps_table_roles():
    js = _read("app.js")
    m = re.search(r"function cardTable\(\w+\) \{(.*?)\n\}", js, re.S)
    assert m, "cardTable(table) is missing from app.js"
    body = m.group(1)
    assert 'setAttribute("data-label"' in body
    assert 'classList.add("table-cards")' in body
    for role in ("table", "rowgroup", "row", "columnheader", "cell"):
        assert f'"role", "{role}"' in body, role
    helper = re.search(r"function table\(headers, rows\) \{(.*?)\n\}", js, re.S)
    assert "cardTable(" in helper.group(1)


@pytest.mark.parametrize("name", TABLE_SCRIPTS)
def test_every_table_goes_through_the_card_helper(name):
    js = _read(name)
    built = len(re.findall(r'h\("table"', js))
    carded = len(re.findall(r'cardTable\(\s*h\("table"', js))
    assert built == carded, f"{name}: {built - carded} table(s) without cardTable"


def test_portal_has_its_own_card_helper():
    js = _read("portal.js")
    assert re.search(r"function cardTable\(\w+\) \{", js)
    assert 'setAttribute("data-label"' in js


@pytest.mark.parametrize(
    "page, asset, first_carded_version",
    [
        # 16: card titles not cut at hyphens (review B5).
        ("index.html", "style.css", 16),
        ("index.html", "app.js", 29),
        ("portal.html", "style.css", 16),
        ("portal.html", "portal.js", 6),
    ],
)
def test_changed_assets_are_cache_busted(page, asset, first_carded_version):
    """A phone that cached the old files would keep the overflowing tables."""
    version = re.search(rf"/dashboard/{re.escape(asset)}\?v=(\d+)", _read(page))
    assert version, f"{page} loads {asset} without a version"
    assert int(version.group(1)) >= first_carded_version
