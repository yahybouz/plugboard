# plugboard-sdk

Official Python SDK for [Plugboard](..) — a thin, dependency-light
wrapper over the OpenAI-compatible `/v1/chat/completions` endpoint.

## Install

```bash
pip install plugboard-sdk
# optional async support
pip install plugboard-sdk[async]
```

## Quick start

```python
from plugboard_sdk import PlugboardClient

client = PlugboardClient(
    api_key="pb_xxxxxxxxxxxxxxxx",
    base_url="http://localhost:8765",
)

reply = client.chat(
    model="gemini:gemini-2.5-flash",
    messages=[{"role": "user", "content": "Hi!"}],
)
print(reply["choices"][0]["message"]["content"])
```

## Streaming

```python
for delta in client.chat_stream("openclaw/main", [{"role": "user", "content": "Count to 5"}]):
    print(delta, end="", flush=True)
```

## Sessions (PERF-1 — server keeps history)

Pass the same `session_id` on consecutive calls and Plugboard will
remember the conversation for you. Only the new turn needs to be sent:

```python
client.chat(model, [{"role": "user", "content": "Remember: my name is Bob."}], session_id="conv-42")
reply = client.chat(model, [{"role": "user", "content": "What's my name?"}], session_id="conv-42")
# → "Bob."
```

Sessions are stored per-client (your API key owns them), capped at 50
turns, and expire after 24 h by default.

## Errors

All non-2xx responses raise `PlugboardError`:

```python
from plugboard_sdk import PlugboardError

try:
    client.chat(model, [{"role": "user", "content": "..."}])
except PlugboardError as e:
    print(e.status, e.detail)
```
