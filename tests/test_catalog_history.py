"""PB-PROV-02: per-provider catalog history (journal retention, query, route, tab)."""

from __future__ import annotations

from datetime import datetime, timezone

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
