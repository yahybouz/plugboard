"""Unit tests for the alerting module — no real webhooks.

Council C11 (2026-05-31): devops-engineer + codex-devops-engineer-critic.
"""

from __future__ import annotations


from tests.live_model_tests.reports.alerting import (
    Alert,
    _notion_date,
    build_alerts,
    format_notion,
    format_slack,
    send_alerts,
)


def _fail_summary(*, dim="api.d3", severity="BLOCKER", pass_rate=0.8, threshold=1.0,
                  cross=None, verdict="NO-GO"):
    return {
        "run_id": "20260531T120000Z", "timestamp": "2026-05-31T12:00:00.123456+00:00",
        "verdict": verdict, "cross_surface_disagreements": cross or [],
        "dim_aggregates": {dim: {"status": "fail", "pass_rate": pass_rate, "threshold": threshold,
                                 "dimension_severity": severity, "denominator": 10, "passed": 8}},
    }


def _go_summary():
    return {
        "run_id": "r-go", "timestamp": "2026-05-31T13:00:00+00:00", "verdict": "GO",
        "cross_surface_disagreements": [],
        "dim_aggregates": {"api.d1": {"status": "pass", "pass_rate": 1.0, "threshold": 0.95,
                                      "dimension_severity": None, "denominator": 5, "passed": 5}},
    }


def _cfg(*, enabled=True, routing=None, min_severity="MINOR"):
    return {"enabled": enabled, "min_severity": min_severity,
            "routing": routing or {"BLOCKER": ["slack", "notion"], "MAJOR": ["slack"], "MINOR": ["notion"]}}


# ── build_alerts ─────────────────────────────────────────────────────────────


def test_build_alerts_one_failing_dim():
    alerts = build_alerts(_fail_summary(severity="BLOCKER"), _cfg())
    assert len(alerts) == 1
    a = alerts[0]
    assert a.severity == "BLOCKER" and a.dimension == "api.d3"
    assert "80.0%" in a.body and "100%" in a.body


def test_build_alerts_go_produces_none():
    assert build_alerts(_go_summary(), _cfg()) == []


def test_build_alerts_cross_surface():
    cross = [{"model_id": "m1", "dim": "d4", "severity": "MAJOR",
              "surface_verdicts": {"api": True, "mcp": False}}]
    summary = {"run_id": "r", "timestamp": "t", "verdict": "NO-GO",
               "cross_surface_disagreements": cross, "dim_aggregates": {}}
    alerts = build_alerts(summary, _cfg())
    assert len(alerts) == 1 and alerts[0].severity == "MAJOR"
    assert "m1" in alerts[0].title and "api: PASS" in alerts[0].body


def test_build_alerts_min_severity_filters(monkeypatch):
    """Council C11 MAJOR-2 — min_severity=BLOCKER drops a MAJOR dim failure."""
    summary = _fail_summary(severity="MAJOR")
    alerts = build_alerts(summary, _cfg(min_severity="BLOCKER"))
    assert alerts == []
    # but BLOCKER passes the floor
    assert len(build_alerts(_fail_summary(severity="BLOCKER"), _cfg(min_severity="BLOCKER"))) == 1


# ── formatters ───────────────────────────────────────────────────────────────


def test_format_slack_blocks():
    a = Alert(severity="MAJOR", title="T", body="B", dimension="api.d1", context={"run_id": "R1"})
    p = format_slack(a)
    assert p["blocks"][0]["type"] == "header" and "T" in p["blocks"][0]["text"]["text"]
    assert "B" in p["blocks"][1]["text"]["text"]
    assert "R1" in p["blocks"][2]["elements"][0]["text"]


def test_format_slack_header_truncated():
    a = Alert(severity="BLOCKER", title="X" * 300, body="b")
    assert len(format_slack(a)["blocks"][0]["text"]["text"]) <= 150


