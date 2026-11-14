# Integrating with Plugboard

Plugboard speaks the OpenAI chat API: most clients only change their base URL and key. This folder is the contract an integrator can rely on.

| File | What it is |
|---|---|
| [`openapi.json`](openapi.json) | The client API, OpenAPI 3.1: every route a client key calls, with request and response schemas, status codes and headers |
| [`mcp.json`](mcp.json) | The MCP server: its tools with their JSON input schemas, its resources and prompts |

Both files are generated from the code (`make contract`) and checked by the local CI (`ci/pipeline.json`; there is no remote CI, nothing runs when a branch is pushed): a route, a field or an MCP tool that changes without the contract fails the run. A running instance serves the same OpenAPI document, without a key, at `GET /v1/openapi.json`.

Admin routes are not part of this contract. They are described by the admin-only `/openapi.json` and `/docs` of the instance.

## Connect

| What | Value |
|---|---|
| Base URL | `http(s)://<host>:8765/v1` (local install: `http://localhost:8765/v1`) |
| Authentication | `Authorization: Bearer pb_...`, a client key issued by the operator ([Clients and keys](../guide/clients-and-keys.md)) |
| Model ids | `GET /v1/models`; pass an `id` as `model`, for example `claude-code:claude-sonnet-5-5` |

With the OpenAI SDKs:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8765/v1", api_key="pb_your_key")
reply = client.chat.completions.create(
    model="claude-code:claude-sonnet-5-5",
    messages=[{"role": "user", "content": "Hello"}],
    extra_body={"cache": False},  # Plugboard fields go in extra_body
)
print(reply.choices[0].message.content)
```

```javascript
import OpenAI from "openai";

