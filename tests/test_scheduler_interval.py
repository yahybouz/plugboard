"""Refresh interval of the pricing and model-availability schedulers
(default 6 hours; 0 = daily at the configured hour)."""
from __future__ import annotations

from plugboard.model_availability import ModelAvailabilityScheduler
from plugboard.pricing_scheduler import PricingScheduler


def test_pricing_interval_mode(monkeypatch):
    monkeypatch.setenv("PRICING_REFRESH_INTERVAL_H", "2")
    s = PricingScheduler()
    assert s.refresh_interval_h == 2
    secs = s._seconds_until_next_run()
    assert 7200.0 <= secs <= 7200.0 + 301  # 2h + up to 300s jitter


def test_pricing_default_is_six_hours(monkeypatch):
    monkeypatch.delenv("PRICING_REFRESH_INTERVAL_H", raising=False)
    monkeypatch.delenv("PLUGBOARD_PRICING_REFRESH_INTERVAL_H", raising=False)
    s = PricingScheduler()
    assert s.refresh_interval_h == 6
    assert 6 * 3600 <= s._seconds_until_next_run() <= 6 * 3600 + 301


def test_pricing_zero_means_daily(monkeypatch):
    monkeypatch.setenv("PLUGBOARD_PRICING_REFRESH_INTERVAL_H", "0")
    s = PricingScheduler()
    assert s.refresh_interval_h == 0
    assert s.status_dict["mode"] == "daily"


def test_canonical_name_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("PRICING_REFRESH_INTERVAL_H", "2")
    monkeypatch.setenv("PLUGBOARD_PRICING_REFRESH_INTERVAL_H", "5")
    assert PricingScheduler().refresh_interval_h == 5


def test_cli_interval_default_and_bounds(monkeypatch):
    from plugboard.intervals import cli_refresh_interval_h

    monkeypatch.delenv("PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H", raising=False)
    assert cli_refresh_interval_h() == 24
    monkeypatch.setenv("PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H", "0")
    assert cli_refresh_interval_h() == 1
    monkeypatch.setenv("PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H", "x")
    assert cli_refresh_interval_h() == 24


def test_pricing_interval_clamped_to_24(monkeypatch):
    monkeypatch.setenv("PRICING_REFRESH_INTERVAL_H", "100")
    assert PricingScheduler().refresh_interval_h == 24


def test_pricing_interval_min_one(monkeypatch):
    monkeypatch.setenv("PRICING_REFRESH_INTERVAL_H", "1")
    s = PricingScheduler()
    assert s.refresh_interval_h == 1
    assert 3600.0 <= s._seconds_until_next_run() <= 3600.0 + 301


def test_model_availability_explicit_zero_overrides_pricing(monkeypatch):
    """MODEL_AVAILABILITY_INTERVAL_H=0 explicitly keeps daily even if pricing has interval."""
    monkeypatch.setenv("PRICING_REFRESH_INTERVAL_H", "4")
    monkeypatch.setenv("MODEL_AVAILABILITY_INTERVAL_H", "0")
    assert ModelAvailabilityScheduler().refresh_interval_h == 0


def test_model_availability_inherits_pricing_interval(monkeypatch):
    monkeypatch.setenv("PRICING_REFRESH_INTERVAL_H", "3")
    monkeypatch.delenv("MODEL_AVAILABILITY_INTERVAL_H", raising=False)
    s = ModelAvailabilityScheduler()
    assert s.refresh_interval_h == 3
    secs = s._seconds_until_next_run()
    assert 10800.0 <= secs <= 10800.0 + 301


def test_model_availability_own_interval_overrides(monkeypatch):
    monkeypatch.setenv("PRICING_REFRESH_INTERVAL_H", "3")
    monkeypatch.setenv("MODEL_AVAILABILITY_INTERVAL_H", "1")
    assert ModelAvailabilityScheduler().refresh_interval_h == 1


def test_status_reports_interval(monkeypatch):
    monkeypatch.setenv("PRICING_REFRESH_INTERVAL_H", "4")
    assert PricingScheduler().status_dict["refresh_interval_h"] == 4  # @property
