"""Regression tests for the aiohttp/aioresponses compatibility shim."""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses


@pytest.mark.asyncio
async def test_aioresponses_builds_response_when_aiohttp_requires_stream_writer():
    """Mocked responses remain usable with aiohttp's required stream_writer."""
    url = "http://compat.test/response"

    with aioresponses() as mocked:
        mocked.get(url, payload={"ok": True})

        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                assert response.status == 200
                assert await response.json() == {"ok": True}
