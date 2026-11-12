"""Plugboard — official Python SDK.

Thin, dependency-light wrapper over the OpenAI-compatible
`/v1/chat/completions` endpoint exposed by Plugboard. Designed so
existing OpenAI-Python users can swap their SDK with a one-line change.

Quick start:
    from plugboard_sdk import PlugboardClient
    client = PlugboardClient(api_key="pb_xxx", base_url="http://localhost:8765")
    reply = client.chat("gemini:gemini-2.5-flash", [{"role": "user", "content": "hi"}])
    print(reply["choices"][0]["message"]["content"])

The SDK also understands PERF-1 sessions:
    client.chat(model, [{"role":"user","content":"remember: my name is Bob"}], session_id="conv-42")
    client.chat(model, [{"role":"user","content":"what's my name?"}], session_id="conv-42")

And streaming:
    for chunk in client.chat_stream(model, [...]):
        print(chunk, end="", flush=True)

Embeddings, and errors that say when to retry:
    try:
        vectors = client.embeddings("ollama:nomic-embed-text", ["a", "b"])
    except PlugboardError as exc:
        if exc.code == "providers_busy":
            time.sleep(exc.retry_after or 5)
"""

from __future__ import annotations

from .client import (
    LLMEnablerClient,
    LLMEnablerError,
    PlugboardClient,
    PlugboardError,
    PlugboardTimeoutError,
)
from .async_client import PlugboardAsyncClient

__all__ = [
    "PlugboardClient",
    "PlugboardAsyncClient",
    "PlugboardError",
    "PlugboardTimeoutError",
    # Deprecated aliases from before the rename.
    "LLMEnablerClient",
    "LLMEnablerError",
]
__version__ = "0.2.0"
