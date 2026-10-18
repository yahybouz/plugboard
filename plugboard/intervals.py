"""Refresh cadences of the pricing and model-catalog schedulers.

All values are hours. 0 means "once a day at the configured hour"; other
values are clamped to 1..24 with a warning. Defaults: 6 hours for prices and
model lists, 24 hours for CLI providers (each refresh spawns the CLIs).
"""

from __future__ import annotations

import logging

from plugboard import envcompat

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_H = 6
DEFAULT_CLI_INTERVAL_H = 24
_warned: set = set()


def _hours(name: str, legacy: tuple, default: int, allow_daily: bool = True) -> int:
    raw = envcompat.get(name, *legacy)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        if name not in _warned:
            _warned.add(name)
            log.warning("%s=%r is not a whole number of hours; using %d", name, raw, default)
        return default
    if value == 0 and allow_daily:
        return 0
    clamped = max(1, min(24, value))
    if clamped != value and name not in _warned:
        _warned.add(name)
        log.warning("%s=%d is outside 1..24; using %d", name, value, clamped)
    return clamped


def pricing_refresh_interval_h() -> int:
    return _hours(
        "PLUGBOARD_PRICING_REFRESH_INTERVAL_H", ("PRICING_REFRESH_INTERVAL_H",), DEFAULT_INTERVAL_H
    )


def models_refresh_interval_h() -> int:
    return _hours(
        "PLUGBOARD_MODEL_AVAILABILITY_INTERVAL_H",
        ("MODEL_AVAILABILITY_INTERVAL_H",),
        pricing_refresh_interval_h(),
    )


def cli_refresh_interval_h() -> int:
    return _hours(
        "PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H", (), DEFAULT_CLI_INTERVAL_H, allow_daily=False
    )


def stale_after_s(interval_h: int) -> int:
    """Age after which a scheduler's last success counts as stale."""
    return int(2 * interval_h * 3600) if interval_h > 0 else 48 * 3600
