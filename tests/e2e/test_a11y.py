"""E2E — automated accessibility audit (axe-core) per tab. MANIFESTO §19 (WCAG 2.1 AA).

Injects axe-core into each dashboard tab and asserts there are no ``critical`` or
``serious`` WCAG 2.1 A/AA violations (the actionable severities). ``moderate`` /
``minor`` findings are reported but do not fail the gate.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import DASHBOARD_TABS

pytestmark = pytest.mark.asyncio(loop_scope="session")

_WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]
_BLOCKING = {"critical", "serious"}


async def _settle(page, tab: str) -> None:
    """Wait for the page-in fade animation (opacity 0→1) to finish.

    Without this, axe runs mid-animation and measures every in-content element at
    partial opacity — producing phantom contrast violations (e.g. muted text read
    as a washed-out grey). Accessibility must be judged on the settled UI.
    """
    await page.wait_for_function(
        "(tab) => { const el = document.querySelector(`#page-${tab}.active`);"
        " return !!el && getComputedStyle(el).opacity === '1'; }",
        arg=tab,
        timeout=5_000,
    )


async def _audit(page, axe_script: str) -> list:
    await page.add_script_tag(content=axe_script)
    results = await page.evaluate(
        "async (tags) => await axe.run(document, { runOnly: tags })",
        _WCAG_TAGS,
    )
    return results["violations"]


def _format(violations: list) -> str:
    lines = []
    for v in violations:
        for n in v.get("nodes", []):
            tgt = "/".join(n.get("target", []))
            data = (n.get("any") or [{}])[0].get("data") or {}
            ratio = data.get("contrastRatio")
            fg = data.get("fgColor")
            bg = data.get("bgColor")
            extra = f" ratio={ratio} fg={fg} bg={bg}" if ratio else ""
            lines.append(f"  [{v['impact']}] {v['id']} → {tgt}{extra}")
    return "\n".join(lines)


@pytest.mark.parametrize("tab", DASHBOARD_TABS)
async def test_tab_has_no_blocking_a11y_violations(a11y_dashboard, axe_script, tab):
    await a11y_dashboard.goto_tab(tab)
    await _settle(a11y_dashboard.page, tab)
    violations = await _audit(a11y_dashboard.page, axe_script)
    blocking = [v for v in violations if v.get("impact") in _BLOCKING]
    assert not blocking, f"{tab}: {len(blocking)} blocking a11y violation(s):\n{_format(blocking)}"
