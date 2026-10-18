"""TNR-026 — Cache HIT timing side-channel mitigation (closes ARCH-DEBT-026).

When PLUGBOARD_CACHE_RESPONSE_FLOOR_MS is set, a cache HIT response is
delayed until at least the configured floor has elapsed since the request
started. Hides "this prompt is cached" from a LAN-level attacker timing
the response.

Default 0 = off (the leak requires a co-located adversary which is out of
scope for most deployments). High-sensitivity deployments opt-in.
"""

from __future__ import annotations

import time

import pytest


@pytest.mark.asyncio
async def test_tnr_026_floor_zero_is_no_op():
    """Default behavior: floor=0 → no padding, no sleep."""
    import plugboard.app as app_mod

    # Backup, set, restore
    original = app_mod._CACHE_RESPONSE_FLOOR_MS
    app_mod._CACHE_RESPONSE_FLOOR_MS = 0
    try:
        t0 = time.perf_counter()
        await app_mod._pad_cache_hit_response(t0)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 10, f"Floor=0 must not pad ({elapsed_ms:.1f}ms elapsed)"
    finally:
        app_mod._CACHE_RESPONSE_FLOOR_MS = original


@pytest.mark.asyncio
async def test_tnr_026_floor_padded_to_minimum():
    """When floor > elapsed, response must wait until the floor is reached."""
    import plugboard.app as app_mod

    original = app_mod._CACHE_RESPONSE_FLOOR_MS
    app_mod._CACHE_RESPONSE_FLOOR_MS = 50  # 50ms floor
    try:
        t0 = time.perf_counter()
        await app_mod._pad_cache_hit_response(t0)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms >= 49, f"Must pad to >= 50ms (got {elapsed_ms:.1f}ms)"
        assert elapsed_ms < 200, f"Must not over-pad (got {elapsed_ms:.1f}ms)"
    finally:
        app_mod._CACHE_RESPONSE_FLOOR_MS = original


@pytest.mark.asyncio
async def test_tnr_026_no_sleep_when_already_past_floor():
    """If elapsed work already exceeds the floor, no extra sleep."""
    import plugboard.app as app_mod

    original = app_mod._CACHE_RESPONSE_FLOOR_MS
    app_mod._CACHE_RESPONSE_FLOOR_MS = 10  # 10ms floor (very low)
    try:
        # Simulate 50ms of work already done before calling pad.
        t0 = time.perf_counter() - 0.050
        t_call = time.perf_counter()
        await app_mod._pad_cache_hit_response(t0)
        sleep_ms = (time.perf_counter() - t_call) * 1000
        assert sleep_ms < 5, f"Must not sleep when past floor ({sleep_ms:.1f}ms slept)"
    finally:
        app_mod._CACHE_RESPONSE_FLOOR_MS = original
