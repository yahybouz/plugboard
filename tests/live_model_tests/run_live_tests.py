"""Live test orchestrator — wires the 6 dimensions across surfaces and models.

Council C10 (2026-05-31), duo:
  - PROPOSER : developer
  - CRITIC   : codex-coder (2 BLOCKER + 4 MAJOR integrated)

Usage:
    python -m tests.live_model_tests.run_live_tests \
        --surface api --dimensions d1,d3 --models claude-haiku-4-5 \
        --base-url http://localhost:8765 --out data/live_test_runs/

Exit code: 0 = GO, 1 = NO-GO (usable as a pre-deploy gate).

Prefer the ``PLUGBOARD_API_KEY`` env var over ``--api-key`` (Council C10
MINOR-2: a CLI key is visible in ``ps``/shell history).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import yaml

from tests.live_model_tests.dimensions.d1_liveness import run_d1_liveness
from tests.live_model_tests.dimensions.d2_integrity import run_d2_integrity
from tests.live_model_tests.dimensions.d3_internet import run_d3_internet
from tests.live_model_tests.dimensions.d4_identity_cooperative import run_d4_identity
from tests.live_model_tests.dimensions.d5_streaming import run_d5_streaming_integrity
from tests.live_model_tests.dimensions.d6_cost_accuracy import (
    _default_pricing_resolver,
    run_d6_cost_accuracy,
)
from tests.live_model_tests.surfaces.api_client import ApiSurfaceClient
from tests.live_model_tests.surfaces.mcp_client import McpSurfaceClient
from tests.live_model_tests.surfaces.ui_client import UiSurfaceClient

_FRAMEWORK_DIR = Path(__file__).parent
_CATALOG_PATH = _FRAMEWORK_DIR / "catalog.yaml"
_EVAL_CONFIG_PATH = _FRAMEWORK_DIR / "eval_config.yaml"
_QUARANTINE_PATH = Path("data/quarantined_models.json")
_DEFAULT_OUT_DIR = Path("data/live_test_runs")

SEVERITY_ORDER: Dict[str, int] = {"BLOCKER": 4, "MAJOR": 3, "MINOR": 2, "INFO": 1}
_DEFAULT_SEVERITY = "MAJOR"

ALL_DIMENSIONS = ["d1", "d2", "d3", "d4", "d5", "d6"]
ALL_SURFACES = ["api", "mcp", "ui"]

_DIM_CONFIG_KEY_MAP = {
    "d1": "d1_liveness",
    "d2": "d2_integrity",
    "d3": "d3_internet",
    "d4": "d4_identity_cooperative",
    "d5": "d5_streaming_integrity",
    "d6": "d6_cost_accuracy",
}
_DIM_CONFIG_TO_SHORT = {v: k for k, v in _DIM_CONFIG_KEY_MAP.items()}

SurfaceFactory = Callable[[str, str, str], Any]  # (surface, base_url, api_key) → client


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def default_surface_factory(surface_name: str, base_url: str, api_key: str) -> Any:
    """Production factory. api (C7) + mcp (C8) + ui (C9) implemented."""
    if surface_name == "api":
        return ApiSurfaceClient(base_url=base_url, api_key=api_key)
    if surface_name == "mcp":
        # The MCP server reads PLUGBOARD_URL / PLUGBOARD_API_KEY from its env.
        return McpSurfaceClient(env={"PLUGBOARD_URL": base_url, "PLUGBOARD_API_KEY": api_key})
    if surface_name == "ui":
        # Playwright drives the Playground; auto-login on localhost. supports_billing
        # is False (D6 skip) and raw_sse_lines is None (D5 skip) — see ui_client.
        return UiSurfaceClient(base_url=base_url, api_key=api_key)
    raise NotImplementedError(f"Surface '{surface_name}' not recognised: {surface_name!r}")


# ── Dimension dispatch (resolves heterogeneous run_dX signatures) ────────────

def _make_dispatch(aliases, pricing_resolver, tolerance_pct) -> Dict[str, Callable[..., Any]]:
    def _d3(**kw):
        return run_d3_internet(now=None, **kw)

    def _d4(**kw):
        return run_d4_identity(aliases=aliases, **kw)

    def _d6(**kw):
        return run_d6_cost_accuracy(pricing_resolver=pricing_resolver,
                                    tolerance_pct=tolerance_pct, **kw)

    return {
        "d1": lambda **kw: run_d1_liveness(**kw),
        "d2": lambda **kw: run_d2_integrity(**kw),
        "d3": _d3,
        "d4": _d4,
        "d5": lambda **kw: run_d5_streaming_integrity(**kw),
        "d6": _d6,
    }


def _dim_config_key(dim_key: str) -> str:
    return _DIM_CONFIG_KEY_MAP.get(dim_key, dim_key)


# ── Pure aggregation (no I/O — fully testable) ───────────────────────────────

def _severity_rank(severity: Optional[str]) -> int:
    # None / unknown ranks 0 so it never beats a real severity in a max scan.
    # The MAJOR default for fieldless results is applied at the read site.
    return SEVERITY_ORDER.get(severity, 0)


def _is_excluded(result: Any) -> bool:
    """A result is out of the threshold denominator when its error_type is a
    ``skipped:*`` OR it carries ``excluded_from_threshold=True``. D1/D2 lack the
    field — their skips are caught by the ``skipped:`` prefix."""
    et = getattr(result, "error_type", None) or ""
    if et.startswith("skipped:"):
        return True
    return bool(getattr(result, "excluded_from_threshold", False))


def aggregate(results: List[Any], eval_config: Dict[str, Any], dim_key: str) -> Dict[str, Any]:
    dim_config = eval_config.get("dimensions", {}).get(_dim_config_key(dim_key), {})
    threshold = float(dim_config.get("pass_threshold", 1.0))

    denominator = [r for r in results if not _is_excluded(r)]
    excluded = [r for r in results if _is_excluded(r)]

    if not denominator:
        return {
            "pass_rate": None, "dimension_pass": True, "dimension_severity": None,
            "total": len(results), "denominator": 0, "passed": 0,
            "skipped": len(excluded), "failed_results": [], "status": "skipped",
            "threshold": threshold,
        }

    passed_count = sum(1 for r in denominator if r.passed)
    pass_rate = passed_count / len(denominator)
    failed = [r for r in denominator if not r.passed]

    max_severity: Optional[str] = None
    for r in failed:
        sev = getattr(r, "finding_severity", _DEFAULT_SEVERITY) or _DEFAULT_SEVERITY
        if _severity_rank(sev) > _severity_rank(max_severity):
            max_severity = sev

    dimension_pass = pass_rate >= threshold
    return {
        "pass_rate": pass_rate, "dimension_pass": dimension_pass,
        "dimension_severity": max_severity if not dimension_pass else None,
        "total": len(results), "denominator": len(denominator), "passed": passed_count,
        "skipped": len(excluded), "failed_results": failed,
        "status": "pass" if dimension_pass else "fail", "threshold": threshold,
    }


def check_cross_surface_agreement(
    results_by_surface_dim_model: Dict[str, Dict[str, Dict[str, Any]]],
    excluded_dimensions: Sequence[str],
    severity_on_disagreement: str = "MAJOR",
) -> List[Dict[str, Any]]:
    disagreements: List[Dict[str, Any]] = []
    combos: Set[Tuple[str, str]] = set()
    for surface_data in results_by_surface_dim_model.values():
        for dim, model_map in surface_data.items():
            if dim in excluded_dimensions:
                continue
            for model_id in model_map:
                combos.add((dim, model_id))

    for dim, model_id in sorted(combos):
        surface_verdicts: Dict[str, bool] = {}
        for surface, surface_data in results_by_surface_dim_model.items():
            result = surface_data.get(dim, {}).get(model_id)
            if result is None or _is_excluded(result):
                continue
            surface_verdicts[surface] = bool(result.passed)
        if len(surface_verdicts) < 2:
            continue
        verdicts = list(surface_verdicts.values())
        if not all(v == verdicts[0] for v in verdicts):
            disagreements.append({
                "model_id": model_id, "dim": dim,
                "surface_verdicts": surface_verdicts, "severity": severity_on_disagreement,
            })
    return disagreements


def compute_verdict(
    dim_aggregates: Dict[str, Dict[str, Any]],
    cross_surface_disagreements: List[Dict[str, Any]],
) -> Dict[str, Any]:
    failing_dims = [dim for dim, agg in dim_aggregates.items() if agg["status"] == "fail"]
    # INTENTIONAL (Council C10 MAJOR-2): a fully-skipped dimension does NOT block
    # the GO (every model may be legitimately quarantined), but it is surfaced.
    skipped_dims = [dim for dim, agg in dim_aggregates.items() if agg["status"] == "skipped"]

    max_severity: Optional[str] = None
    for dim in failing_dims:
        sev = dim_aggregates[dim].get("dimension_severity")
        if _severity_rank(sev) > _severity_rank(max_severity):
            max_severity = sev
    for d in cross_surface_disagreements:
        sev = d.get("severity")
        if _severity_rank(sev) > _severity_rank(max_severity):
            max_severity = sev

    is_go = (not failing_dims) and (not cross_surface_disagreements)
    return {
        "verdict": "GO" if is_go else "NO-GO",
        "failing_dims": failing_dims,
        "skipped_dimensions": skipped_dims,
        "cross_surface_disagreements": cross_surface_disagreements,
        "max_severity": max_severity,
    }


# ── Serialization ─────────────────────────────────────────────────────────────

def _result_to_dict(result: Any, *, surface: str, dim: str, run_id: str) -> Dict[str, Any]:
    d = dataclasses.asdict(result)
    d["_dim"] = dim
    d["_surface"] = surface
    d["_run_id"] = run_id
    d["_ts"] = _now_utc().isoformat()
    return d


def write_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    gitignore = path.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*.jsonl\n*.html\n*.json\n")  # Council C10 MINOR-3
    with path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, default=str) + "\n")


# ── Loading ───────────────────────────────────────────────────────────────────

def load_catalog(path: Path = _CATALOG_PATH) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_eval_config(path: Path = _EVAL_CONFIG_PATH) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_quarantine_ids(path: Path = _QUARANTINE_PATH) -> Set[str]:
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return set(data)
    if isinstance(data, dict):
        return set(data.get("quarantined", []))
    return set()


# ── Core runner (injectable for tests) ───────────────────────────────────────

def run(
    *,
    surfaces: List[str],
    dimensions: List[str],
    model_ids: Optional[List[str]],
    base_url: str,
    api_key: str,
    out_dir: Path,
    run_id: Optional[str] = None,
    surface_factory: SurfaceFactory = default_surface_factory,
    pricing_resolver: Any = _default_pricing_resolver,
    catalog_path: Path = _CATALOG_PATH,
    eval_config_path: Path = _EVAL_CONFIG_PATH,
    quarantine_path: Path = _QUARANTINE_PATH,
    verbose: bool = True,
) -> Tuple[int, Dict[str, Any]]:
    """Run the live tests. Returns ``(exit_code, summary)`` — 0=GO, 1=NO-GO."""
    if run_id is None:
        run_id = _now_utc().strftime("%Y%m%dT%H%M%SZ")

    catalog = load_catalog(catalog_path)
    eval_config = load_eval_config(eval_config_path)
    quarantine_ids = load_quarantine_ids(quarantine_path)
    aliases = catalog.get("aliases", {})
    tolerance_pct = float(
        eval_config.get("dimensions", {}).get("d6_cost_accuracy", {}).get("tolerance_pct", 0.05)
    )
    catalog_entries = {m["id"]: m for m in catalog.get("models", [])}

    if model_ids:
        selected_models = [m for m in model_ids if m in catalog_entries]
        unknown = set(model_ids) - set(catalog_entries)
        if unknown and verbose:
            print(f"[WARN] models not in catalog (ignored): {sorted(unknown)}", file=sys.stderr)
    else:
        selected_models = list(catalog_entries)

    dispatch = _make_dispatch(aliases, pricing_resolver, tolerance_pct)
    results_by_surface_dim_model: Dict[str, Dict[str, Dict[str, Any]]] = {}
    all_records: List[Dict[str, Any]] = []

    for surface_name in surfaces:
        if verbose:
            print(f"[surface={surface_name}]", file=sys.stderr)
        try:
            client = surface_factory(surface_name, base_url, api_key)
        except NotImplementedError as exc:
            if verbose:
                print(f"  SKIP {exc}", file=sys.stderr)
            continue

        surface_results: Dict[str, Dict[str, Any]] = {}
        for dim in dimensions:
            if dim not in dispatch:
                continue
            # Council C10 MAJOR-3 — honour per-dimension surface restriction.
            dim_cfg = eval_config.get("dimensions", {}).get(_dim_config_key(dim), {})
            allowed = dim_cfg.get("surfaces")
            if allowed and surface_name not in allowed:
                if verbose:
                    print(f"  [{dim}] not allowed on {surface_name} — skip", file=sys.stderr)
                continue

            runner = dispatch[dim]
            dim_results: Dict[str, Any] = {}
            for model_id in selected_models:
                result = runner(
                    model_id=model_id, surface_client=client,
                    catalog_entry=catalog_entries[model_id],
                    quarantine_ids=quarantine_ids, clock=time.monotonic,
                )
                dim_results[model_id] = result
                all_records.append(_result_to_dict(result, surface=surface_name, dim=dim, run_id=run_id))
                if verbose:
                    status = "PASS" if result.passed else f"FAIL({result.error_type})"
                    print(f"  [{dim}] {model_id} {status}", file=sys.stderr)
            surface_results[dim] = dim_results
        results_by_surface_dim_model[surface_name] = surface_results

    # Aggregate per (surface, dimension)
    dim_aggregates: Dict[str, Dict[str, Any]] = {}
    for surface_name, surface_results in results_by_surface_dim_model.items():
        for dim, model_map in surface_results.items():
            dim_aggregates[f"{surface_name}.{dim}"] = aggregate(
                list(model_map.values()), eval_config, dim
            )

    cross_cfg = eval_config.get("cross_surface", {})
    excluded_dims_yaml = cross_cfg.get("excluded_dimensions", [])
    excluded_short = {
        _DIM_CONFIG_TO_SHORT.get(d, d) for d in excluded_dims_yaml
    }  # Council C10 MAJOR-1 — inverse map, not split("_")
    disagreements = check_cross_surface_agreement(
        results_by_surface_dim_model, excluded_dimensions=excluded_short,
        severity_on_disagreement=cross_cfg.get("severity_on_disagreement", "MAJOR"),
    )

    verdict_info = compute_verdict(dim_aggregates, disagreements)
    summary = {
        "run_id": run_id,
        "timestamp": _now_utc().isoformat(),
        "surfaces": surfaces,
        "dimensions": dimensions,
        "models": selected_models,
        "verdict": verdict_info["verdict"],
        "failing_dims": verdict_info["failing_dims"],
        "skipped_dimensions": verdict_info["skipped_dimensions"],
        "max_severity": verdict_info["max_severity"],
        "cross_surface_disagreements": disagreements,
        "dim_aggregates": dim_aggregates,
        "raw_records": all_records,  # Council C10 BLOCKER-1 — for the scorecard
    }

    write_jsonl(out_dir / f"{run_id}.jsonl", all_records)

    # Alerting (Council C11) — no-op unless eval_config.alerting.enabled is true.
    alerting_config = eval_config.get("alerting", {})
    if alerting_config.get("enabled", False):
        from tests.live_model_tests.reports.alerting import build_alerts, send_alerts
        send_reports = send_alerts(build_alerts(summary, alerting_config), alerting_config)
        summary["alert_reports"] = send_reports
        if verbose:
            for rep in send_reports:
                state = "OK" if rep["ok"] else f"FAIL({rep['detail']})"
                print(f"  [alert] {rep['channel']} {state}", file=sys.stderr)

    if verbose:
        _print_summary(summary)
    return (0 if verdict_info["verdict"] == "GO" else 1), summary


def _print_summary(summary: Dict[str, Any]) -> None:
    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"  {summary['verdict']}  run_id={summary['run_id']}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    for key, agg in summary["dim_aggregates"].items():
        rate = agg.get("pass_rate")
        rate_str = f"{rate:.1%}" if rate is not None else "N/A"
        sev = agg.get("dimension_severity") or ""
        print(f"  {key:<30} {agg.get('status', '?').upper():<8} {rate_str:>6} {sev}", file=sys.stderr)
    for d in summary.get("cross_surface_disagreements", []):
        print(f"  CROSS-SURFACE: {d['model_id']} {d['dim']} {d['surface_verdicts']}", file=sys.stderr)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Plugboard live tests.")
    p.add_argument("--surface", default="api", help="api,mcp,ui (default: api)")
    p.add_argument("--dimensions", default=",".join(ALL_DIMENSIONS), help="d1,...,d6 (default: all)")
    p.add_argument("--models", default="", help="model IDs (default: all in catalog)")
    p.add_argument("--base-url", default=os.environ.get("PLUGBOARD_BASE_URL", "http://localhost:8765"))
    p.add_argument("--api-key", default=os.environ.get("PLUGBOARD_API_KEY", ""),
                   help="prefer the PLUGBOARD_API_KEY env var (CLI key is visible in ps)")
    p.add_argument("--out", default=str(_DEFAULT_OUT_DIR))
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    surfaces = [s.strip() for s in args.surface.split(",") if s.strip()]
    dimensions = [d.strip() for d in args.dimensions.split(",") if d.strip()]
    model_ids = [m.strip() for m in args.models.split(",") if m.strip()] or None
    exit_code, _ = run(
        surfaces=surfaces, dimensions=dimensions, model_ids=model_ids,
        base_url=args.base_url, api_key=args.api_key, out_dir=Path(args.out),
        verbose=not args.quiet,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
