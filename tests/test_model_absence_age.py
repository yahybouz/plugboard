"""BUG-102: an old absence count does not count as the first absence of today.

The scheduler removes a model after ``PLUGBOARD_MODEL_REMOVAL_PASSES`` (2)
valid discoveries in a row without it. Every absence carries the moment it was
counted; an absence only adds to the previous one when that one is less than
``model_diff.ABSENCE_MAX_AGE_H`` hours old. Otherwise, and for a count written
without a date, the count starts again at 1.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from plugboard import model_diff
from tests.conftest import _setup_providers

PID = "reseller"
TODAY = datetime(2026, 11, 14, 2, 30, tzinfo=timezone.utc)
MODELS = ["alpha", "bravo", "charlie", "delta"]
WITHOUT_DELTA = MODELS[:3]


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.delenv("PLUGBOARD_MODEL_REMOVAL_PASSES", raising=False)
    monkeypatch.setattr(model_diff, "_now", lambda: TODAY)
    pfile = _setup_providers(
        tmp_path,
        monkeypatch,
        [
            {
                "id": PID,
                "name": PID,
                "base_url": "http://localhost:9",
                "default_model": "alpha",
                "models": [{"id": m} for m in MODELS],
                "is_active": True,
            }
        ],
    )
    return pfile


def _absences(pfile) -> dict:
    path = pfile.parent / "catalog" / "model_absences.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_absences(pfile, state: dict) -> None:
    catalog = pfile.parent / "catalog"
    catalog.mkdir(exist_ok=True)
    (catalog / "model_absences.json").write_text(json.dumps(state), encoding="utf-8")


def _saved(pfile) -> list:
    record = json.loads(pfile.read_text(encoding="utf-8"))["providers"][0]
    return [m["id"] for m in record["models"]]


def test_an_absence_is_recorded_with_its_date(provider):
    diff = model_diff.apply(PID, WITHOUT_DELTA)

    assert diff.pending_removal == {"delta": 1}
    assert _absences(provider) == {
        PID: {"delta": {"count": 1, "at": TODAY.isoformat()}}
    }
    assert "delta" in _saved(provider)


def test_a_recent_absence_counts_and_the_model_leaves_at_the_second(provider):
    _write_absences(
        provider,
        {PID: {"delta": {"count": 1, "at": (TODAY - timedelta(hours=24)).isoformat()}}},
    )

    diff = model_diff.apply(PID, WITHOUT_DELTA)

    assert diff.removed == ["delta"]
    assert "delta" not in _saved(provider)
    assert not _absences(provider).get(PID)


def test_an_absence_older_than_the_limit_does_not_count(provider):
    old = TODAY - timedelta(hours=model_diff.ABSENCE_MAX_AGE_H, minutes=1)
    _write_absences(provider, {PID: {"delta": {"count": 1, "at": old.isoformat()}}})

    diff = model_diff.apply(PID, WITHOUT_DELTA)

    assert diff.removed == []
    assert diff.pending_removal == {"delta": 1}
    assert "delta" in _saved(provider)
    assert _absences(provider)[PID]["delta"] == {"count": 1, "at": TODAY.isoformat()}


def test_a_count_of_the_old_format_has_no_date_and_does_not_count(provider):
    _write_absences(provider, {PID: {"delta": 1}})

    diff = model_diff.apply(PID, WITHOUT_DELTA)

    assert diff.removed == []
    assert diff.pending_removal == {"delta": 1}
    assert "delta" in _saved(provider)
    assert _absences(provider)[PID]["delta"] == {"count": 1, "at": TODAY.isoformat()}


def test_the_limit_is_seventy_two_hours():
    assert model_diff.ABSENCE_MAX_AGE_H == 72


def test_a_manual_discover_still_removes_at_once(provider):
    _write_absences(provider, {PID: {"delta": 1}})

    diff = model_diff.apply(PID, WITHOUT_DELTA, "manual")

    assert diff.removed == ["delta"]
    assert not _absences(provider).get(PID)
