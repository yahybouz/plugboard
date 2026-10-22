#!/usr/bin/env python3
"""Compare billing with and without per-provider prices on recorded usage.

Replays every usage entry of a month (default: the previous month) through
two resolutions:

- before: the vendor price only (how calls were priced before provider
  prices existed);
- after:  the price of the provider that served the call, then the vendor.

Prints the difference per client and provider, and exits 1 when a group
moves by more than --fail-above-pct percent. Run it before merging a change
to price resolution, and after enabling provider prices on an install:

    make billing-reconcile ARGS="--month 2026_09 --fail-above-pct 5"
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugboard import billing  # noqa: E402


def _previous_month(month: str) -> str:
    y, m = (int(x) for x in month.split("_"))
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    return f"{y:04d}_{m:02d}"


def _cost(entry: Dict[str, Any], provider: Any) -> Optional[float]:
    match = billing.resolve_price(
        entry["model"], entry.get("pricing_month") or entry["month"], provider=provider
    )
    if match is None:
        return None
    return billing.calculate_cost(
        int(entry.get("prompt_tokens") or 0), int(entry.get("completion_tokens") or 0), match.entry
    )


def reconcile(entries: Iterable[Dict[str, Any]], providers: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per (client_id, provider_id) with recorded, before and after costs."""
    groups: Dict[tuple, Dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "recorded": 0.0, "before": 0.0, "after": 0.0, "unpriced_after": 0}
    )
    for entry in entries:
        key = (entry.get("client_id", ""), entry.get("provider_id", ""))
        row = groups[key]
        row["calls"] += 1
        row["recorded"] += float(entry.get("cost_usd") or 0)
        before = _cost(entry, None)
        provider = providers.get(entry.get("provider_id", "")) or entry.get("provider_id") or None
        after = _cost(entry, provider)
        row["before"] += before or 0.0
        row["after"] += after or 0.0
        if after is None:
            row["unpriced_after"] += 1
    rows = []
    for (client_id, provider_id), row in sorted(groups.items()):
        diff = row["after"] - row["before"]
        if row["before"]:
            pct = diff / row["before"] * 100
        else:
            pct = 100.0 if diff else 0.0
        rows.append(
            {
                "client_id": client_id,
                "provider_id": provider_id,
                "calls": row["calls"],
                "recorded_usd": round(row["recorded"], 6),
                "before_usd": round(row["before"], 6),
                "after_usd": round(row["after"], 6),
                "diff_usd": round(diff, 6),
                "diff_pct": round(pct, 2),
                "unpriced_after": row["unpriced_after"],
            }
        )
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--month", help="YYYY_MM (default: previous month)")
    parser.add_argument("--fail-above-pct", type=float, default=None,
                        help="exit 1 when a client/provider group moves by more than this")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    from plugboard import usage
    from plugboard.config import load_providers

    month = args.month or _previous_month(billing.current_pricing_month())
    providers = {p.get("id"): p for p in load_providers()}
    rows = reconcile(usage.get_usage(month), providers)

    if args.json:
        print(json.dumps({"month": month, "rows": rows}, indent=2))
    else:
        print(f"Billing reconciliation for {month}: {len(rows)} client/provider groups")
        header = f"{'client':<24} {'provider':<24} {'calls':>7} {'before $':>12} {'after $':>12} {'diff %':>8}"
        print(header)
        print("-" * len(header))
        for r in rows:
            print(
                f"{r['client_id'][:24]:<24} {r['provider_id'][:24]:<24} {r['calls']:>7} "
                f"{r['before_usd']:>12.4f} {r['after_usd']:>12.4f} {r['diff_pct']:>8.2f}"
            )
    if args.fail_above_pct is not None:
        over = [r for r in rows if abs(r["diff_pct"]) > args.fail_above_pct]
        if over:
            print(f"{len(over)} group(s) move by more than {args.fail_above_pct}%", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
