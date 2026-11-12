# plugboard-sdk

Official Python SDK for [Plugboard](..) — a thin, dependency-light
wrapper over the gateway's standard `/v1/chat/completions` endpoint.

## Install

**SDK status:** version `0.2.0`, beta, shipped in this repository only and not published on PyPI: install it from a checkout with `pip install ./sdk/python`.

```bash
# from the root of a Plugboard checkout
pip install ./sdk/python
# optional async support
pip install './sdk/python[async]'
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
for delta in client.chat_stream("gemini:gemini-2.5-flash", [{"role": "user", "content": "Count to 5"}]):
    print(delta, end="", flush=True)
```

Choose a model ID returned by `list_models()`. The removed OpenClaw connector's
legacy IDs are refused without fallback; see the [manual migration guide](../../docs/guide/openclaw-removal.md).

## Embeddings

```python
reply = client.embeddings("ollama:nomic-embed-text", ["first text", "second text"])
vectors = [item["embedding"] for item in reply["data"]]  # in input order
```

`input` is one string or a list of strings; `encoding_format="base64"` returns
each vector as a base64 string. The key must be allowed the embedding model.
The async client has the same `embeddings()` method.

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

## Typed decisions (sync and async)

Use a client key explicitly permitted for these engines. Decision engines do
not appear in `list_models()` and cannot be used through `chat()`. Supported
IDs are `jev` (TypeSafe API), `laya` (English) and `laya-multilingual` (the locally
hostable Laya family). Decision 72 of 24 October 2026 removes Kev; its old IDs
are rejected in chat without fallback and grant no capability.

```python
models = client.list_decision_models()  # availability, limits, confidence semantics
questions = {
    "route": {
        "type": "choice",
        "instructions": "Choose a route for the parcel.",
        "criteria": {"accept": "Parcel intact", "inspect": "Damage suspected"},
    },
    "intact": {"type": "noul", "instructions": "The parcel is intact."},
    "condition": {
        "type": "score", "instructions": "Rate parcel condition.",
        "criteria": ["damaged", "intact"],
    },
}
reply = client.decide("jev", "A synthetic parcel arrived intact.", questions)
print(reply["answers"]["route"])
```

`noul` is a probability of true; `score` is the expected zero-based ordinal
index. Confidence retains each engine's documented semantics. Consult the
catalogue before choosing an engine: missing credentials/assets or an
unconfigured worker is an unavailable state. These examples do not establish
real inference. Jev without its server-side API key returns 503; it is not a
local model download.

`reply["billing"]` is `{"status": "billed", "cost_usd": <cost>}`: Jev at its
official $0.042 per million input tokens, from the token usage it reports
(output free); Laya free (`0.0`). It is `"estimated"` (the 64,000-token default
context, $0.002688) when Jev reports no valid usage, and
`{"status": "unrecorded", "cost_usd": null}` when the gateway answered but could
not record the call. Each model returned by `list_decision_models()` carries
`pricing` (`input_usd_per_mtok`, `output_usd_per_mtok`, `unit: "token"`). Decisions
count towards the key's monthly limit: `PlugboardError` with status 429 once it
is spent, 503 for an unavailable engine. Decisions never return 402 (that
status is for chat models without a known price on a budgeted key).
The optional Laya runtime is qualified only for macOS arm64 ≥14, not Linux/OVH.
Historical multilingual smoke remains 2/3; the Torch pt2 reserve and final
reviews remain open.

The optional async client uses `httpx`; close it with `async with` or `aclose()`:

```python
from plugboard_sdk import PlugboardAsyncClient

async def run():
    async with PlugboardAsyncClient("pb_your_key") as async_client:
        models = await async_client.list_decision_models()
        return await async_client.decide(
            "jev", "A synthetic parcel arrived intact.", questions
        )
```

## Errors

Every non-2xx response raises `PlugboardError`, and so does a gateway that
cannot be reached. Since 0.2.0 it carries:

| Attribute | Value |
|---|---|
| `status` | HTTP status; `0` when no answer came |
| `detail` | The answer's body (sync client), or a fixed message (async client, decisions) |
| `code` | The gateway's error code when it gave one: `providers_busy`, `model_not_found`, `provider_disabled`...; `timeout` or `connection_failed` when no answer came; else `None` |
| `retry_after` | Seconds to wait before retrying, from the `Retry-After` header (seconds or a date) or the body's `retry_after`; else `None` |

A request with no answer within `timeout` raises `PlugboardTimeoutError`, a
`PlugboardError` (status `0`, code `timeout`) that is also a `TimeoutError`.
Decision calls keep their fixed details, `Decision request timed out` and
`Decision request failed`, and never include the answer's body or raw transport
text; they keep `code` and `retry_after`.

```python
import time

from plugboard_sdk import PlugboardError

try:
    client.chat(model, [{"role": "user", "content": "..."}])
except PlugboardError as e:
    if e.code == "providers_busy":  # 503: every provider of the model is full
        time.sleep(e.retry_after or 5)
    print(e.status, e.code, e.detail)
```

## Tests

From the repository root: `python -m pytest -c sdk/python/pyproject.toml sdk/python/tests`
(the async tests need `httpx` and `pytest-asyncio`).
