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
"""

from __future__ import annotations

from .client import LLMEnablerClient, LLMEnablerError, PlugboardClient, PlugboardError

__all__ = [
    "PlugboardClient",
    "PlugboardError",
    # Deprecated aliases from before the rename.
    "LLMEnablerClient",
    "LLMEnablerError",
]
__version__ = "0.1.0"
