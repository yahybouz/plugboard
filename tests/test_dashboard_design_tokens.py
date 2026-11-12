"""PB-DASH-02: the dashboard uses the design tokens of DESIGN.md, and only them.

Static checks on ``static/``: no browser, no server. Each rule names the file
and the offending declaration or label, so a failure says what to fix.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
CSS_FILES = ("style.css", "suivi.css")

VAR_USE = re.compile(
    r"(?:var\(\s*|getPropertyValue\(\s*[\"']|_cssVar\(\s*[\"'])(--[\w-]+)"
)
VAR_DEF = re.compile(r"(?<![\w-])(--[\w-]+)\s*:")
TOKEN_BLOCK = re.compile(
    r"/\* design-tokens: start \*/(.*?)/\* design-tokens: end \*/", re.S
)
MARKED_REGION = re.compile(
    r"/\* design-tokens: (?:start|exception start)[^*]*\*/"
    r".*?/\* design-tokens: (?:end|exception end) \*/",
    re.S,
)
COMMENT = re.compile(r"/\*.*?\*/", re.S)
DECLARATION = re.compile(r"([\w-]+)\s*:\s*([^;{}]+)")
COLOUR = re.compile(
    r"(?<![&\w-])#[0-9a-fA-F]{3,8}(?![\w-])|\b(?:rgba?|hsla?|hwb|oklch|oklab)\("
)

NEW_TOKENS = {
    "--fs-root",
    "--fs-label",
    "--fs-xs",
    "--fs-sm",
    "--fs-md",
    "--fs-lg",
    "--fs-xl",
    "--fs-2xl",
    "--fs-icon",
    "--fw-regular",
    "--fw-medium",
    "--fw-semibold",
    "--fw-bold",
    "--space-1",
    "--space-2",
    "--space-3",
    "--space-4",
    "--space-5",
    "--space-6",
    "--space-8",
    "--space-10",
    "--space-12",
    "--pad-control",
    "--pad-control-sm",
    "--pad-input",
    "--pad-badge",
    "--pad-chip",
    "--pad-cell",
    "--radius-xs",
    "--radius-control",
    "--radius-pill",
    "--overlay",
    "--logo-bg",
    "--inset-highlight",
}


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _root_tokens() -> set[str]:
    """Custom properties defined between the design-tokens markers of style.css."""
    tokens: set[str] = set()
    for block in TOKEN_BLOCK.findall(_read("style.css")):
        tokens |= set(VAR_DEF.findall(block))
    return tokens


def _checked_css(name: str) -> str:
    """The stylesheet without its token blocks, marked exceptions and comments."""
    return COMMENT.sub("", MARKED_REGION.sub("", _read(name)))


def _declarations(css: str) -> list[tuple[str, str]]:
    """``(property, value)`` pairs, read inside rule bodies only (not selectors)."""
    pairs = []
    for body in re.findall(r"\{([^{}]*)\}", css):
        pairs += [(p.strip().lower(), v.strip()) for p, v in DECLARATION.findall(body)]
    return pairs


def test_every_css_variable_used_is_defined():
    defined: set[str] = set()
    for name in (*CSS_FILES, "index.html"):  # index.html: the --c of the 3D legend
        defined |= set(VAR_DEF.findall(COMMENT.sub("", _read(name))))
    missing: dict[str, list[str]] = {}
    for path in sorted(STATIC.glob("*.*")):
        if path.suffix not in {".css", ".js", ".html"}:
            continue
        for var in VAR_USE.findall(path.read_text(encoding="utf-8")):
            if var not in defined:
                missing.setdefault(var, []).append(path.name)
    assert missing == {}


def test_design_md_documents_every_token():
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    assert sorted(t for t in _root_tokens() if f"`{t}`" not in design) == []
    assert sorted(NEW_TOKENS - _root_tokens()) == []


SPACING_PROPS = re.compile(
    r"^(?:padding|margin)(?:-(?:top|right|bottom|left|inline|block)(?:-(?:start|end))?)?$"
    r"|^(?:grid-)?(?:row-|column-)?gap$"
)
SPACING_PART = re.compile(
    r"^(?:0|auto|var\(--(?:space|pad)-[\w-]+\)|var\(--sidebar-w\))$"
)
RADIUS_PART = re.compile(r"^(?:0|2px|3px|50%|inherit|var\(--radius(?:-[\w-]+)?\))$")


def _violates(prop: str, value: str) -> bool:
    """True when a declaration uses a raw value where a token is required."""
    value = value.replace("!important", "").strip()
    if prop == "font-size":
        return not re.fullmatch(r"var\(--fs-[\w-]+\)|inherit", value)
    if prop == "font-weight":
        return not re.fullmatch(r"var\(--fw-[\w-]+\)|inherit", value)
    if SPACING_PROPS.match(prop):
        return not all(SPACING_PART.match(part) for part in value.split())
    if re.fullmatch(
        r"border(?:-(?:top|bottom|start|end)-(?:left|right|start|end))?-radius", prop
    ):
        return not all(RADIUS_PART.match(part) for part in value.split())
    if prop == "font":
        return bool(re.search(r"\d(?:px|rem|em|%)", value))
    return False


@pytest.mark.parametrize("name", CSS_FILES)
def test_stylesheet_uses_tokens(name):
    bad = [f"{p}: {v}" for p, v in _declarations(_checked_css(name)) if _violates(p, v)]
    assert bad == []


@pytest.mark.parametrize("name", CSS_FILES)
def test_stylesheet_has_no_raw_colour(name):
    assert [v for _, v in _declarations(_checked_css(name)) if COLOUR.search(v)] == []


def test_touch_rule_still_covers_controls():
    block = re.search(
        r"@media \(pointer: coarse\)\s*\{(.*?)\n\}", _read("style.css"), re.S
    )
    assert block, "the touch-screen rule is gone"
    rule = re.search(r"([^{}]+)\{\s*min-height:\s*44px;?\s*\}", block.group(1))
    assert rule, "no 44px rule for touch screens"
    selectors = {s.strip() for s in rule.group(1).split(",")}
    assert {".btn", ".btn-sm", "input", "select", ".nav-link"} <= selectors


def test_token_markers_cannot_hide_rules():
    css = _read("style.css")
    blocks = TOKEN_BLOCK.findall(css)
    assert len(blocks) == 1
    # Only theme blocks with custom properties (and color-scheme) inside.
    for prop, _ in _declarations(COMMENT.sub("", blocks[0])):
        assert prop.startswith("--") or prop == "color-scheme", prop
    exceptions = re.findall(
        r"/\* design-tokens: exception start[^*]*\*/(.*?)/\* design-tokens: exception end \*/",
        css,
        re.S,
    )
    assert len(exceptions) == 1
    selectors = re.findall(r"([^{}]+)\{", COMMENT.sub("", exceptions[0]))
    for selector in selectors:
        selector = selector.strip()
        if selector.startswith("@") or re.fullmatch(r"\d+%|from|to", selector):
            continue  # at-rules and keyframe steps
        assert "live3d" in selector, selector
    assert "design-tokens" not in _read("suivi.css")


INLINE_STYLE = re.compile(
    r'style=\\?"([^"\\]*)|style=\'([^\']*)\'|style:\s*"([^"]*)"|style:\s*\'([^\']*)\''
)


def _inline_declarations(text: str) -> list[str]:
    """Each declaration of every style attribute or ``style:`` property."""
    found = []
    for groups in INLINE_STYLE.findall(text):
        for decl in "".join(groups).split(";"):
            if decl.strip():
                found.append(re.sub(r"\s*:\s*", ":", decl.strip(), count=1))
    return found


def test_index_inline_styles_only_where_js_needs_them():
    html = _read("index.html")
    allowed = re.compile(r"^(?:display:none|--c:#[0-9a-fA-F]{6}|position:absolute)$")
    assert [d for d in _inline_declarations(html) if not allowed.match(d)] == []
    assert _inline_declarations(_read("suivi.html")) == []


@pytest.mark.parametrize(
    "name", ["app.js", "auto_update.js", "live_requests.js", "portal.js", "suivi.js"]
)
def test_scripts_inline_styles_only_where_js_needs_them(name):
    js = _read(name)
    assert "cssText" not in js
    allowed = re.compile(r"^(?:display|width):")
    assert [d for d in _inline_declarations(js) if not allowed.match(d)] == []


def test_playground_keeps_its_js_hooks():
    js, html = _read("app.js"), _read("index.html")
    # The welcome line is removed through its ``muted`` class; no bubble has it.
    assert 'container.querySelector(".muted")' in js
    assert re.search(r'class="muted[^"]*pg-empty|class="pg-empty[^"]*muted', html)
    # The waiting bubble shows a bare "..." that the browser tests wait on.
    assert '_pgAddMessage("assistant", "...")' in js
    assert 'bubble.dataset.testid = "message-bubble"' in js


JS_COLOUR_FREE = [
    "app.js",
    "auto_update.js",
    "live_requests.js",
    "portal.js",
    "suivi.js",
]


@pytest.mark.parametrize("name", JS_COLOUR_FREE)
def test_scripts_have_no_raw_colour(name):
    assert COLOUR.findall(_read(name)) == []


def test_live_requests_styles_live_in_the_stylesheet():
    assert 'el("style"' not in _read("live_requests.js")
    css = _read("style.css")
    assert "#live-pill" in css and "#live-panel" in css


def test_portal_classes_exist_in_stylesheet():
    css = _read("style.css")
    classes = {
        c
        for a in re.findall(r'class="([^"]+)"', _read("portal.html"))
        for c in a.split()
    }
    missing = [
        c for c in classes if not re.search(r"\." + re.escape(c) + r"(?![\w-])", css)
    ]
    assert sorted(missing) == []


def test_portal_follows_the_theme_and_has_no_inline_style():
    html = _read("portal.html")
    assert "data-theme=" not in html and 'style="' not in html
    assert html.index("/dashboard/theme.js") < html.index("/dashboard/style.css")


LABEL_FILES = [
    "index.html",
    "app.js",
    "portal.html",
    "portal.js",
    "auto_update.js",
    "live_requests.js",
    "suivi.html",
    "suivi.js",
]
# Labels the wording rule replaced: US English, sentence case, "Log out",
# no space before a colon.
FORBIDDEN_LABELS = (
    "API Keys",
    "Usage &amp; Billing",
    "Usage & Billing",
    "Create API Key",
    "Create Key",
    "RPM Limit",
    "Admin Key",
    "Metrics by Provider",
    "By Client",
    "By Model",
    "Add Direct Provider",
    "Add Provider",
    "Edit Provider",
    "Bearer Token",
    "Default Model",
    "GitHub Device Flow",
    "Job Log",
    "Avg Latency",
    "Prompt Tokens",
    "Completion Tokens",
    "Total Cost",
    "Error Rate",
    "Logout",
    "Endpoint :",
    "Default model :",
    "Client Portal",
    "My Usage",
    "Spending Limit",
    "Usage by Model",
    "Daily Usage",
    "Daily Breakdown",
    "Total Tokens",
    "Admin Dashboard",
    "New Key",
    "Monthly Limit",
    "Add Custom Provider",
    "Check Internet",
    "Check All",
    "Sovereign Provider",
    "Pull Model",
    "Installed Models",
    "Cloud Catalogue",
    "catalogue is empty",
    "cloud catalogue:",
)
FRENCH = re.compile(r"[àâçéèêëîïôûùüÿœÀÂÇÉÈÊËÎÏÔÛÙÜŸŒ]")
# "Loading..." and `+ "..."` become "…"; a bare "..." placeholder stays.
ELLIPSIS = re.compile(r'\w\.\.\.|\+\s*"\.\.\."')


@pytest.mark.parametrize("name", LABEL_FILES)
def test_labels_follow_the_copy_rules(name):
    text = _read(name)
    found = [
        label
        for label in FORBIDDEN_LABELS
        if re.search(r"(?<![\w-])" + re.escape(label) + r"(?![\w-])", text)
    ]
    assert found == []
    assert ELLIPSIS.findall(text) == []
    assert FRENCH.findall(text) == []


def test_suivi_is_in_english():
    assert '<html lang="en">' in _read("suivi.html")
    words = re.compile(
        r"\b(?:Suivi|Importer|Nouveau|Titre|Créer|Aucun|Statut|Motif|Preuves?"
        r"|Dette|Fonction|Chemin|Lien|Commande)\b"
    )
    for name in ("suivi.html", "suivi.js", "index.html"):
        assert words.findall(_read(name)) == [], name
