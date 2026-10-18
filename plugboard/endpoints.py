"""Build OpenAI-compatible endpoint URLs from a provider base_url."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# A base URL that already names the API version as a path segment
# (".../v1", ".../v1beta/openai", ".../v1/solar", ".../paas/v4") takes the
# endpoint directly; any other base (a bare host such as OpenClaw, Ollama or
# vLLM, or an Azure ".../openai" root) gets the conventional "/v1" prefix,
# as before.
_VERSIONED = re.compile(r"/v\d+(?:(?:alpha|beta)\d*)?(?:/|$)")


def openai_url(base_url: str, endpoint: str) -> str:
    """``openai_url("https://api.groq.com/openai/v1", "chat/completions")``."""
    base = (base_url or "").rstrip("/")
    endpoint = endpoint.lstrip("/")
    if _VERSIONED.search(urlsplit(base).path):
        return f"{base}/{endpoint}"
    return f"{base}/v1/{endpoint}"
