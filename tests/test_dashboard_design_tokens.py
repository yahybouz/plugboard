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

VAR_USE = re.compile(r"var\(\s*(--[\w-]+)")
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
COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")

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
    return [(p.strip().lower(), v.strip()) for p, v in DECLARATION.findall(css)]


def test_every_css_variable_used_is_defined():
    defined: set[str] = set()
    for name in (*CSS_FILES, "index.html"):  # index.html: the --c of the 3D legend
        defined |= set(VAR_DEF.findall(_read(name)))
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
    r"^(?:padding|margin)(?:-(?:top|right|bottom|left))?$|^(?:row-|column-)?gap$"
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
    if prop == "border-radius":
        return not all(RADIUS_PART.match(part) for part in value.split())
    return False


@pytest.mark.parametrize("name", CSS_FILES)
def test_stylesheet_uses_tokens(name):
    bad = [f"{p}: {v}" for p, v in _declarations(_checked_css(name)) if _violates(p, v)]
    assert bad == []


@pytest.mark.parametrize("name", CSS_FILES)
def test_stylesheet_has_no_raw_colour(name):
    assert [v for _, v in _declarations(_checked_css(name)) if COLOUR.search(v)] == []


def test_touch_rule_still_covers_controls():
    rule = re.search(
        r"@media \(pointer: coarse\)\s*\{(.*?)\n\}", _read("style.css"), re.S
    )
    assert rule, "the touch-screen rule is gone"
    for selector in (".btn", "input", "select", ".nav-link"):
        assert selector in rule.group(1)
