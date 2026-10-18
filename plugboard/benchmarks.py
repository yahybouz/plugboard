"""
Model benchmarks & capabilities — enriched model metadata for clients.

Provides benchmark scores, capabilities, and metadata for known LLM models.
Data sources: public benchmark leaderboards, provider documentation.

Usage:
    from plugboard.benchmarks import get_model_info, get_all_benchmarks
    info = get_model_info("claude-sonnet-4-6")
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp

log = logging.getLogger(__name__)

# Optional external benchmark JSON source
_BENCHMARK_SOURCE_URL = os.environ.get("BENCHMARK_SOURCE_URL", "").strip()

# ── Model database ──────────────────────────────────────────────────────────
# Scores: MMLU (knowledge), HumanEval (coding), MATH (reasoning),
#          GPQA (graduate-level QA), Arena ELO (Chatbot Arena)
# Capabilities: context_window, vision, function_calling, streaming, json_mode

_MODELS_DB: Dict[str, Dict[str, Any]] = {
    # ── Anthropic Claude ────────────────────────────────────────────────────
    "claude-opus-4-7": {
        "provider": "Anthropic",
        "family": "Claude 4.7",
        "tier": "flagship",
        "release_date": "2026-04",
        "description": "Anthropic's latest flagship Claude model. Maximum intelligence for reasoning, coding, and multi-step complex tasks.",
        "context_window": 1000000,
        "max_output_tokens": 32768,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "tool_use",
            "computer_use",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 94.5,
            "HumanEval": 95.8,
            "MATH": 87.2,
            "GPQA_Diamond": 81.0,
            "Arena_ELO": 1420,
        },
        "best_for": [
            "complex reasoning",
            "advanced coding",
            "deep research",
            "agentic planning",
        ],
        "cost_tier": "very_high",
    },
    "claude-opus-4-6": {
        "provider": "Anthropic",
        "family": "Claude 4.6",
        "tier": "flagship",
        "release_date": "2026-03",
        "description": "Most capable Claude model. Best for complex analysis, long documents, advanced coding, and nuanced reasoning.",
        "context_window": 1000000,
        "max_output_tokens": 32768,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "tool_use",
            "computer_use",
        ],
        "benchmarks": {
            "MMLU": 92.8,
            "HumanEval": 93.5,
            "MATH": 82.1,
            "GPQA_Diamond": 74.9,
            "Arena_ELO": 1380,
        },
        "best_for": [
            "complex reasoning",
            "long-form analysis",
            "code generation",
            "research",
        ],
        "cost_tier": "high",
    },
    "claude-opus-4-5": {
        "provider": "Anthropic",
        "family": "Claude 4.5",
        "tier": "flagship",
        "release_date": "2025-11",
        "description": "Previous flagship Claude. Strong at analysis and creative writing with 1M context.",
        "context_window": 1000000,
        "max_output_tokens": 32768,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "tool_use",
        ],
        "benchmarks": {
            "MMLU": 91.5,
            "HumanEval": 91.2,
            "MATH": 79.8,
            "GPQA_Diamond": 72.1,
            "Arena_ELO": 1355,
        },
        "best_for": ["complex reasoning", "creative writing", "code review"],
        "cost_tier": "high",
    },
    "claude-sonnet-4-6": {
        "provider": "Anthropic",
        "family": "Claude 4.6",
        "tier": "mid",
        "release_date": "2026-03",
        "description": "Best balance of speed, cost, and intelligence. Ideal for most production workloads.",
        "context_window": 200000,
        "max_output_tokens": 16384,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "tool_use",
            "computer_use",
        ],
        "benchmarks": {
            "MMLU": 90.2,
            "HumanEval": 90.8,
            "MATH": 78.5,
            "GPQA_Diamond": 68.4,
            "Arena_ELO": 1340,
        },
        "best_for": ["production APIs", "coding", "data extraction", "summarization"],
        "cost_tier": "medium",
    },
    "claude-sonnet-4-5": {
        "provider": "Anthropic",
        "family": "Claude 4.5",
        "tier": "mid",
        "release_date": "2025-09",
        "description": "Previous generation Sonnet. Solid all-around performance.",
        "context_window": 200000,
        "max_output_tokens": 16384,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "tool_use",
        ],
        "benchmarks": {
            "MMLU": 89.0,
            "HumanEval": 88.5,
            "MATH": 76.2,
            "GPQA_Diamond": 65.0,
            "Arena_ELO": 1310,
        },
        "best_for": ["production APIs", "coding", "content generation"],
        "cost_tier": "medium",
    },
    "claude-sonnet-4-0": {
        "provider": "Anthropic",
        "family": "Claude 4.0",
        "tier": "mid",
        "release_date": "2025-06",
        "description": "First Claude 4 Sonnet. Good coding and reasoning.",
        "context_window": 200000,
        "max_output_tokens": 8192,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "tool_use",
        ],
        "benchmarks": {
            "MMLU": 87.5,
            "HumanEval": 86.0,
            "MATH": 72.0,
            "Arena_ELO": 1285,
        },
        "best_for": ["general purpose", "coding", "analysis"],
        "cost_tier": "medium",
    },
    "claude-haiku-4-5": {
        "provider": "Anthropic",
        "family": "Claude 4.5",
        "tier": "fast",
        "release_date": "2025-10",
        "description": "Fastest Claude model. Best for high-volume, low-latency tasks.",
        "context_window": 200000,
        "max_output_tokens": 8192,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "tool_use",
        ],
        "benchmarks": {
            "MMLU": 84.0,
            "HumanEval": 82.5,
            "MATH": 68.0,
            "Arena_ELO": 1230,
        },
        "best_for": ["classification", "routing", "extraction", "high-volume"],
        "cost_tier": "low",
    },
    "claude-3-haiku": {
        "provider": "Anthropic",
        "family": "Claude 3",
        "tier": "fast",
        "release_date": "2024-03",
        "description": "Legacy fast model. Still useful for simple tasks at very low cost.",
        "context_window": 200000,
        "max_output_tokens": 4096,
        "capabilities": ["vision", "streaming"],
        "benchmarks": {
            "MMLU": 75.2,
            "HumanEval": 75.9,
            "MATH": 38.9,
            "Arena_ELO": 1170,
        },
        "best_for": ["simple classification", "short answers", "legacy workloads"],
        "cost_tier": "very_low",
    },
    # ── OpenAI GPT ──────────────────────────────────────────────────────────
    "gpt-5.5-pro": {
        "provider": "OpenAI",
        "family": "GPT-5.5",
        "tier": "flagship",
        "release_date": "2026-05",
        "description": "OpenAI's top-tier flagship model. Best for highly complex reasoning, advanced coding, and comprehensive multimodal tasks.",
        "context_window": 512000,
        "max_output_tokens": 65536,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "web_browsing",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 95.2,
            "HumanEval": 96.9,
            "MATH": 92.5,
            "GPQA_Diamond": 84.5,
            "Arena_ELO": 1435,
        },
        "best_for": [
            "PhD-level reasoning",
            "advanced software engineering",
            "deep multimodal analysis",
        ],
        "cost_tier": "very_high",
    },
    "gpt-5.5": {
        "provider": "OpenAI",
        "family": "GPT-5.5",
        "tier": "mid",
        "release_date": "2026-05",
        "description": "Balanced flagship GPT-5.5 model. Unprecedented general capability, coding, and reasoning speeds.",
        "context_window": 256000,
        "max_output_tokens": 32768,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "web_browsing",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 93.8,
            "HumanEval": 94.5,
            "MATH": 88.0,
            "GPQA_Diamond": 78.0,
            "Arena_ELO": 1410,
        },
        "best_for": ["general purpose", "coding", "analytical tasks"],
        "cost_tier": "high",
    },
    "gpt-5.5-chat": {
        "provider": "OpenAI",
        "family": "GPT-5.5",
        "tier": "mid",
        "release_date": "2026-05",
        "description": "Chat-optimized flagship GPT-5.5 variant.",
        "context_window": 256000,
        "max_output_tokens": 32768,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "web_browsing",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 93.8,
            "HumanEval": 94.5,
            "MATH": 88.0,
            "GPQA_Diamond": 78.0,
            "Arena_ELO": 1410,
        },
        "best_for": [
            "conversational agents",
            "general reasoning",
            "interactive coding",
        ],
        "cost_tier": "high",
    },
    "gpt-5.5-mini": {
        "provider": "OpenAI",
        "family": "GPT-5.5",
        "tier": "fast",
        "release_date": "2026-05",
        "description": "Compact GPT-5.5 model. Rapid response times with strong intelligence, ideal for high-volume pipelines.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
        ],
        "benchmarks": {
            "MMLU": 88.5,
            "HumanEval": 88.0,
            "MATH": 78.0,
            "Arena_ELO": 1320,
        },
        "best_for": [
            "high-volume classification",
            "low-latency routing",
            "data extraction",
        ],
        "cost_tier": "low",
    },
    "gpt-5.5-nano": {
        "provider": "OpenAI",
        "family": "GPT-5.5",
        "tier": "edge",
        "release_date": "2026-05",
        "description": "Ultra-lightweight, extremely fast and cost-effective edge-tier GPT-5.5 variant.",
        "context_window": 64000,
        "max_output_tokens": 4096,
        "capabilities": [
            "function_calling",
            "streaming",
            "json_mode",
        ],
        "benchmarks": {
            "MMLU": 78.0,
            "HumanEval": 75.0,
            "MATH": 58.0,
            "Arena_ELO": 1220,
        },
        "best_for": [
            "simple text classification",
            "interactive parsing",
            "ultra-low-cost requests",
        ],
        "cost_tier": "very_low",
    },
    "gpt-5-4-pro": {
        "provider": "OpenAI",
        "family": "GPT-5.4",
        "tier": "flagship",
        "release_date": "2026-02",
        "description": "OpenAI's most capable model. Excels at complex multi-step reasoning and code.",
        "context_window": 256000,
        "max_output_tokens": 32768,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "web_browsing",
        ],
        "benchmarks": {
            "MMLU": 93.1,
            "HumanEval": 94.2,
            "MATH": 83.5,
            "GPQA_Diamond": 75.8,
            "Arena_ELO": 1390,
        },
        "best_for": ["complex reasoning", "code generation", "research", "multimodal"],
        "cost_tier": "very_high",
    },
    "gpt-5-4": {
        "provider": "OpenAI",
        "family": "GPT-5.4",
        "tier": "mid",
        "release_date": "2026-02",
        "description": "Strong general-purpose GPT-5.4 variant.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 91.0,
            "HumanEval": 91.5,
            "MATH": 80.0,
            "Arena_ELO": 1360,
        },
        "best_for": ["production APIs", "coding", "analysis"],
        "cost_tier": "high",
    },
    "gpt-5-3-codex": {
        "provider": "OpenAI",
        "family": "GPT-5.3 Codex",
        "tier": "specialized",
        "release_date": "2026-01",
        "description": "Code-specialized GPT. Optimized for software engineering tasks.",
        "context_window": 200000,
        "max_output_tokens": 16384,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 86.0,
            "HumanEval": 95.0,
            "MATH": 75.0,
            "SWE_Bench_Verified": 62.0,
            "Arena_ELO": 1345,
        },
        "best_for": ["code generation", "debugging", "code review", "refactoring"],
        "cost_tier": "medium",
    },
    "gpt-5-2": {
        "provider": "OpenAI",
        "family": "GPT-5.2",
        "tier": "mid",
        "release_date": "2025-10",
        "description": "Balanced GPT-5 model. Good performance at moderate cost.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 89.5,
            "HumanEval": 89.0,
            "MATH": 76.0,
            "Arena_ELO": 1320,
        },
        "best_for": ["general purpose", "content generation", "analysis"],
        "cost_tier": "medium",
    },
    "gpt-5-1": {
        "provider": "OpenAI",
        "family": "GPT-5.1",
        "tier": "mid",
        "release_date": "2025-07",
        "description": "Solid GPT-5 variant for everyday tasks.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 88.0,
            "HumanEval": 87.0,
            "MATH": 73.0,
            "Arena_ELO": 1300,
        },
        "best_for": ["general purpose", "writing", "summarization"],
        "cost_tier": "medium",
    },
    "gpt-5-mini": {
        "provider": "OpenAI",
        "family": "GPT-5",
        "tier": "fast",
        "release_date": "2025-08",
        "description": "Small, fast, cheap GPT-5 for high-volume workloads.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 82.0,
            "HumanEval": 80.5,
            "MATH": 65.0,
            "Arena_ELO": 1220,
        },
        "best_for": ["classification", "extraction", "routing", "high-volume"],
        "cost_tier": "low",
    },
    "gpt-5-nano": {
        "provider": "OpenAI",
        "family": "GPT-5",
        "tier": "edge",
        "release_date": "2025-09",
        "description": "Smallest GPT-5. Ultra-fast, ultra-cheap for simple tasks.",
        "context_window": 32000,
        "max_output_tokens": 4096,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 72.0,
            "HumanEval": 68.0,
            "MATH": 48.0,
            "Arena_ELO": 1140,
        },
        "best_for": ["simple classification", "embedding prep", "edge deployment"],
        "cost_tier": "very_low",
    },
    # ── OpenAI o-series (reasoning) ─────────────────────────────────────────
    "o3": {
        "provider": "OpenAI",
        "family": "o3",
        "tier": "reasoning",
        "release_date": "2025-12",
        "description": "Advanced reasoning model. Chain-of-thought for math, science, and complex logic.",
        "context_window": 200000,
        "max_output_tokens": 100000,
        "capabilities": [
            "function_calling",
            "streaming",
            "json_mode",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 91.0,
            "HumanEval": 90.5,
            "MATH": 92.0,
            "GPQA_Diamond": 82.3,
            "Arena_ELO": 1395,
            "ARC_AGI": 88.0,
        },
        "best_for": ["math", "science", "complex reasoning", "research"],
        "cost_tier": "high",
    },
    "o3-mini": {
        "provider": "OpenAI",
        "family": "o3",
        "tier": "reasoning",
        "release_date": "2025-12",
        "description": "Compact reasoning model. Good math/logic at lower cost.",
        "context_window": 200000,
        "max_output_tokens": 65536,
        "capabilities": [
            "function_calling",
            "streaming",
            "json_mode",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 86.0,
            "HumanEval": 85.0,
            "MATH": 87.0,
            "GPQA_Diamond": 72.0,
            "Arena_ELO": 1310,
        },
        "best_for": ["math", "logic", "structured reasoning"],
        "cost_tier": "low",
    },
    "o4-mini": {
        "provider": "OpenAI",
        "family": "o4",
        "tier": "reasoning",
        "release_date": "2026-03",
        "description": "Latest compact reasoning model. Fast chain-of-thought.",
        "context_window": 200000,
        "max_output_tokens": 100000,
        "capabilities": [
            "function_calling",
            "streaming",
            "json_mode",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 87.5,
            "HumanEval": 87.0,
            "MATH": 89.0,
            "GPQA_Diamond": 74.5,
            "Arena_ELO": 1330,
        },
        "best_for": ["math", "code reasoning", "structured output"],
        "cost_tier": "low",
    },
    # ── Google Gemini ───────────────────────────────────────────────────────
    "gemini-3-1-pro-preview": {
        "provider": "Google",
        "family": "Gemini 3.1",
        "tier": "flagship",
        "release_date": "2026-02",
        "description": "Google's latest flagship. Strong multimodal with 2M context window.",
        "context_window": 2000000,
        "max_output_tokens": 65536,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
            "code_execution",
        ],
        "benchmarks": {
            "MMLU": 91.8,
            "HumanEval": 89.0,
            "MATH": 80.5,
            "GPQA_Diamond": 71.0,
            "Arena_ELO": 1350,
        },
        "best_for": ["multimodal", "long context", "code", "grounding with search"],
        "cost_tier": "medium",
    },
    "gemini-2-5-pro": {
        "provider": "Google",
        "family": "Gemini 2.5",
        "tier": "mid",
        "release_date": "2025-06",
        "description": "Strong Gemini model with thinking capabilities.",
        "context_window": 1000000,
        "max_output_tokens": 16384,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
        ],
        "benchmarks": {
            "MMLU": 89.5,
            "HumanEval": 86.0,
            "MATH": 76.0,
            "Arena_ELO": 1300,
        },
        "best_for": ["analysis", "multimodal", "long documents"],
        "cost_tier": "medium",
    },
    "gemini-2-0-flash": {
        "provider": "Google",
        "family": "Gemini 2.0",
        "tier": "fast",
        "release_date": "2025-02",
        "description": "Ultra-fast Gemini. Best for high-throughput at very low cost.",
        "context_window": 1000000,
        "max_output_tokens": 8192,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 78.0,
            "HumanEval": 74.0,
            "MATH": 58.0,
            "Arena_ELO": 1200,
        },
        "best_for": ["high-volume", "classification", "simple tasks", "real-time"],
        "cost_tier": "very_low",
    },
    # ── Other ───────────────────────────────────────────────────────────────
    "glm-5": {
        "provider": "Zhipu AI",
        "family": "GLM-5",
        "tier": "mid",
        "release_date": "2025-11",
        "description": "Chinese-English bilingual model. Strong on Chinese NLP tasks.",
        "context_window": 128000,
        "max_output_tokens": 8192,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 82.0,
            "HumanEval": 78.0,
            "MATH": 65.0,
        },
        "best_for": ["Chinese NLP", "bilingual tasks", "general purpose"],
        "cost_tier": "low",
    },
    "grok-code-fast-1": {
        "provider": "xAI",
        "family": "Grok",
        "tier": "specialized",
        "release_date": "2025-12",
        "description": "xAI code-specialized model. Fast code completion and generation.",
        "context_window": 128000,
        "max_output_tokens": 8192,
        "capabilities": ["function_calling", "streaming"],
        "benchmarks": {
            "HumanEval": 85.0,
            "MATH": 70.0,
        },
        "best_for": ["code completion", "code generation", "fast prototyping"],
        "cost_tier": "medium",
    },
    # ── Anthropic Claude (additional variants) ──────────────────────────────
    "claude-opus-4-0": {
        "provider": "Anthropic",
        "family": "Claude 4.0",
        "tier": "flagship",
        "release_date": "2025-06",
        "description": "First Claude 4 Opus. Strong reasoning and analysis.",
        "context_window": 200000,
        "max_output_tokens": 16384,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "tool_use",
        ],
        "benchmarks": {
            "MMLU": 89.0,
            "HumanEval": 88.0,
            "MATH": 74.0,
            "Arena_ELO": 1300,
        },
        "best_for": ["complex reasoning", "analysis", "code generation"],
        "cost_tier": "very_high",
    },
    "claude-opus-4-1": {
        "provider": "Anthropic",
        "family": "Claude 4.1",
        "tier": "flagship",
        "release_date": "2025-08",
        "description": "Claude 4.1 Opus. Extended thinking and deep analysis.",
        "context_window": 200000,
        "max_output_tokens": 32768,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "tool_use",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 90.5,
            "HumanEval": 90.0,
            "MATH": 77.0,
            "GPQA_Diamond": 70.0,
            "Arena_ELO": 1330,
        },
        "best_for": ["deep analysis", "research", "complex reasoning"],
        "cost_tier": "very_high",
    },
    # ── OpenAI GPT (additional variants) ────────────────────────────────────
    "gpt-5-pro": {
        "provider": "OpenAI",
        "family": "GPT-5",
        "tier": "flagship",
        "release_date": "2025-06",
        "description": "GPT-5 Pro tier. High capability, high cost.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 90.0,
            "HumanEval": 89.5,
            "MATH": 77.0,
            "Arena_ELO": 1330,
        },
        "best_for": ["complex tasks", "multimodal", "analysis"],
        "cost_tier": "high",
    },
    "gpt-5-2-pro": {
        "provider": "OpenAI",
        "family": "GPT-5.2",
        "tier": "flagship",
        "release_date": "2025-10",
        "description": "GPT-5.2 Pro variant. Enhanced reasoning.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 90.5,
            "HumanEval": 90.0,
            "MATH": 78.0,
            "Arena_ELO": 1340,
        },
        "best_for": ["complex reasoning", "code", "analysis"],
        "cost_tier": "high",
    },
    "gpt-5-codex": {
        "provider": "OpenAI",
        "family": "GPT-5 Codex",
        "tier": "specialized",
        "release_date": "2025-07",
        "description": "GPT-5 code-specialized. Optimized for software engineering.",
        "context_window": 200000,
        "max_output_tokens": 16384,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {"HumanEval": 92.0, "MATH": 73.0, "SWE_Bench_Verified": 55.0},
        "best_for": ["code generation", "debugging", "refactoring"],
        "cost_tier": "medium",
    },
    "gpt-5-1-codex": {
        "provider": "OpenAI",
        "family": "GPT-5.1 Codex",
        "tier": "specialized",
        "release_date": "2025-08",
        "description": "GPT-5.1 code-specialized variant.",
        "context_window": 200000,
        "max_output_tokens": 16384,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {"HumanEval": 93.0, "MATH": 74.0, "SWE_Bench_Verified": 57.0},
        "best_for": ["code generation", "debugging", "code review"],
        "cost_tier": "medium",
    },
    "gpt-5-1-codex-mini": {
        "provider": "OpenAI",
        "family": "GPT-5.1 Codex",
        "tier": "fast",
        "release_date": "2025-08",
        "description": "Compact code model. Fast and cheap for code tasks.",
        "context_window": 128000,
        "max_output_tokens": 8192,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {"HumanEval": 85.0, "MATH": 60.0},
        "best_for": ["code completion", "code review", "high-volume code"],
        "cost_tier": "very_low",
    },
    "gpt-5-1-codex-max": {
        "provider": "OpenAI",
        "family": "GPT-5.1 Codex",
        "tier": "flagship",
        "release_date": "2025-09",
        "description": "Maximum capability Codex. Best for complex multi-file refactors.",
        "context_window": 200000,
        "max_output_tokens": 32768,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {"HumanEval": 95.5, "MATH": 78.0, "SWE_Bench_Verified": 65.0},
        "best_for": ["complex refactoring", "multi-file changes", "architecture"],
        "cost_tier": "high",
    },
    "gpt-5-2-codex": {
        "provider": "OpenAI",
        "family": "GPT-5.2 Codex",
        "tier": "specialized",
        "release_date": "2025-11",
        "description": "GPT-5.2 code-specialized. Strong all-around coding.",
        "context_window": 200000,
        "max_output_tokens": 16384,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {"HumanEval": 94.0, "MATH": 76.0, "SWE_Bench_Verified": 60.0},
        "best_for": ["code generation", "debugging", "code review"],
        "cost_tier": "medium",
    },
    "gpt-5-3-codex-spark": {
        "provider": "OpenAI",
        "family": "GPT-5.3 Codex",
        "tier": "fast",
        "release_date": "2026-01",
        "description": "Lightweight Codex for rapid prototyping and code suggestions.",
        "context_window": 128000,
        "max_output_tokens": 8192,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {"HumanEval": 88.0, "MATH": 65.0},
        "best_for": ["rapid prototyping", "code completion", "suggestions"],
        "cost_tier": "medium",
    },
    # ── OpenAI o-series (additional) ────────────────────────────────────────
    "o1": {
        "provider": "OpenAI",
        "family": "o1",
        "tier": "reasoning",
        "release_date": "2024-09",
        "description": "First reasoning model. Strong math and science with chain-of-thought.",
        "context_window": 200000,
        "max_output_tokens": 100000,
        "capabilities": ["function_calling", "streaming", "extended_thinking"],
        "benchmarks": {
            "MMLU": 90.0,
            "MATH": 94.8,
            "GPQA_Diamond": 78.0,
            "Arena_ELO": 1360,
            "ARC_AGI": 76.0,
        },
        "best_for": ["math", "science", "complex reasoning"],
        "cost_tier": "very_high",
    },
    "o1-pro": {
        "provider": "OpenAI",
        "family": "o1",
        "tier": "reasoning",
        "release_date": "2024-12",
        "description": "o1 Pro mode. More compute for harder problems.",
        "context_window": 200000,
        "max_output_tokens": 100000,
        "capabilities": ["function_calling", "extended_thinking"],
        "benchmarks": {
            "MMLU": 91.5,
            "MATH": 96.0,
            "GPQA_Diamond": 80.0,
            "Arena_ELO": 1375,
        },
        "best_for": ["hardest math", "competition programming", "research"],
        "cost_tier": "very_high",
    },
    "o3-pro": {
        "provider": "OpenAI",
        "family": "o3",
        "tier": "reasoning",
        "release_date": "2026-01",
        "description": "o3 Pro mode. Maximum reasoning compute.",
        "context_window": 200000,
        "max_output_tokens": 100000,
        "capabilities": ["function_calling", "streaming", "extended_thinking"],
        "benchmarks": {
            "MMLU": 93.0,
            "MATH": 96.5,
            "GPQA_Diamond": 85.0,
            "Arena_ELO": 1410,
            "ARC_AGI": 92.0,
        },
        "best_for": ["hardest problems", "research", "PhD-level reasoning"],
        "cost_tier": "very_high",
    },
    "o3-deep-research": {
        "provider": "OpenAI",
        "family": "o3",
        "tier": "reasoning",
        "release_date": "2026-01",
        "description": "o3 variant for deep research. Multi-step web research and synthesis.",
        "context_window": 200000,
        "max_output_tokens": 100000,
        "capabilities": [
            "function_calling",
            "streaming",
            "extended_thinking",
            "web_browsing",
        ],
        "benchmarks": {"MMLU": 91.0, "MATH": 92.0, "GPQA_Diamond": 82.0},
        "best_for": ["deep research", "literature review", "multi-step analysis"],
        "cost_tier": "high",
    },
    "o4-mini-deep-research": {
        "provider": "OpenAI",
        "family": "o4",
        "tier": "reasoning",
        "release_date": "2026-03",
        "description": "o4-mini with deep research capabilities. Cost-effective research.",
        "context_window": 200000,
        "max_output_tokens": 100000,
        "capabilities": [
            "function_calling",
            "streaming",
            "extended_thinking",
            "web_browsing",
        ],
        "benchmarks": {"MMLU": 88.0, "MATH": 89.0, "GPQA_Diamond": 75.0},
        "best_for": ["research", "analysis", "fact-checking"],
        "cost_tier": "medium",
    },
    # ── Google Gemini (additional) ──────────────────────────────────────────
    "gemini-3-pro": {
        "provider": "Google",
        "family": "Gemini 3.0",
        "tier": "mid",
        "release_date": "2026-01",
        "description": "Gemini 3 Pro stable. Production-ready, strong multimodal.",
        "context_window": 1000000,
        "max_output_tokens": 16384,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
            "code_execution",
        ],
        "benchmarks": {
            "MMLU": 89.0,
            "HumanEval": 85.0,
            "MATH": 75.0,
            "Arena_ELO": 1300,
        },
        "best_for": ["general purpose", "multimodal", "grounding", "production"],
        "cost_tier": "low",
    },
    "gemini-3-pro-preview": {
        "provider": "Google",
        "family": "Gemini 3.0",
        "tier": "mid",
        "release_date": "2025-12",
        "description": "Gemini 3 Pro preview. Same as gemini-3-pro (preview channel).",
        "context_window": 1000000,
        "max_output_tokens": 16384,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
        ],
        "benchmarks": {
            "MMLU": 88.0,
            "HumanEval": 84.0,
            "MATH": 74.0,
            "Arena_ELO": 1290,
        },
        "best_for": ["general purpose", "multimodal", "grounding"],
        "cost_tier": "low",
    },
    "gemini-3-1-pro": {
        "provider": "Google",
        "family": "Gemini 3.1",
        "tier": "flagship",
        "release_date": "2026-03",
        "description": "Gemini 3.1 Pro stable. Google's best model, 2M context, production-ready.",
        "context_window": 2000000,
        "max_output_tokens": 65536,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
            "code_execution",
        ],
        "benchmarks": {
            "MMLU": 92.0,
            "HumanEval": 89.5,
            "MATH": 81.0,
            "GPQA_Diamond": 71.5,
            "Arena_ELO": 1355,
        },
        "best_for": ["multimodal", "long context", "code", "production", "grounding"],
        "cost_tier": "medium",
    },
    "gemini-3-1-pro-high": {
        "provider": "Google",
        "family": "Gemini 3.1",
        "tier": "flagship",
        "release_date": "2026-02",
        "description": "Gemini 3.1 Pro High quality mode. Maximum capability.",
        "context_window": 2000000,
        "max_output_tokens": 65536,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
            "code_execution",
        ],
        "benchmarks": {
            "MMLU": 92.0,
            "HumanEval": 90.0,
            "MATH": 81.0,
            "Arena_ELO": 1355,
        },
        "best_for": ["complex analysis", "multimodal", "long documents"],
        "cost_tier": "medium",
    },
    "gemini-3-1-pro-low": {
        "provider": "Google",
        "family": "Gemini 3.1",
        "tier": "fast",
        "release_date": "2026-02",
        "description": "Gemini 3.1 Pro Low latency mode. Fast responses.",
        "context_window": 2000000,
        "max_output_tokens": 16384,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
        ],
        "benchmarks": {
            "MMLU": 89.0,
            "HumanEval": 86.0,
            "MATH": 76.0,
            "Arena_ELO": 1310,
        },
        "best_for": ["fast responses", "high-volume", "real-time"],
        "cost_tier": "medium",
    },
    "gemini-3-1-pro-preview-customtools": {
        "provider": "Google",
        "family": "Gemini 3.1",
        "tier": "specialized",
        "release_date": "2026-02",
        "description": "Gemini 3.1 with enhanced custom tool/function calling.",
        "context_window": 2000000,
        "max_output_tokens": 65536,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
            "code_execution",
            "custom_tools",
        ],
        "benchmarks": {
            "MMLU": 91.8,
            "HumanEval": 89.0,
            "MATH": 80.5,
            "Arena_ELO": 1350,
        },
        "best_for": ["tool use", "agents", "function calling", "custom integrations"],
        "cost_tier": "medium",
    },
    # ── Other ───────────────────────────────────────────────────────────────
    "gpt-5-1-chat-latest": {
        "provider": "OpenAI",
        "family": "GPT-5.1",
        "tier": "mid",
        "release_date": "2025-07",
        "description": "Alias for latest GPT-5.1 chat model.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 88.0,
            "HumanEval": 87.0,
            "MATH": 73.0,
            "Arena_ELO": 1300,
        },
        "best_for": ["general purpose", "writing", "summarization"],
        "cost_tier": "medium",
    },
    "gpt-5-2-chat-latest": {
        "provider": "OpenAI",
        "family": "GPT-5.2",
        "tier": "mid",
        "release_date": "2025-10",
        "description": "Alias for latest GPT-5.2 chat model.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 89.5,
            "HumanEval": 89.0,
            "MATH": 76.0,
            "Arena_ELO": 1320,
        },
        "best_for": ["general purpose", "content generation", "analysis"],
        "cost_tier": "medium",
    },
    # ── OpenAI (new models) ───────────────────────────────────────────────
    "gpt-4o": {
        "provider": "OpenAI",
        "family": "GPT-4o",
        "tier": "mid",
        "release_date": "2024-05",
        "description": "GPT-4o multimodal. Fast, capable, good value.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "audio",
        ],
        "benchmarks": {
            "MMLU": 88.7,
            "HumanEval": 90.2,
            "MATH": 76.6,
            "GPQA_Diamond": 53.6,
            "Arena_ELO": 1285,
        },
        "best_for": ["multimodal", "general purpose", "vision", "audio"],
        "cost_tier": "medium",
    },
    "gpt-5-4-mini": {
        "provider": "OpenAI",
        "family": "GPT-5.4",
        "tier": "fast",
        "release_date": "2026-03",
        "description": "Compact GPT-5.4. Fast and cheap for everyday tasks.",
        "context_window": 128000,
        "max_output_tokens": 16384,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 85.0,
            "HumanEval": 84.0,
            "MATH": 70.0,
            "Arena_ELO": 1250,
        },
        "best_for": ["classification", "extraction", "high-volume", "cost-sensitive"],
        "cost_tier": "low",
    },
    # ── Google Gemini (new flash models) ────────────────────────────────────
    "gemini-2-5-flash": {
        "provider": "Google",
        "family": "Gemini 2.5",
        "tier": "fast",
        "release_date": "2025-08",
        "description": "Gemini 2.5 Flash. Ultra-fast with thinking capabilities.",
        "context_window": 1000000,
        "max_output_tokens": 8192,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
        ],
        "benchmarks": {
            "MMLU": 83.0,
            "HumanEval": 79.0,
            "MATH": 65.0,
            "Arena_ELO": 1225,
        },
        "best_for": ["high-volume", "real-time", "cost-sensitive", "long context"],
        "cost_tier": "very_low",
    },
    "gemini-3-flash-preview": {
        "provider": "Google",
        "family": "Gemini 3.0",
        "tier": "fast",
        "release_date": "2025-12",
        "description": "Gemini 3 Flash. Next-gen fast model.",
        "context_window": 1000000,
        "max_output_tokens": 8192,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "grounding",
        ],
        "benchmarks": {
            "MMLU": 85.0,
            "HumanEval": 82.0,
            "MATH": 68.0,
            "Arena_ELO": 1240,
        },
        "best_for": ["high-volume", "classification", "real-time"],
        "cost_tier": "very_low",
    },
    "gemini-3-1-flash-lite-preview": {
        "provider": "Google",
        "family": "Gemini 3.1",
        "tier": "edge",
        "release_date": "2026-03",
        "description": "Gemini 3.1 Flash Lite. Smallest, fastest, cheapest Gemini.",
        "context_window": 1000000,
        "max_output_tokens": 4096,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {"MMLU": 80.0, "HumanEval": 75.0, "MATH": 58.0},
        "best_for": ["edge deployment", "classification", "ultra-low-cost"],
        "cost_tier": "very_low",
    },
    # ── Other ───────────────────────────────────────────────────────────────
    "gpt-oss-120b-medium": {
        "provider": "Google",
        "family": "GPT-OSS 120B",
        "tier": "mid",
        "release_date": "2025-10",
        "description": "Open-source-style 120B parameter model. Decent all-around.",
        "context_window": 32000,
        "max_output_tokens": 8192,
        "capabilities": ["function_calling", "streaming"],
        "benchmarks": {"MMLU": 78.0, "HumanEval": 72.0, "MATH": 55.0},
        "best_for": ["general purpose", "cost-sensitive workloads"],
        "cost_tier": "low",
    },
    # ── Ollama / Open-Source Local (sovereign) ─────────────────────────────
    "llama3": {
        "provider": "Meta (Ollama)",
        "family": "LLaMA 3",
        "tier": "mid",
        "release_date": "2024-04",
        "description": "Meta LLaMA 3 8B. Fast local inference, good for general tasks.",
        "context_window": 8192,
        "max_output_tokens": 4096,
        "capabilities": ["streaming", "json_mode"],
        "benchmarks": {"MMLU": 68.4, "HumanEval": 62.2, "MATH": 30.0},
        "best_for": ["local inference", "privacy", "general tasks"],
        "cost_tier": "free",
    },
    "llama3-70b": {
        "provider": "Meta (Ollama)",
        "family": "LLaMA 3",
        "tier": "high",
        "release_date": "2024-04",
        "description": "Meta LLaMA 3 70B. Strong reasoning, competitive with cloud models.",
        "context_window": 8192,
        "max_output_tokens": 4096,
        "capabilities": ["streaming", "json_mode"],
        "benchmarks": {"MMLU": 82.0, "HumanEval": 81.7, "MATH": 50.4},
        "best_for": ["complex reasoning", "coding", "local privacy"],
        "cost_tier": "free",
    },
    "llama3-1-8b": {
        "provider": "Meta (Ollama)",
        "family": "LLaMA 3.1",
        "tier": "mid",
        "release_date": "2024-07",
        "description": "Meta LLaMA 3.1 8B. Extended context, tool use support.",
        "context_window": 131072,
        "max_output_tokens": 4096,
        "capabilities": ["streaming", "json_mode", "function_calling"],
        "benchmarks": {"MMLU": 73.0, "HumanEval": 72.6, "MATH": 47.2},
        "best_for": ["local inference", "tool use", "long context"],
        "cost_tier": "free",
    },
    "mistral": {
        "provider": "Mistral AI (Ollama)",
        "family": "Mistral",
        "tier": "mid",
        "release_date": "2023-09",
        "description": "Mistral 7B. Efficient, fast, excellent quality for its size.",
        "context_window": 32768,
        "max_output_tokens": 4096,
        "capabilities": ["streaming", "json_mode"],
        "benchmarks": {"MMLU": 64.2, "HumanEval": 40.2, "MATH": 28.4},
        "best_for": ["fast inference", "chat", "lightweight tasks"],
        "cost_tier": "free",
    },
    "codellama": {
        "provider": "Meta (Ollama)",
        "family": "Code Llama",
        "tier": "mid",
        "release_date": "2023-08",
        "description": "Code Llama 7B. Specialized for code generation and understanding.",
        "context_window": 16384,
        "max_output_tokens": 4096,
        "capabilities": ["streaming"],
        "benchmarks": {"HumanEval": 62.4, "MATH": 22.0},
        "best_for": ["code generation", "code review", "local development"],
        "cost_tier": "free",
    },
    "phi3": {
        "provider": "Microsoft (Ollama)",
        "family": "Phi-3",
        "tier": "fast",
        "release_date": "2024-04",
        "description": "Microsoft Phi-3 Mini 3.8B. Small but surprisingly capable.",
        "context_window": 4096,
        "max_output_tokens": 2048,
        "capabilities": ["streaming", "json_mode"],
        "benchmarks": {"MMLU": 69.0, "HumanEval": 57.0, "MATH": 44.6},
        "best_for": ["edge deployment", "fast inference", "resource-constrained"],
        "cost_tier": "free",
    },
    "gemma2": {
        "provider": "Google (Ollama)",
        "family": "Gemma 2",
        "tier": "mid",
        "release_date": "2024-06",
        "description": "Google Gemma 2 9B. Strong reasoning, good multilingual support.",
        "context_window": 8192,
        "max_output_tokens": 4096,
        "capabilities": ["streaming", "json_mode"],
        "benchmarks": {"MMLU": 71.3, "HumanEval": 54.0, "MATH": 36.0},
        "best_for": ["multilingual", "general tasks", "local inference"],
        "cost_tier": "free",
    },
    "qwen2-5-7b": {
        "provider": "Alibaba (Ollama)",
        "family": "Qwen 2.5",
        "tier": "mid",
        "release_date": "2024-09",
        "description": "Alibaba Qwen 2.5 7B. Strong coding and math, multilingual.",
        "context_window": 131072,
        "max_output_tokens": 8192,
        "capabilities": ["streaming", "json_mode", "function_calling"],
        "benchmarks": {"MMLU": 74.2, "HumanEval": 75.2, "MATH": 55.2},
        "best_for": ["coding", "math", "multilingual", "long context"],
        "cost_tier": "free",
    },
    "deepseek-coder-v2": {
        "provider": "DeepSeek (Ollama)",
        "family": "DeepSeek Coder V2",
        "tier": "mid",
        "release_date": "2024-06",
        "description": "DeepSeek Coder V2 16B. Excellent code generation, MoE architecture.",
        "context_window": 128000,
        "max_output_tokens": 4096,
        "capabilities": ["streaming", "json_mode"],
        "benchmarks": {"HumanEval": 90.2, "MATH": 61.6, "MMLU": 73.8},
        "best_for": ["code generation", "debugging", "math reasoning"],
        "cost_tier": "free",
    },
    "kimi-k2.6": {
        "provider": "Moonshot",
        "family": "Kimi K2.6",
        "tier": "mid",
        "release_date": "2026-04",
        "description": "Moonshot's flagship Kimi K2.6 model. Featuring an expansive context length and bilingual fluency.",
        "context_window": 2000000,
        "max_output_tokens": 16384,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {"MMLU": 89.2, "HumanEval": 88.5, "Arena_ELO": 1335},
        "best_for": [
            "extremely long document analysis",
            "bilingual tasks",
            "conversational reasoning",
        ],
        "cost_tier": "medium",
    },
    "kimi-k2.6-thinking": {
        "provider": "Moonshot",
        "family": "Kimi K2.6",
        "tier": "reasoning",
        "release_date": "2026-04",
        "description": "Kimi K2.6 reasoning-focused variant. Uses internal search and chain-of-thought to resolve highly complex analytical prompts.",
        "context_window": 2000000,
        "max_output_tokens": 32768,
        "capabilities": [
            "function_calling",
            "streaming",
            "json_mode",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 91.5,
            "HumanEval": 91.0,
            "MATH": 89.0,
            "Arena_ELO": 1365,
        },
        "best_for": [
            "complex research",
            "academic translation",
            "extended math/logic reasoning",
        ],
        "cost_tier": "high",
    },
    "deepseek-v3": {
        "provider": "DeepSeek",
        "family": "DeepSeek V3",
        "tier": "flagship",
        "release_date": "2025-12",
        "description": "DeepSeek's third-generation flagship MoE model. Exceptional bilingual capability and cost-efficiency.",
        "context_window": 128000,
        "max_output_tokens": 8192,
        "capabilities": ["function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 91.2,
            "HumanEval": 92.5,
            "MATH": 80.0,
            "Arena_ELO": 1375,
        },
        "best_for": [
            "cost-effective flagship tasks",
            "bilingual chat",
            "general coding",
        ],
        "cost_tier": "low",
    },
    "deepseek-r1": {
        "provider": "DeepSeek",
        "family": "DeepSeek R1",
        "tier": "reasoning",
        "release_date": "2025-01",
        "description": "DeepSeek's premier reasoning model. Employs advanced reinforcement learning and chain-of-thought logic to match top-tier flagships in reasoning.",
        "context_window": 128000,
        "max_output_tokens": 32768,
        "capabilities": [
            "function_calling",
            "streaming",
            "json_mode",
            "extended_thinking",
        ],
        "benchmarks": {
            "MMLU": 92.5,
            "HumanEval": 94.0,
            "MATH": 93.1,
            "GPQA_Diamond": 82.5,
            "Arena_ELO": 1415,
        },
        "best_for": [
            "mathematics and science",
            "PhD-level logic problems",
            "deep code reasoning",
        ],
        "cost_tier": "low",
    },
    "qwen3": {
        "provider": "Alibaba",
        "family": "Qwen 3",
        "tier": "flagship",
        "release_date": "2026-03",
        "description": "Alibaba's third-generation Qwen flagship. Strong multilingual support, math, coding, and vision capabilities.",
        "context_window": 128000,
        "max_output_tokens": 8192,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 92.0,
            "HumanEval": 91.5,
            "MATH": 81.2,
            "Arena_ELO": 1385,
        },
        "best_for": [
            "multilingual applications",
            "vision-language tasks",
            "general coding",
        ],
        "cost_tier": "low",
    },
    "qwen3.5": {
        "provider": "Alibaba",
        "family": "Qwen 3.5",
        "tier": "flagship",
        "release_date": "2026-05",
        "description": "Latest Alibaba Qwen flagship variant. Upgraded reasoning, multilingual, and tool-use capabilities.",
        "context_window": 128000,
        "max_output_tokens": 8192,
        "capabilities": ["vision", "function_calling", "streaming", "json_mode"],
        "benchmarks": {
            "MMLU": 93.5,
            "HumanEval": 93.2,
            "MATH": 85.0,
            "Arena_ELO": 1405,
        },
        "best_for": [
            "advanced multilingual tasks",
            "multimodal processing",
            "complex tool orchestration",
        ],
        "cost_tier": "low",
    },
    "claude-3-7-sonnet": {
        "provider": "Anthropic",
        "family": "Claude 3.7 Sonnet",
        "tier": "mid",
        "release_date": "2025-02",
        "description": "Anthropic's state-of-the-art Claude 3.7 Sonnet model with hybrid intelligence, combining fast responses with extended thinking capabilities.",
        "context_window": 200000,
        "max_output_tokens": 8192,
        "capabilities": [
            "vision",
            "function_calling",
            "streaming",
            "json_mode",
            "extended_thinking",
        ],
        "benchmarks": {"MMLU": 89.0, "HumanEval": 92.0, "Arena_ELO": 1368},
        "best_for": [
            "balanced tasks",
            "coding and debugging",
            "agentic workflows",
            "hybrid reasoning",
        ],
        "cost_tier": "medium",
    },
}


def _normalize_model_key(model_id: str) -> str:
    """Extract canonical model name from full model ID.

    BENCH-1: correctly handles Ollama 2-colon IDs (``provider:name:tag``)
    by returning ``name:tag`` instead of the lone ``tag``.
    """
    # Strip the LEADING provider prefix — but only the first colon, so
    # Ollama `ollama:name:tag` keeps `name:tag`.
    if ":" in model_id:
        _, suffix = model_id.split(":", 1)
    else:
        suffix = model_id
    return suffix


def _candidate_model_keys(model_id: str) -> List[str]:
    """Enumerate plausible lookup keys for a routed model ID.

    Mirrors ``billing._candidate_keys`` — tries the raw suffix, the name
    before an Ollama ``:tag``, the model id after a GitHub ``vendor/``,
    the part after an OpenClaw ``vendor--``, and the ``-latest``-stripped
    forms.
    """
    suffix = _normalize_model_key(model_id)
    candidates = [suffix]
    if ":" in suffix:
        name, _tag = suffix.split(":", 1)
        candidates.append(name)
    if "/" in suffix:
        candidates.append(suffix.split("/", 1)[-1])
    if "--" in suffix:
        candidates.append(suffix.split("--", 1)[-1])
    for c in list(candidates):
        if c.endswith("-latest"):
            candidates.append(c[: -len("-latest")])
    # De-dup preserving order
    seen: set = set()
    out: List[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _match_model(model_id: str) -> Optional[str]:
    """Match a model ID to a key in _MODELS_DB. Returns the key or None."""
    from plugboard.billing import _normalize

    for cand in _candidate_model_keys(model_id):
        # Exact match
        if cand in _MODELS_DB:
            return cand
        # Normalised match (dots→dashes, strip dates)
        norm = _normalize(cand)
        for key in _MODELS_DB:
            if _normalize(key) == norm:
                return key
        # Longest-suffix match
        best: Optional[str] = None
        for key in _MODELS_DB:
            if norm.endswith(_normalize(key)) and (
                best is None or len(key) > len(best)
            ):
                best = key
        if best is not None:
            return best

    return None


def get_model_info(model_id: str) -> Optional[Dict[str, Any]]:
    """Get full benchmark + capability info for a model.

    BENCH-1: when no exact benchmark entry exists we still return a
    derived-info dict (provider / family / context_window estimate)
    so clients never get an empty response. Entries from _MODELS_DB
    always win when present.
    """
    key = _match_model(model_id)
    if key:
        info = dict(_MODELS_DB[key])
        info["model_key"] = key
        info["matched_from"] = model_id
        info["info_source"] = "benchmark_db"
        return info

    # Derived fallback: use model_catalog.classify to extract family/variant,
    # then infer provider from the routed owner prefix.
    from plugboard.model_catalog import classify

    # Derive the `owned_by` hint from the routed prefix if present.
    owned_by = model_id.split(":", 1)[0] if ":" in model_id else ""
    c = classify(model_id, owned_by=owned_by)
    family = c.get("family", "")
    provider_hint = _infer_provider_from_owner(owned_by) or _infer_provider_from_family(
        family
    )

    return {
        "model_key": family or model_id,
        "matched_from": model_id,
        "info_source": "derived",
        "provider": provider_hint or "unknown",
        "family": family or model_id,
        "tier": _infer_tier(family, c.get("variant", "")),
        "context_window": _infer_context_window(provider_hint, family),
        "capabilities": ["streaming"],
        "benchmarks": {},
        "best_for": [],
        "description": (
            f"Community/local model — no curated benchmark yet. "
            f"Variant: {c.get('variant') or 'default'}."
        ),
        "cost_tier": "free" if owned_by == "ollama" else "unknown",
    }


def _infer_provider_from_owner(owned_by: str) -> Optional[str]:
    owner = owned_by.lower()
    if "ollama" in owner:
        return "Ollama"
    if "github" in owner:
        return "GitHub Models"
    if "claude" in owner or "anthropic" in owner:
        return "Anthropic"
    if "gemini" in owner or "google" in owner:
        return "Google"
    if "codex" in owner or "openai" in owner or "chatgpt" in owner:
        return "OpenAI"
    return None


def _infer_provider_from_family(family: str) -> Optional[str]:
    f = family.lower()
    if "claude" in f:
        return "Anthropic"
    if "gemini" in f:
        return "Google"
    if "gpt" in f or "o1" in f or "o3" in f or "o4" in f:
        return "OpenAI"
    if "llama" in f:
        return "Meta"
    if "deepseek" in f:
        return "DeepSeek"
    if "qwen" in f:
        return "Alibaba"
    if "mistral" in f or "codestral" in f or "ministral" in f:
        return "Mistral"
    if "phi" in f or "mai-ds" in f:
        return "Microsoft"
    if "grok" in f:
        return "xAI"
    if "kimi" in f:
        return "Moonshot"
    if "glm" in f:
        return "Zhipu"
    if "minimax" in f:
        return "MiniMax"
    if "nemotron" in f:
        return "NVIDIA"
    if "cohere" in f:
        return "Cohere"
    if "jamba" in f or "ai21" in f:
        return "AI21 Labs"
    if "gemma" in f:
        return "Google"
    return None


def _infer_tier(family: str, variant: str) -> str:
    f = (family + "-" + variant).lower()
    if any(t in f for t in ("nano", "mini", "small", "lite", "3b", "7b", "8b")):
        return "lite"
    if any(t in f for t in ("opus", "large", "ultra", "405b", "671b", "pro")):
        return "flagship"
    return "standard"


def _infer_context_window(provider: Optional[str], family: str) -> Optional[int]:
    p = (provider or "").lower()
    f = family.lower()
    if "claude" in f:
        return 200000
    if "gemini" in f:
        return 1000000
    if "gpt-5" in f or "gpt-4.1" in f or "gpt-4o" in f:
        return 128000
    if "llama-4" in f:
        return 128000
    if "llama-3.3" in f or "llama-3.2" in f or "llama-3.1" in f:
        return 128000
    if "deepseek" in f:
        return 64000
    if "qwen" in f:
        return 128000
    if p == "ollama":
        return 8192  # common default for local quantised models
    return None


def get_all_benchmarks() -> Dict[str, Dict[str, Any]]:
    """Return all known model benchmarks."""
    return {k: dict(v) for k, v in _MODELS_DB.items()}


def enrich_models_list(models: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Enrich a /v1/models response with benchmark data."""
    enriched = []
    for m in models:
        model_id = m.get("id", "")
        entry = dict(m)
        info = get_model_info(model_id)
        if info:
            entry["info"] = {
                "provider": info.get("provider"),
                "family": info.get("family"),
                "tier": info.get("tier"),
                "description": info.get("description"),
                "release_date": info.get("release_date"),
                "context_window": info.get("context_window"),
                "max_output_tokens": info.get("max_output_tokens"),
                "capabilities": info.get("capabilities", []),
                "benchmarks": info.get("benchmarks", {}),
                "cost_tier": info.get("cost_tier"),
                "best_for": info.get("best_for", []),
            }
        enriched.append(entry)
    return enriched


async def fetch_external_benchmarks() -> int:
    """Fetch benchmarks from BENCHMARK_SOURCE_URL and merge into _MODELS_DB."""
    if not _BENCHMARK_SOURCE_URL:
        return 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _BENCHMARK_SOURCE_URL,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("Benchmark source returned %d", resp.status)
                    return 0
                data = await resp.json()
                models = data.get("models", data)
                added = 0
                for key, info in models.items():
                    if isinstance(info, dict):
                        _MODELS_DB[key] = info
                        added += 1
                log.info("Merged %d models from external benchmark source", added)
                return added
    except Exception as exc:
        log.warning("Failed to fetch external benchmarks: %s", exc)
    return 0
