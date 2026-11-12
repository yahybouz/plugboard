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
    assert re.search(
        r'<div class="alert alert-warning" id="ollama-off-banner" '
        r'style="[^"]*display:none;[^"]*"',
        html,
    )
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
    # Without a registered provider the switch is greyed out.
    assert "box.disabled = !d.registered;" in js
    assert (
        'getElementById("ollama-off-banner").style.display = '
        'd.registered && !d.enabled ? "" : "none";'
    ) in js


def test_app_js_is_valid():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    subprocess.run([node, "--check", str(ROOT / "static" / "app.js")], check=True)
