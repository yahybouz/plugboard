"""
Pricing scraper — fetch LLM prices from several sources into the current month.

Sources, in the order set by PLUGBOARD_PRICING_SOURCES (see pricing_merge):
- external: PLUGBOARD_PRICING_SOURCE_URL, a JSON grid the operator controls;
- provider_native: prices published by a configured provider (OpenRouter);
- models_dev: the models.dev catalogue (already downloaded by models_dev);
- litellm: LiteLLM's public price file;
- known: the built-in table below, for models that no source prices.

pricing_merge decides which price wins. Past months are never modified.

Usage:
  from plugboard.pricing_scraper import update_current_pricing
  result = await update_current_pricing()
"""

from __future__ import annotations

import asyncio
import re
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from plugboard import pricing_merge
from plugboard.billing import (
    PricingFileError,
    current_pricing_month,
    load_pricing,
    pricing_lock,
    save_pricing,
)

log = logging.getLogger(__name__)

# External JSON grid. None reads PLUGBOARD_PRICING_SOURCE_URL (or the older
# PRICING_SOURCE_URL) at call time; tests set a string to override it.
_PRICING_SOURCE_URL: Optional[str] = None

# Last prices fetched per source, reused by revert_model() without a fetch.
_last_sources: Dict[str, Dict[str, Dict[str, float]]] = {}


def _source_url() -> str:
    if _PRICING_SOURCE_URL is not None:
        return _PRICING_SOURCE_URL
    return pricing_merge.pricing_source_url()


