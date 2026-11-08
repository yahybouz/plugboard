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
    assert not blocking, (
        f"{tab}: {len(blocking)} blocking a11y violation(s):\n{_format(blocking)}"
    )


async def test_suivi_has_no_blocking_a11y_violations(
    a11y_dashboard, axe_script, e2e_server
):
    page = a11y_dashboard.page
    # Cards in several states, so their tags and reasons are audited too.
    api = f"{e2e_server}/admin/suivi/tickets"
    for prio, typ in (("P0", "bug"), ("P1", "securite")):
        resp = await page.request.post(
            api,
            data={
                "titre": f"a11y {prio}",
                "priorite": prio,
                "type": typ,
                "description": "Description",
            },
        )
        assert resp.ok, await resp.text()
    blocked = (await resp.json())["id"]
    resp = await page.request.patch(
        f"{api}/{blocked}", data={"statut": "bloque", "motif": "Accès admin"}
    )
    assert resp.ok, await resp.text()
    await page.goto(f"{e2e_server}/suivi", wait_until="domcontentloaded")
    await page.wait_for_selector('#suivi-board[aria-busy="false"]', timeout=15_000)
    violations = await _audit(page, axe_script)
    blocking = [v for v in violations if v.get("impact") in _BLOCKING]
    assert not blocking, (
        f"/suivi: {len(blocking)} blocking a11y violation(s):\n{_format(blocking)}"
    )
