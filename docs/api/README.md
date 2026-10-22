# Integrating with Plugboard

Plugboard speaks the OpenAI chat API: most clients only change their base URL and key. This folder is the contract an integrator can rely on.

| File | What it is |
|---|---|
| [`openapi.json`](openapi.json) | The client API, OpenAPI 3.1: every route a client key calls, with request and response schemas, status codes and headers |
| [`mcp.json`](mcp.json) | The MCP server: its tools with their JSON input schemas, its resources and prompts |

Both files are generated from the code (`make contract`) and checked in CI: a route, a field or an MCP tool that changes without the contract fails the build. A running instance serves the same OpenAPI document, without a key, at `GET /v1/openapi.json`.

Admin routes are not part of this contract. They are described by the admin-only `/openapi.json` and `/docs` of the instance.

## Connect

| What | Value |
|---|---|
| Base URL | `http(s)://<host>:8765/v1` (local install: `http://localhost:8765/v1`) |
| Authentication | `Authorization: Bearer pb_...`, a client key issued by the operator ([Clients and keys](../guide/clients-and-keys.md)) |
| Model ids | `GET /v1/models`; pass an `id` as `model`, for example `anthropic-direct:claude-sonnet-5-5` |

With the OpenAI SDKs:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8765/v1", api_key="pb_your_key")
reply = client.chat.completions.create(
    model="anthropic-direct:claude-sonnet-5-5",
    messages=[{"role": "user", "content": "Hello"}],
    extra_body={"cache": False},  # Plugboard fields go in extra_body
)
print(reply.choices[0].message.content)
```

```javascript
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://localhost:8765/v1", apiKey: "pb_your_key" });
const reply = await client.chat.completions.create({
  model: "anthropic-direct:claude-sonnet-5-5",
  messages: [{ role: "user", content: "Hello" }],
});
```

```bash
curl http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer pb_your_key" -H "Content-Type: application/json" \
  -d '{"model": "anthropic-direct:claude-sonnet-5-5", "messages": [{"role": "user", "content": "Hello"}]}'
```

The repository also ships a small Python client, [`sdk/python`](../../sdk/python/README.md). To generate a typed client in another language, feed `openapi.json` to a generator such as [OpenAPI Generator](https://openapi-generator.tech).

## Routes

| Route | Use |
|---|---|
| `POST /v1/chat/completions` | Chat completion, streamed or not |
| `GET /v1/models` | Models the gateway serves, with `account_compatible` for your key |
| `GET /v1/models/benchmarks`, `GET /v1/models/{model_id}/info` | Model profiles: capabilities, benchmarks, cost tier |
| `POST /v1/preflight` | Check that provider/model pairs answer before a long run |
| `GET /v1/me`, `GET /v1/me/keys`, `GET /usage/me` | Your key: limits, spend this month, usage entries |
| `GET /health` | Liveness, no key needed |
| `/v2/...` | Same as `/v1`, with `api_version` / `X-API-Version: 2` |

Schemas, status codes and headers of each route are in `openapi.json`.

## What Plugboard adds to the OpenAI API

- **`billing`** in every billed answer: tokens, `cost_usd`, the `model` that served the call (after any fallback) and the `pricing_month`. The same figures are in `X-Cost-USD`, `X-Prompt-Tokens` and `X-Completion-Tokens`.
- **`"cache": false`** in the body bypasses the response cache for that call (`X-Cache` says `HIT`, `MISS` or `SEMANTIC-HIT-*`).
- **`"session_id"`** (or the `X-LLM-Session-ID` header) keeps the conversation on the server: send only the new turn.
- **Per-model adjustments**, so one request shape works everywhere: `temperature`, `top_p` and `top_k` are removed for Claude models that reject them (Opus 4.7 and later, Sonnet 5+, Haiku 5+), and `max_tokens` becomes `max_completion_tokens` for OpenAI o-series models.

## Streaming

With `"stream": true` the answer is `text/event-stream`: `data: <chunk>` events, each a `ChatCompletionChunk`, then `data: [DONE]`. A chunk carrying `usage` (with `cost_usd` when Plugboard computed it) always comes **before** `[DONE]`, even when the provider sends none, so clients that stop at `[DONE]` still get tokens and cost. Billing headers are not sent on streamed answers.

## Errors

Errors are JSON with a `detail` field (`ErrorResponse` in the schema):

| Status | Meaning | What to do |
|---|---|---|
| `401` | Missing, unknown or revoked key | Check the key |
| `403` | The key may not use this model | Pick a model where `account_compatible` is true |
| `413` | Body over `MAX_BODY_BYTES` (10 MB) | Send less |
| `429` | IP rate limit, key RPM limit or monthly budget | Back off; `GET /v1/me` shows the budget left |
| `502` | The provider failed | Retry later or use another model |
| `503` | Gateway at capacity, or provider timeout | Retry after `Retry-After` / `retry_after` seconds |

## MCP

The MCP server (stdio, SSE or streamable HTTP) wraps the same API for MCP clients: `chat`, `chat_stream`, `list_models`, `check_usage`, `provider_status`, plus the update-registry tools. [`mcp.json`](mcp.json) gives each tool's input schema; [MCP server](../guide/mcp.md) explains how to install, configure and connect it.

## Versioning

`info.version` in `openapi.json` and `contract_version` in `mcp.json` follow semantic versioning, independently of the Plugboard release:

- **major**: a route, field, tool or status code is removed, renamed or changes meaning;
- **minor**: something is added (optional field, route, tool);
- **patch**: wording only.

Fields not named in the contract may appear in answers (they pass through from the provider): ignore what you do not know. Compare `info.version` of `GET /v1/openapi.json` with the one you integrated against to detect a major change.

## For contributors

```bash
make contract         # regenerate docs/api/openapi.json and docs/api/mcp.json
make contract-check   # what CI runs
```

The contract lives in `plugboard/api_contract.py`. A new public route, or a change to what a route returns, needs that file updated, `make contract`, and a `CONTRACT_VERSION` bump as above. `tests/test_api_contract.py` checks the routes, the committed file and real answers against it; the `mcp-contract` CI job checks `mcp.json`.