# Known provider pricing (fallback when no external source)
# Format: model_suffix -> (input_price, output_price) per 1M tokens
_KNOWN_PRICES: Dict[str, Dict[str, float]] = {
    # Anthropic Claude
    "claude-opus-4-7": {"input_price": 15.0, "output_price": 75.0},
    "claude-opus-4-6": {"input_price": 5.0, "output_price": 25.0},
    "claude-opus-4-5": {"input_price": 5.0, "output_price": 25.0},
    "claude-opus-4-1": {"input_price": 15.0, "output_price": 75.0},
    "claude-opus-4-0": {"input_price": 15.0, "output_price": 75.0},
    "claude-sonnet-4-6": {"input_price": 3.0, "output_price": 15.0},
    "claude-sonnet-4-5": {"input_price": 3.0, "output_price": 15.0},
    "claude-sonnet-4-0": {"input_price": 3.0, "output_price": 15.0},
    "claude-haiku-4-5": {"input_price": 1.0, "output_price": 5.0},
    "claude-3-haiku": {"input_price": 0.25, "output_price": 1.25},
    # OpenAI GPT
    "gpt-5.5-pro": {"input_price": 10.0, "output_price": 40.0},
    "gpt-5.5": {"input_price": 5.0, "output_price": 20.0},
    "gpt-5.5-chat": {"input_price": 5.0, "output_price": 20.0},
    "gpt-5.5-mini": {"input_price": 0.30, "output_price": 1.20},
    "gpt-5.5-nano": {"input_price": 0.10, "output_price": 0.40},
    "gpt-5-4-pro": {"input_price": 10.0, "output_price": 40.0},
    "gpt-5-4": {"input_price": 5.0, "output_price": 20.0},
    "gpt-5-3-codex": {"input_price": 3.0, "output_price": 15.0},
    "gpt-5-2": {"input_price": 2.50, "output_price": 10.0},
    "gpt-5-1": {"input_price": 2.50, "output_price": 10.0},
    "gpt-5-mini": {"input_price": 0.30, "output_price": 1.20},
    "gpt-5-nano": {"input_price": 0.10, "output_price": 0.40},
    "o3": {"input_price": 10.0, "output_price": 40.0},
    "o3-mini": {"input_price": 1.10, "output_price": 4.40},
    "o4-mini": {"input_price": 1.10, "output_price": 4.40},
    "gpt-4o": {"input_price": 2.50, "output_price": 10.0},
    "gpt-5-4-mini": {"input_price": 0.40, "output_price": 1.60},
    # OpenAI GPT — variants
    "gpt-5-pro": {"input_price": 10.0, "output_price": 40.0},
    "gpt-5-2-pro": {"input_price": 5.0, "output_price": 20.0},
    "gpt-5-2-codex": {"input_price": 2.50, "output_price": 10.0},
    "gpt-5-1-codex": {"input_price": 2.50, "output_price": 10.0},
    "gpt-5-1-codex-mini": {"input_price": 0.30, "output_price": 1.20},
    "gpt-5-1-codex-max": {"input_price": 5.0, "output_price": 20.0},
    "gpt-5-1-chat-latest": {"input_price": 2.50, "output_price": 10.0},
    "gpt-5-2-chat-latest": {"input_price": 2.50, "output_price": 10.0},
    "gpt-5-codex": {"input_price": 5.0, "output_price": 20.0},
    "gpt-5-3-codex-spark": {"input_price": 3.0, "output_price": 15.0},
    # OpenAI o-series
    "o1": {"input_price": 15.0, "output_price": 60.0},
    "o1-pro": {"input_price": 150.0, "output_price": 600.0},
    "o3-pro": {"input_price": 20.0, "output_price": 80.0},
    "o3-deep-research": {"input_price": 10.0, "output_price": 40.0},
    "o4-mini-deep-research": {"input_price": 1.10, "output_price": 4.40},
    # xAI Grok
    "grok-code-fast-1": {"input_price": 5.0, "output_price": 15.0},
    # Google Gemini
    "gemini-3-1-pro": {"input_price": 2.0, "output_price": 12.0},
    "gemini-3-1-pro-preview": {"input_price": 2.0, "output_price": 12.0},
    "gemini-3-1-pro-preview-customtools": {"input_price": 2.0, "output_price": 12.0},
    "gemini-3-pro": {"input_price": 0.50, "output_price": 3.0},
    "gemini-3-pro-preview": {"input_price": 0.50, "output_price": 3.0},
    "gemini-2.5-pro": {"input_price": 1.25, "output_price": 10.0},
    "gemini-2.5-flash": {"input_price": 0.15, "output_price": 0.60},
    "gemini-2.0-flash": {"input_price": 0.10, "output_price": 0.40},
    "gemini-3-flash-preview": {"input_price": 0.10, "output_price": 0.40},
    "gemini-3-1-flash-lite-preview": {"input_price": 0.05, "output_price": 0.20},
    # ── Ollama — Local models (inference on user hardware, free) ──────────
    "llama3": {"input_price": 0.0, "output_price": 0.0},
    "llama3:8b": {"input_price": 0.0, "output_price": 0.0},
    "llama3:70b": {"input_price": 0.0, "output_price": 0.0},
    "llama3.1:8b": {"input_price": 0.0, "output_price": 0.0},
    "llama3.1:70b": {"input_price": 0.0, "output_price": 0.0},
    "llama3.2:3b": {"input_price": 0.0, "output_price": 0.0},
    "llama3.3:70b": {"input_price": 0.0, "output_price": 0.0},
    "mistral": {"input_price": 0.0, "output_price": 0.0},
    "mixtral": {"input_price": 0.0, "output_price": 0.0},
    "codellama": {"input_price": 0.0, "output_price": 0.0},
    "phi3": {"input_price": 0.0, "output_price": 0.0},
    "phi3:mini": {"input_price": 0.0, "output_price": 0.0},
    "gemma2": {"input_price": 0.0, "output_price": 0.0},
    "gemma2:9b": {"input_price": 0.0, "output_price": 0.0},
    "gemma2:27b": {"input_price": 0.0, "output_price": 0.0},
    "qwen2.5:7b": {"input_price": 0.0, "output_price": 0.0},
    "deepseek-coder-v2": {"input_price": 0.0, "output_price": 0.0},
    # ── Ollama Turbo — Cloud models (paid, prices from source providers) ──
    # DeepSeek (api.deepseek.com pricing)
    "deepseek-v3": {"input_price": 0.27, "output_price": 1.10},
    "deepseek-v3.1": {"input_price": 0.27, "output_price": 1.10},
    "deepseek-v3.2": {"input_price": 0.27, "output_price": 1.10},
    # Alibaba Qwen (DashScope pricing)
    "qwen3-next": {"input_price": 0.50, "output_price": 2.00},
    "qwen3-vl": {"input_price": 0.50, "output_price": 2.00},
    "qwen3.5": {"input_price": 0.80, "output_price": 3.00},
    "qwen3-coder-next": {"input_price": 0.50, "output_price": 2.00},
    # OpenAI open-source (gpt-oss 20b/120b)
    "gpt-oss": {"input_price": 0.15, "output_price": 0.60},
    # Nvidia NIM
    "nemotron-3-super": {"input_price": 1.00, "output_price": 3.00},
    # Zhipu GLM
    "glm-5": {"input_price": 0.50, "output_price": 2.00},
    # MiniMax
    "minimax-m2.7": {"input_price": 1.00, "output_price": 4.00},
    "minimax-m2.5": {"input_price": 1.00, "output_price": 4.00},
    "minimax-m2.1": {"input_price": 1.00, "output_price": 4.00},
    "minimax-m2": {"input_price": 1.00, "output_price": 4.00},
    # Moonshot Kimi K2
    "kimi-k2.6": {"input_price": 0.60, "output_price": 2.50},
    "kimi-k2.6-thinking": {"input_price": 0.60, "output_price": 2.50},
    "kimi-k2-thinking": {"input_price": 0.60, "output_price": 2.50},
    "kimi-k2.5": {"input_price": 0.60, "output_price": 2.50},
    # ── PRICE-1 additions (full catalogue coverage) ──────────────────────
    # Ollama local (always free — no cloud billing).
    "gemma3": {"input_price": 0.0, "output_price": 0.0},
    "gemma4": {"input_price": 0.0, "output_price": 0.0},
    "deepseek-r1": {"input_price": 0.0, "output_price": 0.0},
    "qwen3": {"input_price": 0.0, "output_price": 0.0},
    # Claude aliases without explicit -latest (CLI short forms).
    "claude-opus-4": {"input_price": 15.0, "output_price": 75.0},
    "claude-sonnet-4": {"input_price": 3.0, "output_price": 15.0},
    "claude-sonnet-4-1": {"input_price": 3.0, "output_price": 15.0},
    "claude-sonnet-3-5": {"input_price": 3.0, "output_price": 15.0},
    "claude-haiku-3-5": {"input_price": 0.80, "output_price": 4.0},
    "claude-3-5-haiku": {"input_price": 0.80, "output_price": 4.0},
    "claude-3-5-haiku-latest": {"input_price": 0.80, "output_price": 4.0},
    "claude-3-5-haiku-20241022": {"input_price": 0.80, "output_price": 4.0},
    "claude-3-7-sonnet": {"input_price": 3.0, "output_price": 15.0},
    "claude-3-7-sonnet-latest": {"input_price": 3.0, "output_price": 15.0},
    "claude-3-7-sonnet-20250219": {"input_price": 3.0, "output_price": 15.0},
    "claude-3-opus": {"input_price": 15.0, "output_price": 75.0},
    "claude-3-opus-20240229": {"input_price": 15.0, "output_price": 75.0},
    "claude-3-sonnet": {"input_price": 3.0, "output_price": 15.0},
    "claude-3-sonnet-20240229": {"input_price": 3.0, "output_price": 15.0},
    # Claude CLI bare aliases (Anthropic accepts "opus", "sonnet", "haiku").
    "opus": {"input_price": 15.0, "output_price": 75.0},
    "sonnet": {"input_price": 3.0, "output_price": 15.0},
    "haiku": {"input_price": 0.80, "output_price": 4.0},
    # Codex CLI aliases.
    "gpt-5": {"input_price": 5.0, "output_price": 20.0},
    "gpt-5.4-nano": {"input_price": 0.10, "output_price": 0.40},
    "gpt-5.4-codex": {"input_price": 5.0, "output_price": 20.0},
    "gpt-4-turbo": {"input_price": 10.0, "output_price": 30.0},
    "gpt-4": {"input_price": 30.0, "output_price": 60.0},
    "o4": {"input_price": 10.0, "output_price": 40.0},
    # Gemini CLI aliases / extras.
    "gemini-2.5-flash-lite": {"input_price": 0.05, "output_price": 0.30},
    "gemini-3.1-flash-preview": {"input_price": 0.10, "output_price": 0.40},
    "gemini-3-pro-image-preview": {"input_price": 0.50, "output_price": 3.0},
    "gemini-3.1-flash-image-preview": {"input_price": 0.10, "output_price": 0.40},
    # Gemini CLI bare aliases ("pro", "flash", "flash-lite").
    "pro": {"input_price": 0.50, "output_price": 3.0},
    "flash": {"input_price": 0.10, "output_price": 0.40},
    "flash-lite": {"input_price": 0.05, "output_price": 0.30},
    # GitHub Models — namespaced IDs (vendor/model).
    "openai/gpt-5.5": {"input_price": 5.0, "output_price": 20.0},
    "openai/gpt-5.5-chat": {"input_price": 5.0, "output_price": 20.0},
    "openai/gpt-5.5-mini": {"input_price": 0.30, "output_price": 1.20},
    "openai/gpt-5.5-nano": {"input_price": 0.10, "output_price": 0.40},
    "openai/gpt-5.5-pro": {"input_price": 10.0, "output_price": 40.0},
    "openai/gpt-4o": {"input_price": 2.50, "output_price": 10.0},
    "openai/gpt-4o-mini": {"input_price": 0.15, "output_price": 0.60},
    "openai/gpt-4.1": {"input_price": 2.50, "output_price": 10.0},
    "openai/gpt-4.1-mini": {"input_price": 0.40, "output_price": 1.60},
    "openai/gpt-4.1-nano": {"input_price": 0.10, "output_price": 0.40},
    "openai/gpt-5": {"input_price": 5.0, "output_price": 20.0},
    "openai/gpt-5-chat": {"input_price": 5.0, "output_price": 20.0},
    "openai/o1-mini": {"input_price": 1.10, "output_price": 4.40},
    "openai/o1-preview": {"input_price": 15.0, "output_price": 60.0},
    "meta/llama-3.2-11b-vision-instruct": {"input_price": 0.0, "output_price": 0.0},
    "meta/llama-3.2-90b-vision-instruct": {"input_price": 0.0, "output_price": 0.0},
    "meta/llama-3.3-70b-instruct": {"input_price": 0.0, "output_price": 0.0},
    "meta/llama-4-maverick-17b-128e-instruct-fp8": {
        "input_price": 0.0,
        "output_price": 0.0,
    },
    "meta/llama-4-scout-17b-16e-instruct": {"input_price": 0.0, "output_price": 0.0},
    "meta/meta-llama-3.1-405b-instruct": {"input_price": 0.0, "output_price": 0.0},
    "meta/meta-llama-3.1-8b-instruct": {"input_price": 0.0, "output_price": 0.0},
    "deepseek/deepseek-r1": {"input_price": 0.27, "output_price": 1.10},
    "deepseek/deepseek-r1-0528": {"input_price": 0.27, "output_price": 1.10},
    "deepseek/deepseek-v3": {"input_price": 0.27, "output_price": 1.10},
    "deepseek/deepseek-v3-0324": {"input_price": 0.27, "output_price": 1.10},
    "mistral-ai/codestral-2501": {"input_price": 0.30, "output_price": 0.90},
    "mistral-ai/ministral-3b": {"input_price": 0.04, "output_price": 0.04},
    "mistral-ai/mistral-medium-2505": {"input_price": 0.40, "output_price": 2.0},
    "mistral-ai/mistral-small-2503": {"input_price": 0.20, "output_price": 0.60},
    "xai/grok-3": {"input_price": 3.0, "output_price": 15.0},
    "xai/grok-3-mini": {"input_price": 0.30, "output_price": 0.50},
    "microsoft/mai-ds-r1": {"input_price": 0.0, "output_price": 0.0},
    "microsoft/phi-4": {"input_price": 0.0, "output_price": 0.0},
    "microsoft/phi-4-mini-instruct": {"input_price": 0.0, "output_price": 0.0},
    "microsoft/phi-4-mini-reasoning": {"input_price": 0.0, "output_price": 0.0},
    "microsoft/phi-4-multimodal-instruct": {"input_price": 0.0, "output_price": 0.0},
    "microsoft/phi-4-reasoning": {"input_price": 0.0, "output_price": 0.0},
    "cohere/cohere-command-a": {"input_price": 2.50, "output_price": 10.0},
    "cohere/cohere-command-r-08-2024": {"input_price": 0.15, "output_price": 0.60},
    "cohere/cohere-command-r-plus-08-2024": {"input_price": 2.50, "output_price": 10.0},
    "ai21-labs/ai21-jamba-1.5-large": {"input_price": 2.0, "output_price": 8.0},
    "ai21-labs/ai21-jamba-1.5-mini": {"input_price": 0.20, "output_price": 0.40},
}


