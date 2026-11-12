"""PB-PROV-02: per-provider catalog history (journal retention, query, route, tab)."""

from __future__ import annotations

import inspect
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from plugboard import change_events

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


# -- retention ---------------------------------------------------------------------


def test_append_drops_lines_older_than_retention(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "30")
    change_events.append_journal(
        [
            {"at": "2026-10-01T00:00:00Z", "n": 1},
            {"at": "2026-10-20T00:00:00Z", "n": 2},
        ],
        now=NOW,
    )
    change_events.append_journal([{"at": "2026-11-12T00:00:00Z", "n": 3}], now=NOW)
    assert [e["n"] for e in change_events.read_journal(10)] == [2, 3]


def test_append_uses_the_module_clock(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "30")
    monkeypatch.setattr(change_events, "_utcnow", lambda: NOW)
    change_events.append_journal(
        [{"at": "2026-10-01T00:00:00Z", "n": 1}, {"at": "2026-10-20T00:00:00Z", "n": 2}]
    )
    assert [e["n"] for e in change_events.read_journal(10)] == [2]


def test_append_keeps_lines_without_a_date(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "1")
    change_events.append_journal([{"n": 1}, {"at": "not a date", "n": 2}], now=NOW)
    assert [e["n"] for e in change_events.read_journal(10)] == [1, 2]


def test_zero_days_keeps_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "0")
    change_events.append_journal([{"at": "2020-01-01T00:00:00Z", "n": 1}], now=NOW)
    assert len(change_events.read_journal(10)) == 1
    assert change_events.cutoff() is None


@pytest.mark.parametrize("value", [None, "-5", "abc"])
def test_retention_defaults_to_365_days_and_10000_lines(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", raising=False)
    else:
        monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", value)
    assert change_events.retention_days() == 365
    assert change_events.JOURNAL_MAX_LINES == 10000


def test_cutoff_is_now_minus_retention(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "10")
    monkeypatch.setattr(change_events, "_utcnow", lambda: NOW)
    assert change_events.cutoff() == datetime(2026, 11, 2, tzinfo=timezone.utc)
    naive = datetime(2026, 11, 12)  # a naive "now" is read as UTC
    assert change_events.cutoff(naive) == datetime(2026, 11, 2, tzinfo=timezone.utc)


def test_huge_retention_means_no_age_limit(monkeypatch, tmp_path):
    """A value past the calendar ("keep for ever") must not break every write."""
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "1000000")
    assert change_events.cutoff() is None
    change_events.append_journal([{"at": "2020-01-01T00:00:00Z", "n": 1}])
    assert len(change_events.read_journal(10)) == 1
    assert change_events.query_model_history()["total"] == 0


def test_a_later_write_purges_lines_already_written(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    path = tmp_path / "catalog" / "changes.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(
            [
                json.dumps({"at": "2026-09-01T00:00:00+02:00", "n": 1}),
                "not json",
                json.dumps({"at": "0001-01-01T00:00:00+01:00", "n": 2}),
                json.dumps({"at": "2026-10-13T01:00:00+02:00", "n": 3}),
                json.dumps({"at": "2026-10-20T00:00:00Z", "n": 4}),
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "30")  # limit: 2026-10-13
    change_events.append_journal([{"at": "2026-11-12T00:00:00Z", "n": 5}], now=NOW)
    kept = path.read_text().splitlines()
    # Lines without a readable date (not JSON, a date past the calendar) only
    # go with the line cap; 2026-10-13T01:00+02:00 is 23:00 UTC the day before.
    assert kept[0] == "not json"
    assert [json.loads(x)["n"] for x in kept[1:]] == [2, 4, 5]


def test_line_cap_applies_after_the_age_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "30")
    monkeypatch.setattr(change_events, "JOURNAL_MAX_LINES", 3)
    old = [{"at": "2026-01-01T00:00:00Z", "n": i} for i in range(5)]
    new = [{"at": "2026-11-01T00:00:00Z", "n": 10 + i} for i in range(5)]
    change_events.append_journal(old + new, now=NOW)
    assert [e["n"] for e in change_events.read_journal(10)] == [12, 13, 14]


# -- query -------------------------------------------------------------------------

LINES = [
    {
        "at": "2026-01-05T10:00:00Z",
        "event": "models.changed",
        "provider": "provider-a",
        "added": ["alpha-1"],
        "removed": [],
    },
    {
        "at": "2026-03-01T00:00:00Z",
        "event": "pricing.changed",
        "provider": "provider-a",
        "model": "alpha-1",
    },
    {
        "at": "2026-06-30T23:59:59Z",
        "event": "models.changed",
        "provider": "provider-b",
        "added": "oops",
        "removed": ["Beta-Large"],
        "extra": "hidden",
    },
    {
        "at": "2026-07-01T00:00:00Z",
        "event": "models.changed",
        "provider": "old-provider",
        "added": ["gamma"],
        "removed": ["zeta", "alpha-1"],
    },
]


