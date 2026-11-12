"""Dashboard wiring of the Ollama switch (PLU-12)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _html() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    return (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def test_ollama_page_has_the_switch():
    html = _html()
    page = html[html.index('id="page-ollama"') :]
    page = page[: page.index("</section>")]
    assert 'id="ollama-enabled"' in page and 'data-testid="ollama-enabled"' in page
    assert 'id="ollama-enabled-hint"' in page
    assert "Available to the API, MCP and Playground" in page
    # The switch comes first on the page, before the status cards.
    assert page.index('id="ollama-enabled"') < page.index('id="ollama-status"')


def test_ollama_page_has_the_off_banner():
    html = _html()
    # Hidden by style: the hidden attribute loses to .alert's display rule.
    tag = re.search(r'<div [^>]*id="ollama-off-banner"[^>]*>', html).group(0)
    assert 'class="alert alert-warning"' in tag
    assert re.search(r'style="[^"]*display:none;', tag)
    assert (
        "Ollama is off: the API, the MCP server and the Playground do not use it."
        in html
    )


def test_app_js_reads_and_sets_the_switch():
    js = _js()
    assert 'api("GET", "/admin/ollama/switch")' in js
    assert 'api("PUT", "/admin/ollama/switch", { enabled: enabled })' in js
    assert "await loadOllamaSwitch();" in js
    assert 'getElementById("ollama-enabled").addEventListener("change"' in js
    # Greyed out without a registered provider, and while a PUT runs: a
    # Refresh during that PUT must not unlock it.
    assert "box.disabled = !d.registered || _ollamaSwitchBusy;" in js
    assert (
        'getElementById("ollama-off-banner").style.display = '
        'd.registered && !d.enabled ? "" : "none";'
    ) in js


def test_app_js_is_valid():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    subprocess.run([node, "--check", str(ROOT / "static" / "app.js")], check=True)


def _save_switch_body(js: str) -> str:
    start = js.index("async function saveOllamaSwitch(enabled)")
    return js[start : js.index("\n}\n", start)]


def test_switch_is_locked_while_its_put_runs():
    """Turning Ollama on waits for the model sync: no second PUT meanwhile."""
    body = _save_switch_body(_js())
    assert body.index("_ollamaSwitchBusy = true;") < body.index('api("PUT"')
    assert body.index("box.disabled = true;") < body.index('api("PUT"')
    assert "finally {\n    _ollamaSwitchBusy = false;" in body
    # The toast says what the gateway answered, not what was asked.
    assert "d.enabled ? " in body
    # A failed PUT puts the box back before the state is read again.
    assert body.index("box.checked = !enabled;") < body.index(
        "await loadOllamaSwitch();"
    )


def test_switch_markup_is_accessible_and_starts_locked():
    html = _html()
    tag = re.search(r'<input type="checkbox" id="ollama-enabled"[^>]*>', html).group(0)
    assert "disabled" in tag, "locked until GET /admin/ollama/switch answers"
    assert 'aria-describedby="ollama-enabled-hint"' in tag
    banner = re.search(r'<div [^>]*id="ollama-off-banner"[^>]*>', html).group(0)
    assert 'role="status"' in banner
