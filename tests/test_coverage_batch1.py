"""
Coverage-boost tests for benchmarks.py and pricing_scraper.py.

Targets the specific uncovered lines identified by coverage analysis.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from aioresponses import aioresponses

from plugboard import benchmarks
from plugboard import pricing_scraper


# ===================================================================
# benchmarks.py — fetch_external_benchmarks (lines 917-941)
# ===================================================================


class TestFetchExternalBenchmarksNoUrl:
    """8 — Returns 0 when no BENCHMARK_SOURCE_URL is configured."""

    @pytest.mark.asyncio
    async def test_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(benchmarks, "_BENCHMARK_SOURCE_URL", "")
        result = await benchmarks.fetch_external_benchmarks()
        assert result == 0


class TestFetchExternalBenchmarksSuccess:
    """9 — Fetches and merges models from external source."""

    @pytest.mark.asyncio
    async def test_merges_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "http://benchmark-source.test/models.json"
        monkeypatch.setattr(benchmarks, "_BENCHMARK_SOURCE_URL", url)

        payload = {
            "models": {
                "test-model-alpha": {
                    "provider": "test",
                    "family": "alpha",
                    "tier": "premium",
                },
                "test-model-beta": {
                    "provider": "test",
                    "family": "beta",
                    "tier": "standard",
                },
            }
        }

        with aioresponses() as mock:
            mock.get(url, payload=payload)
            result = await benchmarks.fetch_external_benchmarks()

        assert result == 2
        assert "test-model-alpha" in benchmarks._MODELS_DB
        assert benchmarks._MODELS_DB["test-model-alpha"]["provider"] == "test"

        # Cleanup injected keys
        benchmarks._MODELS_DB.pop("test-model-alpha", None)
        benchmarks._MODELS_DB.pop("test-model-beta", None)


class TestFetchExternalBenchmarksError:
    """10 — Returns 0 when fetch fails."""

    @pytest.mark.asyncio
    async def test_returns_zero_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "http://benchmark-source.test/models.json"
        monkeypatch.setattr(benchmarks, "_BENCHMARK_SOURCE_URL", url)

        with aioresponses() as mock:
            mock.get(url, exception=ConnectionError("network error"))
            result = await benchmarks.fetch_external_benchmarks()

        assert result == 0


# ===================================================================
# pricing_scraper.py — _fetch_external_source (lines 69-85)
# ===================================================================


class TestFetchExternalSourceNoUrl:
    """11 — Returns empty dict when no PRICING_SOURCE_URL."""

    @pytest.mark.asyncio
    async def test_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", "")
        result = await pricing_scraper._fetch_external_source()
        assert result == {}


class TestFetchExternalSourceSuccess:
    """12 — Fetches pricing JSON from external source."""

    @pytest.mark.asyncio
    async def test_returns_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "https://pricing-source.test/pricing.json"
        monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", url)

        payload = {
            "models": {
                "new-model-x": {"input_price": 1.5, "output_price": 7.5},
            }
        }

        with aioresponses() as mock:
            mock.get(url, payload=payload)
            result = await pricing_scraper._fetch_external_source()

        assert "new-model-x" in result
        assert result["new-model-x"]["input_price"] == 1.5


class TestFetchExternalSourceError:
    """13 — Returns empty dict on fetch failure."""

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://pricing-source.test/pricing.json"
        monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", url)

        with aioresponses() as mock:
            mock.get(url, exception=ConnectionError("boom"))
            result = await pricing_scraper._fetch_external_source()

        assert result == {}


# ===================================================================
# pricing_scraper.py — update_current_pricing with external (lines 168-174, 193)
# ===================================================================


class TestUpdatePricingWithExternal:
    """14 — Merges external pricing into current month grid."""

    @pytest.mark.asyncio
    async def test_merge_external(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "https://pricing-source.test/pricing.json"
        monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", url)

        # Mock load_pricing to return an existing grid
        monkeypatch.setattr(
            pricing_scraper,
            "load_pricing",
            lambda month, strict=False: {
                "models": {
                    "existing-model": {"input_price": 2.0, "output_price": 10.0},
                },
                "note": "USD per 1M tokens",
            },
        )

        saved: Dict[str, Any] = {}

        def mock_save(month: str, data: Dict[str, Any]) -> None:
            saved["month"] = month
            saved["data"] = data

        monkeypatch.setattr(pricing_scraper, "save_pricing", mock_save)
        monkeypatch.setattr(pricing_scraper, "current_pricing_month", lambda: "2026_04")

        external_payload = {
            "models": {
                "brand-new-model": {"input_price": 5.0, "output_price": 25.0},
                "existing-model": {"input_price": 3.0, "output_price": 15.0},
            }
        }

        with aioresponses() as mock:
            mock.get(url, payload=external_payload)
            result = await pricing_scraper.update_current_pricing()

        assert result["added"] == 1  # brand-new-model
        assert result["updated"] == 1  # existing-model price changed
        assert result["source"] == "external"
        assert result["month"] == "2026_04"
        # Verify save was called
        assert saved["month"] == "2026_04"
        assert "brand-new-model" in saved["data"]["models"]


# ===================================================================
# pricing_scraper.py — _discover_new_models with -- separator (lines 113-118)
# ===================================================================


class TestUpdatePricingWithProviderModels:
    """15 — Discover new models from provider list including -- separator."""

    @pytest.mark.asyncio
    async def test_discovers_via_separator(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pricing_scraper, "_PRICING_SOURCE_URL", "")

        # Mute LiteLLM live fetcher to assert source="known_prices" deterministic.
        async def _no_litellm():
            return {}

        monkeypatch.setattr(pricing_scraper, "_fetch_litellm_pricing", _no_litellm)

        monkeypatch.setattr(
            pricing_scraper,
            "load_pricing",
            lambda month, strict=False: {"models": {}, "note": ""},
        )

        saved: Dict[str, Any] = {}

        def mock_save(month: str, data: Dict[str, Any]) -> None:
            saved["month"] = month
            saved["data"] = data

        monkeypatch.setattr(pricing_scraper, "save_pricing", mock_save)
        monkeypatch.setattr(pricing_scraper, "current_pricing_month", lambda: "2026_04")

        # Pass models with : and -- separators that match known prices
        provider_models = [
            "anthropic:claude-opus-4-5",
            "openai--gpt-5-mini",
        ]

        result = await pricing_scraper.update_current_pricing(
            provider_models=provider_models
        )
        assert result["added"] >= 1
        assert result["source"] == "known_prices"

    def test_discover_new_models_separator(self) -> None:
        """Direct test of _discover_new_models with -- separator."""
        existing: Dict[str, Any] = {"gpt-5-mini": {"input_price": 0.3}}
        provider_models = [
            "openai--gpt-5-mini",  # already exists
            "openai--claude-opus-4-5",  # new (despite wrong prefix)
            "anthropic:claude-haiku-4-5",  # new via : separator
        ]
        missing = pricing_scraper._discover_new_models(existing, provider_models)
        # gpt-5-mini should NOT appear since it already exists
        assert "gpt-5-mini" not in missing
        # The others should be discovered
        assert "claude-opus-4-5" in missing or "claude-haiku-4-5" in missing
