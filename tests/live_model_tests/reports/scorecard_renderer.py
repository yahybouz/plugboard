"""Scorecard HTML renderer for live-test runs.

Pure ``render_scorecard(run_summary) -> str`` (standalone HTML, inline CSS, no
external dependencies). Council C10 (developer + codex-coder).
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Optional

_SEVERITY_COLOR = {
    "BLOCKER": "#c0392b",
    "MAJOR": "#e67e22",
    "MINOR": "#f1c40f",
    "INFO": "#3498db",
}
_VERDICT_COLOR = {"GO": "#27ae60", "NO-GO": "#c0392b"}


def _esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else ""


def _rate_str(rate: Optional[float]) -> str:
    return "N/A" if rate is None else f"{rate:.1%}"


def _badge(passed: Optional[bool], error_type: Optional[str]) -> str:
    if passed is True:
        return '<span class="pass">✓</span>'
    if error_type and error_type.startswith("skipped:"):
        return f'<span class="skip" title="{_esc(error_type)}">—</span>'
    if passed is False:
        return f'<span class="fail" title="{_esc(error_type)}">✗</span>'
    return '<span class="skip">?</span>'


def _dim_table(dim_aggregates: Dict[str, Dict[str, Any]]) -> str:
    rows = []
    for key, agg in sorted(dim_aggregates.items()):
        status = agg.get("status", "?")
        threshold = agg.get("threshold")
        thr = f"≥ {threshold:.0%}" if threshold is not None else "?"
        sev = agg.get("dimension_severity") or ""
        cls = (
            "pass" if status == "pass" else ("skip" if status == "skipped" else "fail")
        )
        label = (
            "NO-GO" if status == "fail" else ("SKIP" if status == "skipped" else "GO")
        )
        rows.append(
            f"<tr><td>{_esc(key)}</td><td>{_rate_str(agg.get('pass_rate'))}</td>"
            f"<td>{thr}</td><td style='color:{_SEVERITY_COLOR.get(sev, '#888')}'>{_esc(sev)}</td>"
            f"<td class='{cls}'>{label}</td></tr>"
        )
    head = (
        "<thead><tr><th>Dimension</th><th>Pass rate</th><th>Threshold</th>"
        "<th>Severity</th><th>Verdict</th></tr></thead>"
    )
    return f"<table>{head}<tbody>{''.join(rows)}</tbody></table>"


def _model_table(
    raw_records: List[Dict[str, Any]], dimensions: List[str], models: List[str]
) -> str:
    if not raw_records:
        return "<p>No per-model data.</p>"
    index: Dict[tuple, Dict[str, Any]] = {
        (r.get("model_id"), r.get("_dim"), r.get("_surface")): r for r in raw_records
    }
    surfaces = sorted({r.get("_surface") for r in raw_records if r.get("_surface")})
    combos = [(d, s) for d in dimensions for s in surfaces]
    if not combos:
        return "<p>No per-model data.</p>"
    head_cells = "".join(
        f"<th>{_esc(d)}<br><small>{_esc(s)}</small></th>" for d, s in combos
    )
    rows = []
    for model_id in models:
        cells = []
        for d, s in combos:
            rec = index.get((model_id, d, s))
            cells.append(
                "<td>—</td>"
                if rec is None
                else f"<td>{_badge(rec.get('passed'), rec.get('error_type'))}</td>"
            )
        rows.append(
            f"<tr><td class='model-name'>{_esc(model_id)}</td>{''.join(cells)}</tr>"
        )
    return (
        f"<table class='model-table'><thead><tr><th>Model</th>{head_cells}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _disagreements(disagreements: List[Dict[str, Any]]) -> str:
    if not disagreements:
        return "<p>No cross-surface disagreements.</p>"
    rows = []
    for d in disagreements:
        verdicts = ", ".join(
            f"{s}: {'PASS' if v else 'FAIL'}"
            for s, v in d.get("surface_verdicts", {}).items()
        )
        sev = d.get("severity", "MAJOR")
        rows.append(
            f"<tr><td>{_esc(d.get('model_id'))}</td><td>{_esc(d.get('dim'))}</td>"
            f"<td>{_esc(verdicts)}</td>"
            f"<td style='color:{_SEVERITY_COLOR.get(sev, '#888')}'>{_esc(sev)}</td></tr>"
        )
    head = "<thead><tr><th>Model</th><th>Dimension</th><th>Verdicts</th><th>Severity</th></tr></thead>"
    return f"<table>{head}<tbody>{''.join(rows)}</tbody></table>"


_CSS = """
body{font-family:system-ui,sans-serif;margin:2rem;color:#222}
h1{font-size:1.4rem;margin-bottom:.3rem}h2{font-size:1.1rem;border-bottom:1px solid #ddd;padding-bottom:.3rem}
.meta{color:#555;font-size:.9rem;margin-bottom:1.5rem}.verdict{font-size:1.8rem;font-weight:bold;margin:1rem 0}
table{border-collapse:collapse;width:100%;margin-bottom:1.5rem}
th,td{border:1px solid #ddd;padding:.5rem .75rem;text-align:left}th{background:#f5f5f5}
tr:nth-child(even){background:#fafafa}.pass{color:#27ae60;font-weight:bold}.fail{color:#c0392b;font-weight:bold}
.skip{color:#888}.model-name{font-family:monospace;font-size:.85rem}
.model-table td{text-align:center}.model-table td.model-name{text-align:left}section{margin-bottom:2rem}
"""


def render_scorecard(run_summary: Dict[str, Any]) -> str:
    """Produce a standalone HTML scorecard from a run summary dict."""
    run_id = _esc(run_summary.get("run_id", "unknown"))
    verdict = run_summary.get("verdict", "UNKNOWN")
    max_sev = run_summary.get("max_severity") or ""
    sev_html = (
        (
            f'<div style="color:{_SEVERITY_COLOR.get(max_sev, "#888")}">'
            f"Max severity: <strong>{_esc(max_sev)}</strong></div>"
        )
        if max_sev
        else ""
    )
    skipped = run_summary.get("skipped_dimensions") or []
    skipped_html = (
        f'<div class="skip">Skipped dimensions: {_esc(", ".join(skipped))}</div>'
        if skipped
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Plugboard Live Test Scorecard — {run_id}</title><style>{_CSS}</style></head>
<body>
<h1>Plugboard Live Test Scorecard</h1>
<div class="meta">Run ID: <code>{run_id}</code> &nbsp;|&nbsp; {_esc(run_summary.get("timestamp", ""))}</div>
<div class="verdict" style="color:{_VERDICT_COLOR.get(verdict, "#888")}">{_esc(verdict)}</div>
{sev_html}{skipped_html}
<section><h2>Dimension summary</h2>{_dim_table(run_summary.get("dim_aggregates", {}))}</section>
<section><h2>Per-model results</h2>{_model_table(run_summary.get("raw_records", []), run_summary.get("dimensions", []), run_summary.get("models", []))}</section>
<section><h2>Cross-surface disagreements</h2>{_disagreements(run_summary.get("cross_surface_disagreements", []))}</section>
</body></html>"""