@pytest.fixture
def journal(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    monkeypatch.delenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", raising=False)
    monkeypatch.setattr(change_events, "_utcnow", lambda: NOW)
    path = tmp_path / "catalog" / "changes.jsonl"
    path.parent.mkdir(parents=True)
    rows = [json.dumps(LINES[0]), "not json", json.dumps({"event": "models.changed"})]
    rows += ["[1, 2]", "null", '{"event": "models.changed", "provider": 5}']
    rows += [json.dumps(line) for line in LINES[1:]]
    path.write_text("\n".join(rows) + "\n")
    return path


def _providers(out):
    return [e["provider"] for e in out["entries"]]


def test_history_lists_model_changes_newest_first(journal):
    out = change_events.query_model_history()
    assert _providers(out) == ["old-provider", "provider-b", "provider-a"]
    assert out["total"] == 3
    assert out["providers"] == ["old-provider", "provider-a", "provider-b"]
    assert out["oldest"] == "2026-01-05T10:00:00Z"


def test_history_skips_bad_lines(journal):
    entries = change_events.query_model_history()["entries"]
    assert all(set(e) == {"at", "provider", "added", "removed"} for e in entries)
    assert entries[1] == {
        "at": "2026-06-30T23:59:59Z",
        "provider": "provider-b",
        "added": [],
        "removed": ["Beta-Large"],
    }


def test_history_filters_by_provider(journal):
    out = change_events.query_model_history(provider="provider-b")
    assert _providers(out) == ["provider-b"] and out["total"] == 1
    assert out["providers"] == ["old-provider", "provider-a", "provider-b"]


def test_history_until_is_inclusive(journal):
    out = change_events.query_model_history(until=date(2026, 6, 30))
    assert _providers(out) == ["provider-b", "provider-a"]


def test_history_accepts_the_last_day_of_the_calendar(journal):
    out = change_events.query_model_history(until=date.max, since=date.max)
    assert out["total"] == 0
    assert change_events.query_model_history(until=date.max)["total"] == 3


def test_history_survives_corrupted_lines(journal):
    with journal.open("ab") as fh:
        fh.write(
            b'{"event": "models.changed", "provider": "provider-c", "added": ["\xff"]}\n'
        )
        fh.write(("[" * 100000 + "\n").encode())
        fh.write(
            b'{"at": "0001-01-01T00:00:00+01:00", "event": "models.changed", "provider": "provider-d"}\n'
        )
    out = change_events.query_model_history()
    assert "provider-c" in _providers(out)
    assert out["total"] == 5


def test_history_dates_are_utc(journal):
    with journal.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "at": "2026-08-01T02:00:00+02:00",
                    "event": "models.changed",
                    "provider": "provider-e",
                    "added": ["x"],
                }
            )
            + "\n"
        )
    first = change_events.query_model_history()["entries"][0]
    assert first == {
        "at": "2026-08-01T00:00:00Z",
        "provider": "provider-e",
        "added": ["x"],
        "removed": [],
    }


def test_history_since(journal):
    out = change_events.query_model_history(since=date(2026, 7, 1))
    assert _providers(out) == ["old-provider"]


def test_history_filters_by_model_without_case(journal):
    assert _providers(change_events.query_model_history(model="beta")) == ["provider-b"]
    assert _providers(change_events.query_model_history(model="ALPHA")) == [
        "old-provider",
        "provider-a",
    ]


def test_history_puts_matching_names_first(journal):
    assert change_events.query_model_history()["entries"][0]["removed"] == [
        "zeta",
        "alpha-1",
    ]
    first = change_events.query_model_history(model="alpha")["entries"][0]
    assert first["removed"] == ["alpha-1", "zeta"] and first["added"] == ["gamma"]


def test_history_hides_lines_past_retention(journal, monkeypatch):
    monkeypatch.setenv("PLUGBOARD_CHANGE_JOURNAL_DAYS", "140")  # limit: 2026-06-25
    out = change_events.query_model_history()
    assert _providers(out) == ["old-provider", "provider-b"]
    assert out["providers"] == ["old-provider", "provider-b"]
    assert out["oldest"] == "2026-06-30T23:59:59Z"


def test_history_excludes_undated_lines_with_a_date_filter(journal):
    with journal.open("a") as fh:
        fh.write(
            json.dumps(
                {"event": "models.changed", "provider": "provider-c", "added": ["x"]}
            )
            + "\n"
        )
    assert "provider-c" in _providers(change_events.query_model_history())
    dated = change_events.query_model_history(since=date(2026, 1, 1))
    assert "provider-c" not in _providers(dated)


def test_history_paginates(journal):
    out = change_events.query_model_history(limit=1, offset=1)
    assert _providers(out) == ["provider-b"] and out["total"] == 3


def test_history_without_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("PLUGBOARD_DATA", str(tmp_path))
    assert change_events.query_model_history() == {
        "entries": [],
        "total": 0,
        "providers": [],
        "oldest": None,
    }