const client = new OpenAI({ baseURL: "http://localhost:8765/v1", apiKey: "pb_your_key" });
const reply = await client.chat.completions.create({
  model: "claude-code:claude-sonnet-5-5",
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
  -d '{"model": "claude-code:claude-sonnet-5-5", "messages": [{"role": "user", "content": "Hello"}]}'
```

The repository also ships a small Python client, [`sdk/python`](../../sdk/python/README.md).

**SDK status:** version `0.2.0`, beta, shipped in this repository only and not published on PyPI: install it from a checkout with `pip install ./sdk/python`.

To generate a typed client in another language, feed `openapi.json` to a generator such as [OpenAPI Generator](https://openapi-generator.tech).

## Routes

| Route | Use |
|---|---|
| `POST /v1/chat/completions` | Chat completion, streamed or not |
| `GET /v1/models` | Models the gateway serves, with `account_compatible` for your key; a key limited by `allowed_models` sees only the models it may call |
| `GET /v1/decisions/models` | Permitted decision engines, availability, limits and confidence semantics |
| `POST /v1/decisions` | Named typed questions; requires an explicitly permitted client key |
| `GET /v1/models/benchmarks`, `GET /v1/models/{model_id}/info` | Model profiles: capabilities, benchmarks, cost tier. A key limited by `allowed_models` gets only the models it may call; for any other model, `/info` answers as for an unknown model (`info_source: "derived"`) |
| `POST /v1/preflight` | Check that provider/model pairs answer before a long run (10 pairs at most; each ping is checked, counted and billed like a chat call; `error` is a closed code) |
| `GET /v1/me`, `GET /v1/me/keys`, `GET /usage/me` | Your key: limits, spend this month, usage entries |
| `GET /health` | Liveness, no key needed: `ok` and `version` only, always `200` |
| `GET /health/ready` | Readiness, no key needed: `200` when the instance can serve, `503` with the same body when not (`ready`, `version`, generic `checks`) |
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
- **`"session_id"`** (or the `X-LLM-Session-ID` header) keeps the conversation on the server: send only the new turn. The turns of one session run one after the other, streamed turns included; a turn whose previous one is still being answered when its wait ends (10 s by default, shared with the other waits of the request) gets `503` with `"code": "session_busy"` and `Retry-After`.
- **Per-model adjustments**, so one request shape works everywhere: `temperature`, `top_p` and `top_k` are removed for Claude models that reject them (Opus 4.7 and later, Sonnet 5+, Haiku 5+), and `max_tokens` becomes `max_completion_tokens` for OpenAI o-series models.

## Streaming

With `"stream": true` the answer is `text/event-stream`: `data: <chunk>` events, each a `ChatCompletionChunk`, then `data: [DONE]`. A chunk carrying `usage` (with `cost_usd` when Plugboard computed it) always comes **before** `[DONE]`, even when the provider sends none, so clients that stop at `[DONE]` still get tokens and cost. Billing headers are not sent on streamed answers.

For a model without provider prefix, a provider that fails before the first chunk (an error status, a timeout, a dropped connection, a full or turned-off provider) hands the request over to the next candidate, as for a non-streamed request: the client sees only the stream that worked, or the last error. Once the first chunk is out, nothing is replayed: if the provider breaks off, the stream ends with an error event in OpenAI's shape, then `data: [DONE]`:

```
data: {"error": {"message": "The provider stopped the answer before its end: what came before this event is all there is.", "type": "server_error", "code": "stream_interrupted"}}
```

What was received is billed; a session turn ended this way is not recorded.

## Errors

Errors are JSON with a `detail` field (`ErrorResponse` in the schema):

| Status | Meaning | What to do |
|---|---|---|
| `400` | Chat: the body is not valid JSON or not a JSON object, the model is not a string or was removed, `messages` is missing or invalid, `session_id` is a list or an object, a decision model was sent, or the provider rejected the request (context too long, parameters; `error_ref`); nothing was billed | Fix the request; `detail` says what is wrong |
| `400` | Chat only, `code` is `unsupported_on_cli`: only CLI subscription routes serve the model and the request asks for tools, a structured `response_format` or a non-text content part, listed in `unsupported`; nothing was called ([details](../guide/providers-and-subscriptions.md#what-a-cli-subscription-route-does-not-do)) | Send plain text, or pick a model an API or Ollama provider serves |
| `400` | Chat only, `code` is `model_not_supported_by_subscription`: the account plan of a CLI subscription route does not serve the model (codex with a ChatGPT account); no other provider was tried | Pick another model from `GET /v1/models`, or use a route with an API key |
| `400` | Chat only, `code` is `context_length_exceeded`: a CLI subscription route found the prompt too long for the model's context; no other provider was tried | Shorten the messages or pick a model with a larger context |
| `401` | Missing, unknown or revoked key | Check the key |
| `401`, `403` | Chat: the provider's own credentials (a CLI login, a Copilot licence) could not be used; `error_ref` | Tell the operator the `error_ref` |
| `402` | The key has a monthly budget and the model has no known price; nothing was called | Ask the operator to price the model, or use a priced one |
| `403` | The key may not use this model | Pick a model where `account_compatible` is true |
| `404` | Chat only, `code` is `model_not_found`: no provider knows the model, so nothing was called (below) | Send an `id` from `GET /v1/models` |
| `413` | Body over `MAX_BODY_BYTES` (10 MB) | Send less |
| `429` | IP rate limit, key RPM limit or monthly budget, also when the call's estimated cost does not fit in what is left once the key's calls in progress are counted (nothing was called) | Back off, or send a smaller `max_tokens`; `GET /v1/me` shows the budget left (calls in progress not counted) |
| `429` | The last provider tried has exhausted its quota or rate limit (a streamed request is offered to the next provider only before its first chunk); `detail` names the provider | Retry after `Retry-After` / `retry_after` seconds when set (at most 7 days), else later or with another model |
| `502` | The provider failed; a client key gets a sentence and an `error_ref`, never the provider's or the gateway's error text | Retry later or use another model; give the operator the `error_ref` |
| `503` | Gateway at capacity (`code` `server_busy`: the cap of concurrent chat requests stayed full for the whole wait), an HTTP provider's timeout (`Retry-After: 10`, `error_ref`), with `code` `session_busy` the previous turn of the request's session was still being answered when its wait ended, or too many of its turns were already waiting, or, with `code` `providers_busy`, every provider of the model busy at its concurrency cap after the wait, or with too many requests already waiting for it; nothing was called | Retry after `Retry-After` / `retry_after` seconds (5 for `server_busy`, `session_busy` and `providers_busy`) |
| `503` | `code` is `usage_unavailable`: the key has a monthly budget and usage cannot be recorded right now; nothing was called | Retry after `Retry-After` / `retry_after` seconds (30) |
| `503` | Chat only, `code` is `provider_disabled`: the model belongs to a provider the operator turned off; nothing was called and no other provider stands in (below) | Pick another model from `GET /v1/models`, or ask the operator to turn the provider back on |

### `404 model_not_found`

`POST /v1/chat/completions` and `/v2/chat/completions`, streamed or not, answer `404` without calling any provider when no provider knows the model:

- no registered provider ID prefixes it (`<provider-id>:`, inactive providers included), and no display alias (`claude-code:`, `Codex:`, `Gemini:`) or older CLI prefix (`claude-cli:`, `codex-cli:`, `gemini-cli:`) picks an active provider;
- no provider, active or not, lists it as a model, an extra model ID, its default model or a family alias: `/v1/models` gives a family whose listed IDs are all dated snapshots its bare name in `aliases`, so `claude-sonnet-4-5` counts as listed when a provider lists only `claude-sonnet-4-5-20250929`. The comparison ignores case, surrounding spaces and a `:latest` tag, as Ollama does, so `GPT-OSS:20B-Cloud` and `llama3` count as listed when `gpt-oss:20b-cloud` and `llama3:latest` are. A vendor spelling of a model a CLI provider lists counts too (`openai:gpt-6.1-sol` for the Codex CLI's `gpt-6.1-sol`);
- every active provider has a model list. A provider without one (a custom endpoint never discovered) may serve any name: the request is then sent to the providers as before, and a model none of them serves still ends in `502`.

```json
{
  "detail": "Model 'gpt-5-turbo' not found: no provider ID prefixes it and no provider lists it.",
  "code": "model_not_found",
  "hint": "GET /v1/models lists the model IDs this gateway serves: send one as written there, <provider-id>:<model>."
}
```

`code` is stable, test it rather than the text. `detail` echoes the model you sent, cut after 100 characters. A key limited by `allowed_models` gets `403` for a model outside its list, whether a provider knows it or not, so the `404` only tells it about models it may use. A budgeted key gets this `404` rather than a `402` for a missing price. A model with a known provider prefix (`<provider-id>:no-such-model`) is still sent to that provider, which answers for it (`502` when it refuses).

`POST /v1/preflight` applies the same rule to each pair, read as `<provider>:<model>`: such a pair answers `"ok": false` with `"error": "model_not_found"`, and is neither pinged nor billed. As on chat, a limited key gets `forbidden_model` first, and a budgeted key gets `model_not_found` rather than `price_unavailable`.

### `503 provider_disabled`

When the operator turns a provider off (the **Ollama** page switch, or **Active** unticked on the **Providers** page), its models leave `GET /v1/models` at once, without a restart. A chat request for one of them, `/v1` or `/v2`, streamed or not, answers `503` and calls nobody: neither that provider nor another one in its place. The model is one of the provider's when its ID prefixes it (`ollama:qwen3:8b`), or when no prefix matches, every active provider has a model list, none knows the name and the disabled provider does (`qwen3:8b`, `llama3`, or a display alias such as `Codex:gpt-6.1-sol` of a CLI provider turned off). A display alias that selects an active provider still goes to it.

```json
{
  "detail": "Provider 'ollama' is turned off by the administrator; nothing was called.",
  "code": "provider_disabled",
  "hint": "Pick another model from GET /v1/models, or ask the administrator to turn the provider back on."
}
```

There is no `Retry-After`: nobody knows when the provider comes back. A key limited by `allowed_models` gets `403` first for a model outside its list. No cached answer is served for such a model. A request already running when the provider is turned off goes to its end; one still waiting for a slot on it gets this `503` when its turn comes, or when its wait (`PLUGBOARD_PROVIDER_WAIT_S`) ends. `POST /v1/preflight` answers such a pair `"ok": false` with `"error": "provider_disabled"`, without a ping. Embeddings keep their own answer: `503` *Embedding provider unavailable*.

## MCP

The MCP server (stdio, SSE or streamable HTTP) wraps the same API for MCP clients: `chat`, `chat_stream`, `list_models`, `embed`, `list_embedding_models`, `list_decision_models`, `decide`, `check_usage`, `inflight_requests`, `provider_status`, plus the update-registry tools and, over stdio only and off by default, the admin tools `add_provider` and `refresh_catalog`. [`mcp.json`](mcp.json) gives each tool's input schema; [MCP server](../guide/mcp.md) explains how to install, configure and connect it.

## Versioning

`info.version` in `openapi.json` and `contract_version` in `mcp.json` follow semantic versioning, independently of the Plugboard release:

- **major**: a route, field, tool or status code is removed, renamed or changes meaning;
- **minor**: something is added (optional field, route, tool);
- **patch**: wording only.

Fields not named in the contract may appear in answers (they pass through from the provider): ignore what you do not know. Compare `info.version` of `GET /v1/openapi.json` with the one you integrated against to detect a major change.

## For contributors

```bash
make contract         # regenerate docs/api/openapi.json and docs/api/mcp.json
make contract-check   # fail when docs/api/ no longer matches the code
```

The contract lives in `plugboard/api_contract.py`. A new public route, or a change to what a route returns, needs that file updated, `make contract`, and a `CONTRACT_VERSION` bump as above. `tests/test_api_contract.py` checks the routes, the committed file and real answers against it; the `mcp-contract` step of the local CI (`ci/pipeline.json`, run with `make ci`) checks `mcp.json`.
