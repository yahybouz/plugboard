"""Alerting for the live-test framework — DISABLED by default.

Pure builders (build_alerts, format_slack, format_notion) + best-effort I/O
(send_alerts). Council C11 (2026-05-31), triplet devops:
  - PROPOSER : devops-engineer
  - CRITIC   : codex-devops-engineer-critic (4 MAJOR + minors integrated)

Policy:
  - ``enabled: false`` by default (no-cron). ``send_alerts`` is a no-op unless
    enabled.
  - Secrets are read from env at send time ONLY (eval_config holds no secrets):
        PLUGBOARD_SLACK_WEBHOOK   — Slack incoming webhook URL
        PLUGBOARD_NOTION_TOKEN    — Notion integration token
        PLUGBOARD_NOTION_DB       — Notion database id
  - A down webhook NEVER crashes the run (best-effort; every send returns a
    report dict {channel, ok, status_code, detail}).
  - ``min_severity`` filters alerts below the configured floor (Council C11
    MAJOR-2). A severity absent from ``routing`` is reported, not silently
    dropped (MAJOR-4).
"""

from __future__ import annotations

import dataclasses
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

_LOG = logging.getLogger(__name__)

SEVERITY_ORDER: Dict[str, int] = {"BLOCKER": 4, "MAJOR": 3, "MINOR": 2, "INFO": 1}
_SEVERITY_EMOJI = {
    "BLOCKER": ":red_circle:",
    "MAJOR": ":large_orange_circle:",
    "MINOR": ":large_yellow_circle:",
    "INFO": ":large_blue_circle:",
}
_VALID_CHANNELS = {"slack", "notion"}

_ENV_SLACK_WEBHOOK = "PLUGBOARD_SLACK_WEBHOOK"
_ENV_NOTION_TOKEN = "PLUGBOARD_NOTION_TOKEN"
_ENV_NOTION_DB = "PLUGBOARD_NOTION_DB"

# (url, payload, headers) -> (ok, status_code, detail)
Poster = Callable[[str, Dict[str, Any], Dict[str, str]], Tuple[bool, int, str]]


@dataclasses.dataclass
class Alert:
    severity: str
    title: str
    body: str
    dimension: Optional[str] = None
    context: Dict[str, Any] = dataclasses.field(default_factory=dict)


def _rank(severity: Optional[str]) -> int:
    return SEVERITY_ORDER.get(str(severity or "").upper(), 0)


# ── Pure: build alerts ────────────────────────────────────────────────────────


def build_alerts(
    summary: Dict[str, Any], alerting_config: Dict[str, Any]
) -> List[Alert]:
    """Pure — build the Alert list from a run summary. GO with no findings → []."""
    alerts: List[Alert] = []
    min_sev = str(
        alerting_config.get("min_severity", "INFO")
    ).upper()  # Council C11 MAJOR-2
    run_id = summary.get("run_id", "unknown")
    verdict = summary.get("verdict", "UNKNOWN")
    timestamp = summary.get("timestamp", "")

    for dim_key, agg in (summary.get("dim_aggregates") or {}).items():
        if agg.get("status") != "fail":
            continue
        severity = str(agg.get("dimension_severity") or "MAJOR").upper()
        if _rank(severity) < _rank(min_sev):
            continue
        pass_rate = agg.get("pass_rate")
        threshold = agg.get("threshold")
        rate_str = f"{pass_rate:.1%}" if isinstance(pass_rate, (int, float)) else "N/A"
        thr_str = f"{threshold:.0%}" if isinstance(threshold, (int, float)) else "N/A"
        alerts.append(
            Alert(
                severity=severity,
                title=f"[{severity}] Dimension {dim_key} FAILED — {rate_str} < {thr_str}",
                body=(
                    f"*Run*: `{run_id}`  |  *Time*: {timestamp}\n"
                    f"*Dimension*: `{dim_key}`\n*Pass rate*: {rate_str} (threshold >= {thr_str})\n"
                    f"*Models*: {agg.get('denominator', 0)}  |  *Passed*: {agg.get('passed', 0)}\n"
                    f"*Verdict*: {verdict}"
                ),
                dimension=dim_key,
                context={
                    "run_id": run_id,
                    "verdict": verdict,
                    "pass_rate": pass_rate,
                    "threshold": threshold,
                    "timestamp": timestamp,
                },
            )
        )

    for d in summary.get("cross_surface_disagreements") or []:
        severity = str(d.get("severity") or "MAJOR").upper()
        if _rank(severity) < _rank(min_sev):
            continue
        verdicts = ", ".join(
            f"{s}: {'PASS' if v else 'FAIL'}"
            for s, v in sorted((d.get("surface_verdicts") or {}).items())
        )
        alerts.append(
            Alert(
                severity=severity,
                title=f"[{severity}] Cross-surface disagreement — {d.get('model_id')} {d.get('dim')}",
                body=(
                    f"*Run*: `{run_id}`  |  *Time*: {timestamp}\n"
                    f"*Model*: `{d.get('model_id')}`  |  *Dimension*: `{d.get('dim')}`\n"
                    f"*Verdicts*: {verdicts}\n*Verdict*: {verdict}"
                ),
                dimension=d.get("dim"),
                context={
                    "run_id": run_id,
                    "verdict": verdict,
                    "timestamp": timestamp,
                    "model_id": d.get("model_id"),
                },
            )
        )
    return alerts


# ── Pure: formatters ──────────────────────────────────────────────────────────