def _PRICING_DIR_path(month: str):
    from plugboard import billing

    return billing._PRICING_DIR / f"{month}.json"


def _secure_source(url: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and (parts.hostname or "") in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


async def _fetch_external_source() -> Dict[str, Dict[str, float]]:
    """Fetch the operator's grid (expects JSON {models: {...}})."""
    url = _source_url()
    if not url:
        return {}
    if not _secure_source(url):
        log.error(
            "PLUGBOARD_PRICING_SOURCE_URL must use https (http is accepted only for "
            "localhost): this source is trusted without review, so a clear-text "
            "request would let anyone on the network change billed prices. Ignored."
        )
        return {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("Pricing source returned %d", resp.status)
                    return {}
                data = await resp.json()
                models = data.get("models", data)
                if isinstance(models, dict):
                    log.info(
                        "Fetched %d models from external pricing source", len(models)
                    )
                    return models
    except Exception as exc:
        log.warning("Failed to fetch external pricing source: %s", exc)
    return {}


async def _fetch_anthropic_pricing() -> Dict[str, Dict[str, float]]:
    """Try to fetch Anthropic pricing from their public API."""
    # Anthropic doesn't have a JSON pricing API — use known prices
    return {}


async def _fetch_openai_pricing() -> Dict[str, Dict[str, float]]:
    """Try to fetch OpenAI pricing from their public API."""
    # OpenAI doesn't have a JSON pricing API — use known prices
    return {}


# LiteLLM maintains a public, MIT-licensed JSON pricing file that
# covers Anthropic, OpenAI, Google, Mistral, Cohere, AI21, DeepSeek,
# Meta, xAI and dozens of other providers. It's updated on every
# provider price change and has ~2700 entries as of 2026-04. Using
# it lets plugboard price newly-released models automatically
# without hand-editing `_KNOWN_PRICES`.
_LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)


async def _fetch_litellm_pricing() -> Dict[str, Dict[str, float]]:
    """Fetch and flatten the LiteLLM public pricing JSON.

    Their schema uses per-token costs as floats ; we convert to our
    per-1M-tokens format. Only entries with `mode=chat` and both
    input/output costs are returned — image/audio/search entries are
    skipped because our billing layer doesn't have slots for them.

    Key normalisation : LiteLLM ships both `gpt-4o` (OpenAI direct)
    and `openai/gpt-4o` (GitHub Models format). We emit both so
    `_candidate_keys` can find either.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _LITELLM_PRICING_URL,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("LiteLLM pricing endpoint returned %d", resp.status)
                    return {}
                raw = await resp.json(content_type=None)
    except Exception as exc:
        log.warning("Failed to fetch LiteLLM pricing: %s", exc)
        return {}

    result: Dict[str, Dict[str, float]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("mode") != "chat":
            continue
        in_cost = entry.get("input_cost_per_token")
        out_cost = entry.get("output_cost_per_token")
        if in_cost is None or out_cost is None:
            continue
        try:
            priced = {
                "input_price": round(float(in_cost) * 1_000_000, 6),
                "output_price": round(float(out_cost) * 1_000_000, 6),
            }
        except (TypeError, ValueError):
            continue
        result[key] = priced
        # Also emit `openai/foo` alias for GitHub-Models-style keys
        # when the source key is an OpenAI model without provider
        # prefix — covers our `openai/gpt-4o` namespacing.
        if "/" not in key and any(
            key.startswith(prefix)
            for prefix in ("gpt-", "o1", "o3", "o4", "chatgpt-", "text-")
        ):
            result[f"openai/{key}"] = priced
    log.info("Fetched %d chat models from LiteLLM public pricing", len(result))
    return result


def _discover_new_models(
    existing_models: Dict[str, Any],
    provider_models: List[str],
) -> List[str]:
    """Find models from providers that don't have pricing yet."""
    from plugboard.billing import _normalize

    existing_normalized = {_normalize(k) for k in existing_models}
    missing = []
    for model_id in provider_models:
        # Extract model suffix (after provider prefix)
        parts = model_id.split(":", 1)
        suffix = parts[-1] if len(parts) > 1 else model_id
        # Also try after -- separator (OpenClaw format)
        if "--" in suffix:
            suffix = suffix.split("--", 1)[-1]
        norm = _normalize(suffix)
        # Also check Ollama base name before ':' tag (e.g. "deepseek-v3.1" from "deepseek-v3.1:671b-cloud")
        # Check both the suffix and the original model_id for ':' tag format
        ollama_base = ""
        if ":" in model_id:
            ollama_base = model_id.split(":")[0]
        if ":" in suffix:
            ollama_base = suffix.split(":")[0] or ollama_base
        base_name = ollama_base
        base_norm = _normalize(base_name) if base_name else ""
        if (
            norm not in existing_normalized
            and suffix not in existing_models
            and (not base_norm or base_norm not in existing_normalized)
            and (not base_name or base_name not in existing_models)
        ):
            missing.append(suffix)
    return missing


def _match_known_price(model_suffix: str) -> Optional[Dict[str, float]]:
    """Try to match a model suffix against _KNOWN_PRICES."""
    from plugboard.billing import _normalize

    # Exact match
    if model_suffix in _KNOWN_PRICES:
        return _KNOWN_PRICES[model_suffix]
    # Normalized match
    norm = _normalize(model_suffix)
    for known, price in _KNOWN_PRICES.items():
        if _normalize(known) == norm:
            return price
    # Ollama format: try model name before ':' tag (e.g. "deepseek-v3.1:671b-cloud" -> "deepseek-v3.1")
    if ":" in model_suffix:
        base_name = model_suffix.split(":")[0]
        if base_name in _KNOWN_PRICES:
            return _KNOWN_PRICES[base_name]
        base_norm = _normalize(base_name)
        for known, price in _KNOWN_PRICES.items():
            if _normalize(known) == base_norm:
                return price
    # Suffix match (longest wins)
    candidates = [
        (k, v) for k, v in _KNOWN_PRICES.items() if norm.endswith(_normalize(k))
    ]
    if candidates:
        best = max(candidates, key=lambda x: len(x[0]))
        return best[1]
    return None


_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OLLAMA_PRICING_URL = "https://ollama.com/pricing"

# models.dev lists the same model under many resellers. The bare model id
# takes the price of the first vendor below that lists it.
_MODELS_DEV_VENDORS = (
    "openai",
    "anthropic",
    "google",
    "mistral",
    "deepseek",
    "xai",
    "moonshotai",
    "alibaba",
    "cohere",
    "minimax",
    "zhipuai",
)

# Resellers whose own prices are kept in models_by_provider when they differ
# from the vendor's. The key is the models.dev provider id, which is also the
# price space of a provider created from the catalog preset.
AGGREGATORS = ("openrouter", "opencode", "deepinfra", "togetherai", "fireworks-ai")

Prices = Dict[str, Dict[str, float]]


def _openrouter_configured() -> bool:
    try:
        from plugboard.config import load_providers

        providers = load_providers()
    except Exception as exc:  # noqa: BLE001 - a broken store must not stop pricing
        log.warning("provider_native pricing: cannot read providers: %s", exc)
        return False
    return any("openrouter.ai" in (p.get("base_url") or "") for p in providers)


async def _fetch_provider_native_pricing() -> Prices:
    """Prices published by OpenRouter (per token, as strings) when it is configured."""
    if not _openrouter_configured():
        return {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _OPENROUTER_MODELS_URL, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    log.warning("OpenRouter models endpoint returned %d", resp.status)
                    return {}
                raw = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to fetch OpenRouter prices: %s", exc)
        return {}
    result: Prices = {}
    for entry in (raw or {}).get("data", []) if isinstance(raw, dict) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        pricing = entry.get("pricing") or {}
        inp = pricing_merge.per_token_to_per_million(pricing.get("prompt"))
        out = pricing_merge.per_token_to_per_million(pricing.get("completion"))
        if inp is None or out is None:
            continue  # "-1" marks a router whose price depends on the model chosen
        result[entry["id"]] = {"input_price": inp, "output_price": out}
    log.info("Fetched %d prices from OpenRouter", len(result))
    return result


def _ollama_configured() -> bool:
    try:
        from plugboard.config import load_providers

        providers = load_providers()
    except Exception as exc:  # noqa: BLE001 - a broken store must not stop pricing
        log.warning("ollama pricing: cannot read providers: %s", exc)
        return False
    return any(
        str(p.get("id") or "").startswith("ollama")
        or ":11434" in (p.get("base_url") or "")
        for p in providers
    )


def _dollars(cell: str) -> Optional[float]:
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", cell)
    return float(match.group(1)) if match else None


def parse_ollama_pricing(html: str) -> Prices:
    """Cloud prices from the "Model pricing" table of ollama.com/pricing.

    Prices are USD per million tokens. Each row gives a model name (``gpt-oss:120b``
    or a family such as ``gemma4``) and input / cached input / output prices;
    the peak price is kept and the ``(Off-Peak)`` rows are skipped. Keys are the
    cloud ids Ollama serves: ``gpt-oss:120b-cloud``, ``gemma4:cloud`` (a family
    price, matched by every ``gemma4:<tag>-cloud``). Rows without an output
    price are skipped.
    """
    start = html.find("Model pricing")
    if start < 0:
        return {}
    end = html.find("</table>", start)
    table = html[start : end if end > 0 else None]
    prices: Prices = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, flags=re.S | re.I):
        cells = [
            re.sub(r"<[^>]+>", "", c).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)
        ]
        if len(cells) < 4 or "off-peak" in cells[0].lower():
            continue
        name = cells[0].split()[0].strip().lower()
        inp, cached, out = _dollars(cells[1]), _dollars(cells[2]), _dollars(cells[3])
        if not name or inp is None or out is None:
            continue
        from plugboard.ollama import _to_cloud_id

        entry: Dict[str, float] = {"input_price": inp, "output_price": out}
        if cached is not None:
            entry["cached_input_price"] = cached
        prices[_to_cloud_id(name)] = entry
    return prices


