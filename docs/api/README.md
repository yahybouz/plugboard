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

Inject the client key into `PLUGBOARD_API_KEY` through your shell or secret manager.
For an interactive Bash session, enter it without echoing it or placing it in shell
history:

```bash
read -r -s -p "Plugboard API key: " PLUGBOARD_API_KEY; printf '\n'
export PLUGBOARD_API_KEY
```

```bash
curl http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer ${PLUGBOARD_API_KEY:?Set PLUGBOARD_API_KEY}" -H "Content-Type: application/json" \
  -d '{"model": "anthropic-direct:claude-sonnet-5-5", "messages": [{"role": "user", "content": "Hello"}]}'
```

The repository also ships a small Python client, [`sdk/python`](../../sdk/python/README.md). To generate a typed client in another language, feed `openapi.json` to a generator such as [OpenAPI Generator](https://openapi-generator.tech).

## Routes

| Route | Use |
|---|---|
| `POST /v1/chat/completions` | Chat completion, streamed or not |
| `GET /v1/models` | Models the gateway serves, with `account_compatible` for your key |
| `GET /v1/decisions/models` | Permitted decision engines, availability, limits and confidence semantics |
| `POST /v1/decisions` | Named typed questions; requires an explicitly permitted client key |
| `GET /v1/models/benchmarks`, `GET /v1/models/{model_id}/info` | Model profiles: capabilities, benchmarks, cost tier |
| `POST /v1/preflight` | Check that provider/model pairs answer before a long run (10 pairs at most; each ping is checked, counted and billed like a chat call; `error` is a closed code) |
| `GET /v1/me`, `GET /v1/me/keys`, `GET /usage/me` | Your key: limits, spend this month, usage entries |
| `GET /health` | Liveness, no key needed |
| `/v2/...` | Same as `/v1`, with `api_version` / `X-API-Version: 2` |

Schemas, status codes and headers of each route are in `openapi.json`.

## Typed decisions

Decision engines use a dedicated API and are absent from the chat catalogue and
Playground. Only client keys with explicit `allowed_decision_models` permissions
can use them; instance and admin keys do not grant access. The exact supported
IDs are `jev` (TypeSafe API), `laya` (English) and `laya-multilingual` (the same
locally hostable Laya family). Kev was removed by decision 72 on 24 October 2026;
legacy Kev IDs are rejected in chat without a generative fallback and confer no
capability. An absent or empty
policy grants no decision models. Use the exact IDs returned by the catalogue.

```json
{
  "model": "jev",
  "state": "A synthetic parcel arrived intact.",
  "questions": {
    "route": {
      "type": "choice",
      "instructions": "Choose the next step.",
      "criteria": {"accept": "Parcel intact", "inspect": "Damage suspected"}
    },
    "intact": {"type": "noul", "instructions": "The parcel is intact."},
    "condition": {
      "type": "score",
      "instructions": "Rate the parcel condition.",
      "criteria": ["damaged", "intact"]
    }
  }
}
```

`choice` criteria are named options mapped to descriptions (or null); `noul`
returns a probability of true; `score` criteria are ordered levels and its
answer is their zero-based expected index. Responses contain `model`, `answers`
keyed by question ID, and `billing`: `{"status": "billed", "cost_usd": <cost>}`
(Jev at its official $0.042 per million input tokens from the usage it
reports; Laya free, 0), `{"status": "estimated", "cost_usd": 0.002688}` when
Jev reports no valid usage (64,000-token default context), or
`{"status": "unrecorded", "cost_usd": null}` when the answer could not be
written to the usage ledger. `GET /v1/decisions/models` lists each engine's
`pricing` per million tokens. A second concurrent decision from one key gets
429, and other keys queue briefly; queue plus engine stay within
`api_timeout_seconds` (see [Typed decisions](../guide/decisions.md)). Consult each engine's catalogue
limits and confidence semantics before comparing confidence values.

Missing credentials, incomplete local assets or an unconfigured worker are
reported as unavailable (503), including Jev without its server-side API key.
This documentation's examples are synthetic requests, not evidence of real model
inference. No inference request downloads weights. The optional ML runtime is
qualified only on macOS arm64 ≥14; Linux/OVH hosting remains unproven. Historical
multilingual smoke remains 2/3 and the Torch pt2 reserve remains open. UI, pentest
and final reviews are not validated by these examples.

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
| `402` | The key has a monthly budget and the model has no known price; nothing was called | Ask the operator to price the model, or use a priced one |
| `403` | The key may not use this model | Pick a model where `account_compatible` is true |
| `413` | Body over `MAX_BODY_BYTES` (10 MB) | Send less |
| `429` | IP rate limit, key RPM limit or monthly budget | Back off; `GET /v1/me` shows the budget left |
| `502` | The provider failed | Retry later or use another model |
| `503` | Gateway at capacity, or provider timeout | Retry after `Retry-After` / `retry_after` seconds |

## MCP

The MCP server (stdio, SSE or streamable HTTP) wraps the same API for MCP clients: `chat`, `chat_stream`, `list_models`, `list_decision_models`, `decide`, `check_usage`, `provider_status`, plus the update-registry tools. [`mcp.json`](mcp.json) gives each tool's input schema; [MCP server](../guide/mcp.md) explains how to install, configure and connect it.

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
