"""Dashboard wiring of the "Public addresses only" provider checkbox (PLU-7)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_form_has_egress_guard_checkbox():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="pf-egress-guard"' in html and 'id="pf-egress-guard-hint"' in html
    assert "Public addresses only" in html


def test_app_js_sends_and_shows_the_guard():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert (
        'body.egress_guard = document.getElementById("pf-egress-guard").checked' in js
    )
    assert "p.egress_guard_forced === true" in js
    assert "Always on for catalog and built-in cloud presets." in js
    assert re.search(
        r'const cres = await api\("POST", "/providers", body\);\s*if \(!cres\.ok\)', js
    )
    assert "egress_refused:" in js


def test_dashboard_uses_the_exact_texts():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert (
        "Refuses private, loopback and link-local addresses at every connection. "
        "Ignores HTTPS_PROXY: the server needs direct internet access. "
        "Leave off for Ollama or another local server." in html
    )
    assert 'egress_refused: "Refused: non-public address"' in js
    assert '"public only")' in js
    assert (
        js.count(
            'body.egress_guard = document.getElementById("pf-egress-guard").checked'
        )
        == 1
    )


def test_app_js_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    subprocess.run([node, "--check", str(ROOT / "static" / "app.js")], check=True)