def format_slack(alert: Alert) -> Dict[str, Any]:
    emoji = _SEVERITY_EMOJI.get(alert.severity, ":white_circle:")
    header = f"{emoji} {alert.title}"[:150]  # Council C11 P7 — Slack header limit
    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header, "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": alert.body[:2900]}},
    ]
    footer = []
    if alert.context.get("run_id"):
        footer.append(f"run: `{alert.context['run_id']}`")
    if alert.dimension:
        footer.append(f"dim: `{alert.dimension}`")
    if footer:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": " | ".join(footer)}],
            }
        )
    return {"blocks": blocks}


def _notion_date(timestamp: str) -> str:
    """Normalise any ISO-8601 to an offset-bearing ISO-8601 for Notion
    (Council C11 MAJOR-3 — a naive ``[:25]`` slice drops the +00:00 offset)."""
    raw = str(timestamp or "").strip()
    if not raw:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def format_notion(alert: Alert) -> Dict[str, Any]:
    """Notion ``/v1/pages`` payload (``parent.database_id`` injected by
    send_alerts). Required DB columns: Title(title), Severity(select),
    Dimension(rich_text), RunID(rich_text), Verdict(select), Date(date)."""
    ctx = alert.context
    properties: Dict[str, Any] = {
        "Title": {"title": [{"type": "text", "text": {"content": alert.title[:2000]}}]},
        "Severity": {"select": {"name": alert.severity}},
        "RunID": {
            "rich_text": [
                {"type": "text", "text": {"content": str(ctx.get("run_id", ""))[:200]}}
            ]
        },
        "Verdict": {"select": {"name": str(ctx.get("verdict") or "UNKNOWN")}},
    }
    if alert.dimension:
        properties["Dimension"] = {
            "rich_text": [
                {"type": "text", "text": {"content": str(alert.dimension)[:200]}}
            ]
        }
    if ctx.get("timestamp"):
        properties["Date"] = {"date": {"start": _notion_date(ctx["timestamp"])}}
    return {
        "properties": properties,
        "children": [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": alert.body[:2000]}}
                    ]
                },
            }
        ],
    }


# ── I/O: send ─────────────────────────────────────────────────────────────────


def _resolve_secrets() -> Dict[str, Optional[str]]:
    return {
        "slack_webhook": os.environ.get(_ENV_SLACK_WEBHOOK) or None,
        "notion_token": os.environ.get(_ENV_NOTION_TOKEN) or None,
        "notion_database_id": os.environ.get(_ENV_NOTION_DB) or None,
    }


def _default_poster(
    url: str, payload: Dict[str, Any], headers: Dict[str, str]
) -> Tuple[bool, int, str]:
    try:
        import httpx  # lazy — keeps the module importable without httpx

        resp = httpx.post(url, json=payload, headers=headers, timeout=10.0)
        ok = 200 <= resp.status_code < 300
        # Council C11 MINOR-1 — do not echo the response body into the report.
        return (ok, resp.status_code, "" if ok else f"http_error:{resp.status_code}")
    except Exception as exc:  # noqa: BLE001 — best-effort, never crash the run
        return (False, 0, type(exc).__name__)


def send_alerts(
    alerts: List[Alert],
    alerting_config: Dict[str, Any],
    *,
    poster: Optional[Poster] = None,
) -> List[Dict[str, Any]]:
    """Best-effort send. No-op when disabled; never raises."""
    if not alerting_config.get("enabled", False):
        return []
    if not alerts:
        return []

    routing: Dict[str, List[str]] = alerting_config.get("routing") or {}
    unknown = {c for chans in routing.values() for c in chans} - _VALID_CHANNELS
    if unknown:
        _LOG.warning("alerting: unknown channels configured: %s", sorted(unknown))

    post = poster if poster is not None else _default_poster
    secrets = _resolve_secrets()
    reports: List[Dict[str, Any]] = []

    for alert in alerts:
        channels = routing.get(alert.severity) or []
        if not channels:  # Council C11 MAJOR-4 — report, don't drop silently
            reports.append(
                {
                    "alert_title": alert.title,
                    "channel": "routing",
                    "ok": False,
                    "status_code": 0,
                    "detail": f"no route for severity {alert.severity}",
                }
            )
            continue
        for channel in channels:
            base = {"alert_title": alert.title, "channel": channel}
            if channel == "slack":
                url = secrets.get("slack_webhook")
                if not url:
                    reports.append(
                        {
                            **base,
                            "ok": False,
                            "status_code": 0,
                            "detail": f"env {_ENV_SLACK_WEBHOOK} not set",
                        }
                    )
                    continue
                ok, status, detail = post(url, format_slack(alert), {})
                reports.append(
                    {**base, "ok": ok, "status_code": status, "detail": detail}
                )
            elif channel == "notion":
                token = secrets.get("notion_token")
                db_id = secrets.get("notion_database_id")
                if not token or not db_id:
                    missing = _ENV_NOTION_TOKEN if not token else _ENV_NOTION_DB
                    reports.append(
                        {
                            **base,
                            "ok": False,
                            "status_code": 0,
                            "detail": f"env {missing} not set",
                        }
                    )
                    continue
                payload = format_notion(alert)
                payload["parent"] = {"database_id": db_id}  # injected at send time
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": "2022-06-28",
                    "Content-Type": "application/json",
                }
                ok, status, detail = post(
                    "https://api.notion.com/v1/pages", payload, headers
                )
                reports.append(
                    {**base, "ok": ok, "status_code": status, "detail": detail}
                )
            else:
                reports.append(
                    {
                        **base,
                        "ok": False,
                        "status_code": 0,
                        "detail": f"unknown channel: {channel}",
                    }
                )
    return reports
