"""PLUGBOARD_RETRY_DELAYS takes finite seconds, zero or more, only."""

from __future__ import annotations

import math

import pytest

from plugboard import envcompat, router

DEFAULT = [2.0, 5.0, 15.0]


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    # The reader reports a bad value once per process: each test starts clean.
    monkeypatch.setattr(envcompat, "_bad_numbers", set())


@pytest.mark.parametrize(
    "raw",
    ["inf", "nan", "1e999", "-inf", "-1", "2,inf,15", "0.05,nan,0.2", "2,abc"],
)
def test_a_delay_that_is_not_finite_or_is_negative_keeps_the_default(
    monkeypatch, caplog, raw
):
    # asyncio.sleep(inf) and asyncio.sleep(nan) never return: the first retry
    # of a 429, a 5xx or a timeout then held the request for good.
    monkeypatch.setenv("PLUGBOARD_RETRY_DELAYS", raw)

    with caplog.at_level("WARNING"):
        delays = router._read_retry_delays()

    assert delays == DEFAULT
    assert all(math.isfinite(delay) for delay in delays)
    assert caplog.text.count("PLUGBOARD_RETRY_DELAYS") == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.05,0.1,0.2", [0.05, 0.1, 0.2]),
        (" 1 , 2.5 ,30 ", [1.0, 2.5, 30.0]),
        ("1,,3", [1.0, 3.0]),
        ("0", [0.0]),
        ("", DEFAULT),
        (",", DEFAULT),
    ],
)
def test_finite_delays_are_kept(monkeypatch, caplog, raw, expected):
    monkeypatch.setenv("PLUGBOARD_RETRY_DELAYS", raw)

    with caplog.at_level("WARNING"):
        delays = router._read_retry_delays()

    assert delays == expected
    assert "PLUGBOARD_RETRY_DELAYS" not in caplog.text


def test_unset_delays_give_the_default(monkeypatch):
    monkeypatch.delenv("PLUGBOARD_RETRY_DELAYS", raising=False)

    assert router._read_retry_delays() == DEFAULT