def test_format_notion_properties():
    a = Alert(severity="BLOCKER", title="NT", body="NB", dimension="mcp.d4",
              context={"run_id": "R2", "verdict": "NO-GO", "timestamp": "2026-05-31T12:00:00+00:00"})
    p = format_notion(a)
    props = p["properties"]
    assert props["Title"]["title"][0]["text"]["content"] == "NT"
    assert props["Severity"]["select"]["name"] == "BLOCKER"
    assert props["RunID"]["rich_text"][0]["text"]["content"] == "R2"
    assert props["Dimension"]["rich_text"][0]["text"]["content"] == "mcp.d4"
    assert "Date" in props


def test_notion_date_preserves_offset():
    """Council C11 MAJOR-3 — the +00:00 offset must survive."""
    assert _notion_date("2026-05-31T12:00:00.123456+00:00").endswith("+00:00")
    assert _notion_date("2026-05-31T12:00:00Z").endswith("+00:00")
    assert _notion_date("") != ""  # falls back to now(utc)


# ── send_alerts ──────────────────────────────────────────────────────────────


def test_send_disabled_is_noop():
    calls = []

    def poster(u, p, h):
        calls.append(u)
        return (True, 200, "")

    alerts = build_alerts(_fail_summary(), _cfg(enabled=True))
    assert send_alerts(alerts, _cfg(enabled=False), poster=poster) == []
    assert calls == []


def test_send_routes_blocker_to_both_channels(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_SLACK_WEBHOOK", "https://slack")
    monkeypatch.setenv("PLUGBOARD_NOTION_TOKEN", "secret")
    monkeypatch.setenv("PLUGBOARD_NOTION_DB", "db123")
    posted = []

    def poster(url, payload, headers):
        posted.append((url, payload, headers))
        return (True, 200, "")

    cfg = _cfg(enabled=True)
    reports = send_alerts(build_alerts(_fail_summary(severity="BLOCKER"), cfg), cfg, poster=poster)
    chans = {r["channel"] for r in reports}
    assert chans == {"slack", "notion"} and all(r["ok"] for r in reports)
    # Notion payload carries parent.database_id injected at send time
    notion_call = [c for c in posted if "notion" in c[0]][0]
    assert notion_call[1]["parent"]["database_id"] == "db123"
    assert "Bearer secret" in notion_call[2]["Authorization"]


def test_send_missing_secret_skips_without_crash(monkeypatch):
    monkeypatch.delenv("PLUGBOARD_SLACK_WEBHOOK", raising=False)
    monkeypatch.delenv("PLUGBOARD_NOTION_TOKEN", raising=False)
    monkeypatch.delenv("PLUGBOARD_NOTION_DB", raising=False)
    cfg = _cfg(enabled=True)
    reports = send_alerts(build_alerts(_fail_summary(severity="BLOCKER"), cfg), cfg,
                          poster=lambda u, p, h: (True, 200, ""))
    assert all(not r["ok"] for r in reports)
    assert all("not set" in r["detail"] for r in reports)


def test_send_webhook_error_best_effort(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_SLACK_WEBHOOK", "https://slack")
    cfg = _cfg(enabled=True, routing={"MAJOR": ["slack"]})
    reports = send_alerts(build_alerts(_fail_summary(severity="MAJOR"), cfg), cfg,
                          poster=lambda u, p, h: (False, 500, "http_error:500"))
    assert len(reports) == 1 and reports[0]["ok"] is False and reports[0]["status_code"] == 500


def test_send_unrouted_severity_reported_not_dropped(monkeypatch):
    """Council C11 MAJOR-4 — a severity absent from routing is reported."""
    monkeypatch.setenv("PLUGBOARD_SLACK_WEBHOOK", "https://slack")
    # routing has only BLOCKER; a MAJOR alert has no route.
    cfg = _cfg(enabled=True, routing={"BLOCKER": ["slack"]})
    reports = send_alerts(build_alerts(_fail_summary(severity="MAJOR"), cfg), cfg,
                          poster=lambda u, p, h: (True, 200, ""))
    assert len(reports) == 1 and reports[0]["channel"] == "routing"
    assert "no route" in reports[0]["detail"]