async def _fetch_ollama_pricing() -> Prices:
    """Ollama's published cloud prices, when an Ollama provider is configured."""
    if not _ollama_configured():
        return {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _OLLAMA_PRICING_URL, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    log.warning("Ollama pricing page returned %d", resp.status)
                    return {}
                html = await resp.text()
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to fetch Ollama prices: %s", exc)
        return {}
    prices = parse_ollama_pricing(html)
    if not prices:
        log.warning("Ollama pricing page: no price table found (layout change?)")
    else:
        log.info("Fetched %d prices from Ollama", len(prices))
    return prices


def _catalog_costs(provider: Any) -> Prices:
    prices: Prices = {}
    for model_id, model in provider.models.items():
        cost = model.cost
        if cost is None or cost.input is None or cost.output is None:
            continue
        prices[model_id] = {"input_price": cost.input, "output_price": cost.output}
    return prices


def _models_dev_pricing() -> Tuple[Prices, Dict[str, Prices]]:
    """Vendor prices and reseller prices from the models.dev catalogue on disk."""
    from plugboard import models_dev

    try:
        catalog = models_dev.load_catalog()
    except Exception as exc:  # noqa: BLE001
        log.warning("models.dev pricing unavailable: %s", exc)
        return {}, {}
    vendor: Prices = {}
    for vendor_id in _MODELS_DEV_VENDORS:
        if vendor_id in catalog:
            for model_id, price in _catalog_costs(catalog[vendor_id]).items():
                vendor.setdefault(model_id, price)
    resellers = {
        space: _catalog_costs(catalog[space])
        for space in AGGREGATORS
        if space in catalog
    }
    return vendor, {space: prices for space, prices in resellers.items() if prices}


async def _fetch_sources(
    order: List[str],
) -> Tuple[Dict[str, Prices], Dict[str, Dict[str, Prices]]]:
    """Fetch every listed source.

    Returns (vendor sources, reseller sources by space). Vendor sources feed
    ``models``; reseller sources feed ``models_by_provider``.
    """
    fetchers = {
        "external": _fetch_external_source,
        "provider_native": _fetch_provider_native_pricing,
        "ollama_native": _fetch_ollama_pricing,
        "litellm": _fetch_litellm_pricing,
    }
    names = [name for name in order if name in fetchers]
    if "provider_native" in names:
        names.append("ollama_native")  # Ollama's own table is a provider-native source
    results = await asyncio.gather(
        *(fetchers[name]() for name in names), return_exceptions=True
    )
    fetched: Dict[str, Prices] = {}
    for name, value in zip(names, results):
        if isinstance(value, BaseException):
            log.warning("Pricing source %s failed: %s", name, value)
            value = {}
        fetched[name] = value or {}
    by_space: Dict[str, Dict[str, Prices]] = {}
    native = fetched.pop("provider_native", {})
    if native:
        by_space.setdefault("openrouter", {})["provider_native"] = native
    ollama_native = fetched.pop("ollama_native", {})
    if ollama_native:
        by_space.setdefault("ollama", {})["provider_native"] = ollama_native
    if "models_dev" in order:
        vendor, resellers = _models_dev_pricing()
        fetched["models_dev"] = vendor
        for space, prices in resellers.items():
            by_space.setdefault(space, {})["models_dev"] = prices
    return fetched, by_space


def _model_suffix(model_id: str) -> str:
    parts = model_id.split(":", 1)
    suffix = parts[-1] if len(parts) > 1 else model_id
    if "--" in suffix:
        suffix = suffix.split("--", 1)[-1]
    return suffix


def _known_source(
    existing_models: Dict[str, Any], provider_models: Optional[List[str]]
) -> Prices:
    """Built-in prices, only for routable models that no grid entry prices.

    The fuzzy match in _match_known_price is good enough to fill a gap, not
    to override a price that another source gave.
    """
    known: Prices = {}
    for suffix in _discover_new_models(existing_models, provider_models or []):
        price = _match_known_price(suffix)
        if price:
            known[suffix] = dict(price)
    for model_id in provider_models or []:
        suffix = _model_suffix(model_id)
        current = existing_models.get(suffix)
        if current is None or current.get("source"):
            continue
        if current.get("input_price", 0) == 0 and current.get("output_price", 0) == 0:
            price = _match_known_price(suffix)
            if price and (price["input_price"] > 0 or price["output_price"] > 0):
                known[suffix] = dict(price)
    return known


def _vendor_price(model: str, models: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from plugboard import billing

    match = billing.match_in_grid(
        {"models": models}, [], billing._candidate_keys(model)
    )
    return match.entry if match else None


def _same_as_vendor(model: str, price: Dict[str, Any], models: Dict[str, Any]) -> bool:
    vendor = _vendor_price(model, models)
    return vendor is not None and pricing_merge.same_price(vendor, price)


def _merge_spaces(
    current: Dict[str, Dict[str, Any]],
    by_space: Dict[str, Dict[str, Prices]],
    models: Dict[str, Any],
    order: List[str],
    drift_pct: float,
    action: str,
    reviewed_all: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None,
) -> Tuple[
    Dict[str, Dict[str, Any]],
    List[Dict[str, Any]],
    int,
    List[Dict[str, Any]],
    List[Dict[str, Any]],
]:
    """Merge reseller prices; keep only those that differ from the vendor's.

    Returns (models_by_provider, drifts, added, updated, rejected); drift,
    update and rejection items carry their ``space``.
    """
    out: Dict[str, Dict[str, Any]] = {}
    drifts: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    updated: List[Dict[str, Any]] = []
    added = 0
    for space in sorted(set(current) | set(by_space)):
        existing = {
            m: dict(e)
            for m, e in (current.get(space) or {}).items()
            if e.get("source") == "manual" or not _same_as_vendor(m, e, models)
        }
        sources = {
            name: {m: p for m, p in prices.items() if not _same_as_vendor(m, p, models)}
            for name, prices in (by_space.get(space) or {}).items()
        }
        res = pricing_merge.merge_sources(
            existing,
            sources,
            order,
            drift_pct=drift_pct,
            action=action,
            reviewed=(reviewed_all if reviewed_all is not None else {}).setdefault(
                space, {}
            ),
        )
        for drift in res.drifts:
            drift["space"] = space
        for item in res.rejected:
            item["space"] = space
        drifts.extend(res.drifts)
        rejected.extend(res.rejected)
        added += len(res.added)
        updated.extend({**item, "space": space} for item in res.updated)
        if res.models:
            out[space] = res.models
    return out, drifts, added, updated, rejected


def _legacy_source_label(sources: Dict[str, Dict[str, Any]], order: List[str]) -> str:
    for name in order:
        if sources.get(name):
            return "known_prices" if name == "known" else name
    return "known_prices"


# Last prices fetched, reused by revert_model() without a network call.
_last_space_sources: Dict[str, Dict[str, Prices]] = {}


async def update_current_pricing(
    provider_models: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Refresh the current month's grid from every configured source.

    Sources are fetched first, outside any lock. The grid is then re-read,
    merged and written under the month's lock, so a concurrent admin edit is
    never lost. Previous months are read (inheritance) but never written.

    Returns: {month, added, updated, inherited, total, source, sources,
    drifts, rejected, provider_prices} plus {persisted: False,
    sentinel_refused: True} when the shrink guard refuses to write.
    """
    month = current_pricing_month()
    order = pricing_merge.source_order(external_configured=bool(_source_url()))
    sources, by_space = await _fetch_sources(order)
    drift_pct = pricing_merge.drift_threshold_pct()
    action = pricing_merge.drift_action()

    with pricing_lock(month):
        try:
            existing = load_pricing(month, strict=True)
        except PricingFileError as exc:
            # Keep the damaged file for inspection and rebuild the month from
            # the previous one and the sources, rather than fail every pass.
            damaged = _PRICING_DIR_path(month)
            aside = damaged.with_suffix(
                f".corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
            )
            damaged.rename(aside)
            log.critical("%s; moved it to %s and rebuilt the grid", exc, aside.name)
            existing = {}
        before = {k: dict(v) for k, v in (existing.get("models") or {}).items()}
        before_spaces = existing.get("models_by_provider") or {}
        existing_models = {k: dict(v) for k, v in before.items()}
        inherited = 0

        # Inherit from the previous month (fill-only baseline): a grid seeded
        # with a single test model must not shrink the catalogue.
        prev_month = _previous_month(month)
        prev: Dict[str, Any] = {}
        if prev_month:
            prev = load_pricing(prev_month)
            for model, price in (prev.get("models") or {}).items():
                if model not in existing_models:
                    existing_models[model] = dict(price)
                    inherited += 1
            if inherited:
                log.info(
                    "Inherited %d models from %s into %s", inherited, prev_month, month
                )
        spaces_in = {
            s: dict(sub) for s, sub in (prev.get("models_by_provider") or {}).items()
        }
        for space, sub in before_spaces.items():
            spaces_in[space] = {**spaces_in.get(space, {}), **sub}

        if "known" in order:
            sources["known"] = _known_source(existing_models, provider_models)
        _last_sources.clear()
        _last_sources.update({name: dict(prices) for name, prices in sources.items()})
        _last_space_sources.clear()
        _last_space_sources.update({s: dict(src) for s, src in by_space.items()})

        reviewed_all = {
            k: dict(v) for k, v in (existing.get("reviewed_prices") or {}).items()
        }
        merged = pricing_merge.merge_sources(
            existing_models,
            sources,
            order,
            drift_pct=drift_pct,
            action=action,
            reviewed=reviewed_all.setdefault("", {}),
        )
        models = merged.models
        spaces, space_drifts, space_added, space_updated, space_rejected = (
            _merge_spaces(
                spaces_in, by_space, models, order, drift_pct, action, reviewed_all
            )
        )
        for item in merged.rejected + space_rejected:
            log.warning(
                "Pricing %s: rejected %s price for %s (out of bounds or ratio): %s",
                month,
                item["source"],
                item["model"],
                item["entry"],
            )
        used = [
            name
            for name in order
            if sources.get(name) or any(name in src for src in by_space.values())
        ]
        summary = {
            "month": month,
            "added": len(merged.added) + space_added,
            "updated": len(merged.updated) + len(space_updated),
            "inherited": inherited,
            "total": len(models),
            "provider_prices": sum(len(sub) for sub in spaces.values()),
            "source": _legacy_source_label(sources, order),
            "sources": used,
            "drifts": len(merged.drifts) + len(space_drifts),
            "rejected": len(merged.rejected) + len(space_rejected),
        }

        # Shrink guard: refuse to persist a grid that lost more than 80% of
        # the previous month's models (a partial read once produced 2700 -> 1).
        prev_total = len(prev.get("models") or {})
        if prev_total > 50 and len(models) < prev_total * 0.2:
            log.critical(
                "Pricing %s SENTINEL REFUSED: in-memory grid has %d models but %s had %d. "
                "Persist aborted to avoid corrupting the on-disk snapshot.",
                month,
                len(models),
                prev_month,
                prev_total,
            )
            return {**summary, "persisted": False, "sentinel_refused": True}

        reviewed_all = {k: v for k, v in reviewed_all.items() if v}
        if (
            models != before
            or spaces != before_spaces
            or reviewed_all != (existing.get("reviewed_prices") or {})
        ):
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            note = existing.get("note", "")
            label = ",".join(used) or str(summary["source"])
            if not note:
                note = f"USD per 1M tokens. Updated {now} via scraper ({label})"
            else:
                note = note.rstrip(".") + f". Updated {now} ({label})"
            grid = {**existing, "models": models, "note": note}
            if reviewed_all:
                grid["reviewed_prices"] = reviewed_all
            if spaces:
                grid["models_by_provider"] = spaces
            else:
                grid.pop("models_by_provider", None)
            save_pricing(month, grid)
            log.info(
                "Pricing %s: +%d added, %d updated, %d held for review, %d models, %d provider prices",
                month,
                summary["added"],
                summary["updated"],
                summary["drifts"],
                len(models),
                summary["provider_prices"],
            )

    drifts = merged.drifts + space_drifts
    new_drifts: List[Dict[str, Any]] = []
    if drifts:
        from plugboard import pricing_suggestions

        created = set(pricing_suggestions.record_drifts(month, drifts))
        # Only a newly queued suggestion is announced, not the same one each pass.
        new_drifts = [
            d
            for d in drifts
            if pricing_suggestions.suggestion_id(
                "drift", d["model"], d["new"], d.get("space", "")
            )
            in created
        ]
    batch = price_change_events(
        month, merged.updated + space_updated, new_drifts, drift_pct
    )
    if len(batch):
        summary["webhooks"] = batch.flush_in_background()
    return summary


def price_change_events(
    month: str,
    updated: List[Dict[str, Any]],
    drifts: List[Dict[str, Any]],
    drift_pct: float,
):
    """``pricing.changed`` for changes above the drift threshold.

    Applied changes carry ``status: applied``, changes held for review
    ``status: pending``. First prices and source-only changes are silent.
    """
    from plugboard.change_events import ChangeBatch

    batch = ChangeBatch()
    for item, status in [(u, "applied") for u in updated] + [
        (d, "pending") for d in drifts
    ]:
        if item.get("old") is None or float(item.get("pct") or 0) <= drift_pct:
            continue
        data = {
            "month": month,
            "model": item["model"],
            "source": item.get("source", ""),
            "old": item["old"],
            "new": item["new"],
            "pct": item.get("pct"),
            "status": status,
        }
        if item.get("space"):
            data["provider"] = item["space"]
        batch.add("pricing.changed", data)
    return batch


def revert_model(month: str, model: str, space: str = "") -> Optional[Dict[str, Any]]:
    """Drop a manual price and give the model back to the automatic sources.

    ``space`` selects a provider price (models_by_provider) instead of the
    vendor price. The new price comes from the sources fetched by the last
    update (kept in memory, empty after a restart), else from the automatic
    price the manual edit replaced, else from the previous month; returns the new entry, or None when nothing prices
    it any more. Raises KeyError when the model has no entry, ValueError when
    the entry is not manual.
    """
    with pricing_lock(month):
        grid = load_pricing(month, strict=True) or {"models": {}}
        if space:
            table = grid.setdefault("models_by_provider", {}).setdefault(space, {})
            cache = _last_space_sources.get(space, {})
        else:
            table = grid.setdefault("models", {})
            cache = _last_sources
        entry = table.get(model)
        if entry is None:
            raise KeyError(model)
        if entry.get("source") != "manual":
            raise ValueError(
                f"{model} is not a manual price (source: {entry.get('source') or 'none'})"
            )
        del table[model]
        order = pricing_merge.source_order(external_configured=bool(_source_url()))
        cached = {
            name: {model: prices[model]}
            for name, prices in cache.items()
            if model in prices
        }
        merged = pricing_merge.merge_sources({}, cached, order, action="apply")
        new = merged.models.get(model)
        if (
            new is not None
            and space
            and _same_as_vendor(model, new, grid.get("models") or {})
        ):
            new = None  # the vendor price applies again
        if new is None and isinstance(entry.get("previous"), dict):
            new = dict(entry["previous"])  # the automatic price the admin replaced
        if new is None:
            prev_month = _previous_month(month)
            prev_grid = load_pricing(prev_month) if prev_month else {}
            prev_table = (
                (prev_grid.get("models_by_provider") or {}).get(space, {})
                if space
                else prev_grid.get("models") or {}
            )
            if model in prev_table:
                new = {k: v for k, v in prev_table[model].items() if k != "source"}
        if new is not None:
            table[model] = new
        if space and not table:
            grid["models_by_provider"].pop(space, None)
        if not grid.get("models_by_provider"):
            grid.pop("models_by_provider", None)
        save_pricing(month, grid)
    return new


def _previous_month(month: str) -> Optional[str]:
    """Return 'YYYY_MM' for the month preceding `month`. None on bad input."""
    try:
        year_s, mon_s = month.split("_")
        y, m = int(year_s), int(mon_s)
    except (ValueError, AttributeError):
        return None
    m -= 1
    if m == 0:
        m = 12
        y -= 1
    return f"{y:04d}_{m:02d}"