# -- route -------------------------------------------------------------------------

ADMIN = {"Authorization": "Bearer adminkey"}
URL = "/admin/catalog/history"


async def test_history_route_requires_admin(admin_client, journal):
    assert (await admin_client.get(URL)).status_code == 401
    client_key = {"Authorization": "Bearer testkey"}
    assert (await admin_client.get(URL, headers=client_key)).status_code == 401
    assert (await admin_client.get(URL + "?limit=0")).status_code == 401


async def test_history_route_returns_a_page(admin_client, journal):
    resp = await admin_client.get(URL + "?limit=1", headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "entries",
        "total",
        "limit",
        "offset",
        "providers",
        "retention",
    }
    assert body["total"] == 3 and body["limit"] == 1 and body["offset"] == 0
    assert [e["provider"] for e in body["entries"]] == ["old-provider"]
    assert body["retention"] == {
        "days": 365,
        "max_lines": 10000,
        "oldest": "2026-01-05T10:00:00Z",
    }


@pytest.mark.parametrize(
    "query, providers",
    [
        ("provider=provider-b", ["provider-b"]),
        ("since=2026-07-01", ["old-provider"]),
        ("until=2026-06-30", ["provider-b", "provider-a"]),
        ("model=beta", ["provider-b"]),
        ("provider=&model=", ["old-provider", "provider-b", "provider-a"]),
        ("offset=2", ["provider-a"]),
        ("until=9999-12-31", ["old-provider", "provider-b", "provider-a"]),
    ],
)
async def test_history_route_passes_filters(admin_client, journal, query, providers):
    body = (await admin_client.get(f"{URL}?{query}", headers=ADMIN)).json()
    assert [e["provider"] for e in body["entries"]] == providers


@pytest.mark.parametrize(
    "query",
    [
        "limit=0",
        "limit=201",
        "offset=-1",
        "since=2026-13-01",
        "until=yesterday",
        "since=2026-07-02&until=2026-07-01",
        "provider=" + "p" * 201,
        "model=" + "m" * 201,
    ],
)
async def test_history_route_rejects_bad_parameters(admin_client, journal, query):
    resp = await admin_client.get(f"{URL}?{query}", headers=ADMIN)
    assert resp.status_code == 422


def test_history_handler_runs_off_the_event_loop():
    from plugboard import app as app_module

    assert not inspect.iscoroutinefunction(app_module.admin_catalog_history)


# -- dashboard tab -----------------------------------------------------------------

STATIC = Path(__file__).resolve().parent.parent / "static"


async def test_dashboard_has_the_catalog_history_tab(admin_client):
    await admin_client.post("/auth/admin/login", json={"key": "adminkey"})
    shell = (await admin_client.get("/dashboard")).text
    assert 'data-tab="catalog-history"' in shell
    assert 'id="page-catalog-history"' in shell
    assert shell.count('href="#catalog-history"') >= 2  # nav + Monitoring link
    assert "/dashboard/catalog_history.js" in shell
    js = await admin_client.get("/dashboard/catalog_history.js")
    assert js.status_code == 200 and "/admin/catalog/history" in js.text


def test_app_js_routes_the_tab():
    assert '"catalog-history": loadCatalogHistory' in (STATIC / "app.js").read_text()


def test_catalog_history_js_writes_text_only():
    js = (STATIC / "catalog_history.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js, sink


@pytest.mark.parametrize(
    "file, label",
    [
        ("index.html", "> Catalog history"),
        ("index.html", "<h2>Catalog history</h2>"),
        ("index.html", '<option value="">All providers</option>'),
        ("index.html", '<option value="">All time</option>'),
        ("index.html", '<option value="7">Last 7 days</option>'),
        ("index.html", '<option value="30">Last 30 days</option>'),
        ("index.html", '<option value="90">Last 90 days</option>'),
        ("index.html", '<option value="365">Last year</option>'),
        ("index.html", 'placeholder="Search a model name"'),
        ("index.html", '<a href="#catalog-history">Full history</a>'),
        ("index.html", 'id="ch-refresh"'),
        (
            "index.html",
            'id="ch-status" class="muted small" role="status" aria-live="polite"',
        ),
        ("catalog_history.js", '"All providers"'),
        ("catalog_history.js", '"Load more ("'),
        ("catalog_history.js", '"No model change matches these filters."'),
        ("catalog_history.js", '"No model change recorded yet."'),
        ("catalog_history.js", '"Catalog history unavailable: "'),
        ("catalog_history.js", '" added"'),
        ("catalog_history.js", '" removed"'),
    ],
)
def test_catalog_history_labels(file, label):
    assert label in (STATIC / file).read_text()


def test_only_the_status_line_is_announced():
    """Screen readers hear "50 of 120 changes", not 50 cards on each filter."""
    html = (STATIC / "index.html").read_text()
    list_tag = html[html.index('id="ch-list"') - 5 : html.index('id="ch-list"') + 60]
    assert "aria-live" not in list_tag
