# Clients and keys

Every application that calls Plugboard gets its own key. Keys carry the limits, and usage is recorded per key and per client, month by month.

## Concepts

| Term | Meaning |
|---|---|
| Client ID | A slug for the calling application, for example `billing-service`. Several keys can share one client ID (rotation, environments). |
| Key | A secret of the form `pb_` followed by 64 hex characters. Older keys starting with `lle_` keep working. |
| Label | Free text, for example `production` or `staging`. |
| Organisation | Optional `org_id` grouping several clients for reporting. |

Only a SHA-256 hash of each key is stored, in `data/api_keys.json` (file mode `0600`). The plaintext key is returned once, when it is created.

## Turn client authentication on

Set a non-empty `API_KEY` in `.env` (`make init` does it). From then on every `/v1/*` request must carry `Authorization: Bearer <key>` with a valid client key or the `ADMIN_KEY`; anything else gets `401 Invalid API key`. The value of `API_KEY` is only a switch: it is not accepted as a client key.

With an empty `API_KEY`, the server logs a critical warning every five minutes (unless `PLUGBOARD_DEV=1`) and `/health` reports `"auth_disabled": true`. Even then, chat requests without a key are refused with `401` because tenant isolation is strict by default (see `PLUGBOARD_ALLOW_PERMISSIVE` in the [configuration reference](configuration.md)), and a presented `pb_` key is always validated.

## Create a key

From the dashboard: **API Keys → + New Key**, fill in the client ID, an optional label, the monthly limit and the RPM limit, then **Create Key** and copy the key.

From the admin API:

```bash
curl -s -X POST http://localhost:8765/admin/keys \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{
        "client_id": "billing-service",
        "label": "production",
        "monthly_limit_usd": 25,
        "rpm_limit": 120,
        "allowed_models": ["anthropic-direct:claude-haiku-4-5-20251001", "ollama:qwen3:8b"],
        "org_id": "acme"
      }'
```

When admin MFA is enforced, add `-H "X-Plugboard-Admin-TOTP: <code>"` with a fresh code from the authenticator (each code works once); see [admin API with MFA](monitoring.md#admin-api-with-mfa).

The response contains the record and, once, the plaintext key in `"key"`.

| Field | Default when omitted | Effect |
|---|---|---|
| `monthly_limit_usd` | `PLUGBOARD_DEFAULT_MONTHLY_LIMIT_USD` (10) | Monthly budget in USD for this key. `0` means unlimited. |
| `rpm_limit` | `PLUGBOARD_DEFAULT_RPM_LIMIT` (60) | Requests per minute for this key. `0` means unlimited. |
| `allowed_models` | empty (all models) | List of exact model IDs. A request is allowed when the model that serves it equals one entry, written as the bare model name, as `<provider-id>:<model>` or as the full ID `/v1/models` lists for it. No substring, prefix, wildcard or change of case. Set through the admin API only: the dashboard form has no field for it. See [allowed models](#allowed-models). |
| `org_id` | empty | Organisation used for grouped reporting. |

### Allowed models

Each entry is compared, as a whole string, with the provider and model that serve the request. One model can be listed under three spellings, here the Codex CLI model `gpt-6.1-sol`:

| Spelling | Example |
|---|---|
| bare model name | `gpt-6.1-sol` |
| `<provider-id>:<model>` | `chatgpt-via-codex-cli:gpt-6.1-sol` |
| full ID listed by `/v1/models` | `Codex:gpt-6.1-sol` |

- The provider ID is the one shown on the Providers page. `/v1/models` lists the CLI providers under a display prefix instead (`claude-code:`, `Codex:`, `Gemini:`). An entry equal to the full ID it lists for a model allows that model; the prefix alone (`Codex:`) allows nothing. A provider whose ID is the prefix itself owns that spelling, and `/v1/models` then lists the CLI under its provider ID ([details](../model-access-correction.md#published-model-id-as-a-third-spelling)).
- A bare model name (`qwen3:8b`) allows that name on every provider.
- Clients should send the model with its provider, as `<provider-id>:<model>` or as the ID `/v1/models` lists. A name without a provider prefix can be routed to another provider and refused.
- When a provider lists only dated snapshots of a model (`claude-haiku-4-5-20251001`), a request for the undated name can be served, and is then checked, under the newest dated ID once Plugboard has loaded its model list. List the dated ID and request it as it is.
- On `/v1/chat/completions`, the `detail` of a `403` names the ID that was checked (`Model '<provider-id>:<model>' not allowed ...`) and the key's list. `/v1/embeddings` answers `403 Embedding model not permitted for this key` without them, and `/v1/preflight` gives the closed code `forbidden_model`.

An entry that is only a provider prefix (`"anthropic-direct:"`, `"Codex:"`) or part of a name (`"claude"` for `claude-haiku-...`) has allowed none of the models it used to cover since 4.3.1: their requests get `403`. `GET /admin/keys` shows the list of each key. A list cannot be edited, and rotating a key copies it: create a new key with full IDs, then revoke the old one ([details](../model-access-correction.md)).

## How limits are enforced

Checks run in this order on each chat request:

1. **Global rate limit per IP address**: `RATE_LIMIT` requests per minute (default 60, `0` disables it), for every route. Exceeding it returns `429 Rate limit exceeded`. Behind a reverse proxy every request comes from the proxy's address; see [Deployment](deployment.md#behind-a-reverse-proxy).
2. **Global concurrency cap** (optional): with `PLUGBOARD_MAX_INFLIGHT_CHAT` above 0, extra concurrent chat requests wait up to `PLUGBOARD_INFLIGHT_WAIT_S` seconds, then get `503` with `Retry-After: 5`. A request holds its slot until its answer is sent, or until its client hangs up: the gateway then stops the provider call (the CLI and its children are killed, an HTTP request is cancelled), bills and caches nothing, and frees the slot at once.
3. **Monthly budget**: the key's spend for the current month is summed from the usage records. When it is greater than or equal to the limit the request is refused with `429 Monthly spending limit exceeded`. The check happens before the call, so the last request of a month can take the spend slightly over the limit.
4. **Allowed models**: a model outside the list returns `403`.
5. **Known price** (keys with a monthly budget only): a model without a known price returns `402 Model price unavailable for this key`, before any provider call, streamed or not. Such a call would be recorded at 0 and the budget could never stop it. Every provider the request may fall back to is checked. The refusal is logged (key, provider, model; never the prompt). Keys with `monthly_limit_usd` 0 are not affected.
6. **RPM limit**: a sliding 60-second window per key; exceeding it returns `429 Client rate limit exceeded`.
7. **Session turn** (requests with a `session_id` only): the request waits until the previous turn of its session is answered ([request options](#request-options)). It comes after every check above, so a refused request is answered at once and never waits for it.

Then each provider takes a limited number of requests at once (**provider concurrency cap**): a local one (`sovereign`, a loopback or private address, or an Ollama over plain `http://`) takes one at a time by default, any provider takes at most its `max_concurrency` ([details](providers-and-subscriptions.md#concurrent-requests-per-provider)). A non-streamed request for a model without provider prefix goes to the next candidate when one is full; a streamed request is sent to a single provider and waits for that one only, even when another candidate is free. When every candidate is full, the request waits up to `PLUGBOARD_PROVIDER_WAIT_S` seconds (default 10) for the first freed slot, oldest request first, then gets `503` with `"code": "providers_busy"` and `Retry-After: 5`; nothing was called. A client that hangs up while it waits leaves the queue at once.

Requests made with the `ADMIN_KEY` (or from the dashboard Playground) are not subject to budgets, model restrictions, price checks or RPM limits.

`POST /v1/preflight` runs the same checks for each pair before its ping, inside the same concurrency cap, and accepts at most 10 pairs (`422` beyond). Each ping counts towards the RPM limit and is billed like a chat call. A refused or failed pair answers `"ok": false` with a closed `error` code, never the provider's message: `unknown_model`, `forbidden_model`, `budget_exceeded`, `price_unavailable`, `rate_limited`, `upstream_error` (details stay in the server log) or `providers_busy` (the provider stayed at its concurrency cap; nothing was called).

### Which models have a known price

For a budgeted key, a model counts as priced when the [pricing grid](pricing.md), the official price table or the built-in table gives it a price, 0 included. With an empty grid (a fresh install, or `PRICING_SCHEDULER_DISABLED=1`):

| Class | Price today | Budgeted key |
|---|---|---|
| Ollama local model (not `-cloud` / `:cloud`, any case) on a self-hosted Ollama provider (`base_url` on `localhost`, a loopback or a private IP literal; see [pricing](pricing.md#how-a-model-finds-its-price)) | 0, explicit (`local`) | allowed |
| The same model on an Ollama reached by a DNS name (`host.docker.internal`) or a public host | not free | `402` unless priced (a `manual` 0 in the `ollama` space is a price) |
| Ollama cloud model (`*-cloud`, `*:cloud`) with an `ollama` price, or a vendor price (`gpt-oss:20b-cloud`) | that price | allowed |
| Ollama cloud model whose only match is a built-in local 0 (`gemma4:31b-cloud`), or not priced at all | unknown | `402` |
| A local model name (`llama3`, `qwen3:8b`) on any other provider | not free: a local 0 by name (built-in, or written in a grid by a source) applies to Ollama providers only; an admin's `manual` 0 is a price | `402` unless priced |
| Subscription CLIs (Claude Code, Codex, Gemini, Antigravity) and OAuth/broker providers | API price of the model when known (`claude-haiku-4-5`, `gpt-5.5`) | allowed |
| Official price table (checked 2026-10-24, dated) | Claude Fable 5.1 10/50, Opus 5.5 4/20, Sonnet 5.5 2/10, Haiku 5.5 0.10/0.50; agy `-medium` variants as their base model; `gemini-4-argon-*` 2/10 through November 2026, 4/20 from December 2026; `gemini-3.8-flash-*` 0.75/3.75 through December 2026, 1.50/7.50 from January 2027; `gpt-4o-mini` 0.15/0.60 (USD per million tokens, input/output) | allowed |
| No official price: `gpt-oss-120b-medium` (agy), `meta-llama-3.1-405b-instruct` (GitHub broker), and any model the grid and the table do not price | unknown | `402` |
| Custom provider, model not in the grid | unknown | `402` |

A subscription is not treated as free: Plugboard has no rule that says so, and a budget set on a key is meant to hold. To let a budgeted key use such a model, give it a price in the grid (0 if you decide it costs nothing), or remove the key's monthly limit.

Limits of the official table: prices change by grid month (the price in force on the first day of the month), and there are no price tiers: Haiku 5.5 prompts above 100k tokens (0.50/2.50 for the whole request) are billed at the standard rate. The price refresh writes the table into each month's grid; see [pricing sources](pricing.md#sources).

When `ALERT_WEBHOOK_URL` is set, every billed request from a client at or above 80 % of its monthly limit posts a `spending_alert` (`"status": "warning"`, or `"exceeded"` from 100 %). There is no deduplication, so expect one alert per request in that range. See [Monitoring](monitoring.md#alerts-and-webhooks).

## Revoke, rotate, delete

| Action | Dashboard | API |
|---|---|---|
| Revoke (keep the record, refuse the key) | API Keys → Revoke | `POST /admin/keys/{key_id}/revoke` |
| Rotate (revoke and issue a new key with the same settings) | not in the dashboard | `POST /admin/keys/{key_id}/rotate` |
| Delete | API Keys → Delete | `DELETE /admin/keys/{key_id}` |
| List | API Keys | `GET /admin/keys`, `GET /admin/keys/client/{client_id}` |

## What clients get back

A successful non-streaming response is a standard chat completion plus a `billing` object:

```json
"billing": {
  "prompt_tokens": 12,
  "completion_tokens": 9,
  "total_tokens": 21,
  "cost_usd": 0.0000072,
  "model": "gpt-4o-mini",
  "pricing_month": "2026_10"
}
```

Response headers:

| Header | Meaning |
|---|---|
| `X-Cache` | `MISS`, `HIT`, `SEMANTIC-HIT-MINHASH` or `SEMANTIC-HIT-EMBED` |
| `X-Cost-USD`, `X-Prompt-Tokens`, `X-Completion-Tokens` | Billing of this call (on a cache miss) |
| `X-Request-ID` | Request identifier, also present in the server logs |

Cache hits are not billed again. Streaming responses (`"stream": true`) are server-sent events; Plugboard records their usage when the stream ends and appends a final chunk with `usage` when the provider did not send one.

### Request options

- `"cache": false` in the request body skips the response cache for that call. With an SDK, send it as an extra body field (for example `extra_body={"cache": False}`).
- `"session_id": "<uuid>"` in the body keeps the conversation on the server: Plugboard prepends the stored history, so the client only sends the new turn. The `X-LLM-Session-ID` header does the same. Sessions are isolated per client, expire after `PLUGBOARD_SESSION_TTL_S` (24 hours) and keep `PLUGBOARD_SESSION_MAX_TURNS` turns (50). The turns of one session run one after the other, in arrival order: a turn sent while the previous one is still being answered waits for it and sees it, and a client that hangs up while it waits leaves the queue. Streamed turns are kept too: an HTTP provider's stream once it is complete (an interrupted one is not), a CLI's answer before it is replayed as a stream (kept even if the client hangs up during the replay). Requests without a session never wait.
- `/v2/chat/completions` and `/v2/models` behave like the `/v1` routes and add `X-API-Version: 2`.

### Errors

| Status | Typical cause |
|---|---|
| `400` | Malformed request, or the provider rejected it (context too long, unsupported parameter): the message is written by Plugboard and carries an `error_ref` |
| `401` | Missing, unknown or revoked key |
| `402` | The key has a monthly budget and the model has no known price |
| `403` | Model not in the key's `allowed_models` |
| `413` | Request body larger than `MAX_BODY_BYTES` (10 MB) |
| `429` | IP rate limit, key RPM limit or monthly budget reached, or the quota of the last provider tried exhausted (`Retry-After` when it gives a delay) |
| `502` | The provider returned an error or could not be reached, or every candidate provider failed. A client key never gets the provider's own message or address: the `error_ref` finds it in the gateway log |
| `503` | Server at its concurrency cap, every provider of the model at its own cap (`"code": "providers_busy"`), or a CLI provider timed out (`Retry-After` is set) |

## Self-service for clients

A client can check its own consumption with its key, without admin access:

- **Portal**: <http://localhost:8765/portal>, paste the key, see the month's usage.
- `GET /v1/me`: client ID, key ID, limits, current month usage and remaining budget.
- `GET /v1/me/keys`: the client's keys, masked.
- `GET /usage/me?month=YYYY_MM`: usage entries for a month (current month by default).

## Usage reports for the operator

The **Usage & Billing** page shows the month per client and exports CSV. The same data over the admin API:

| Endpoint | Content |
|---|---|
| `GET /admin/usage/{month}` | All usage for a month (`month` is `YYYY_MM`) |
| `GET /admin/usage/{month}/{client_id}` | One client |
| `GET /admin/usage/{month}/key/{key_id}` | One key |
| `GET /admin/usage/{month}/export?format=csv` | CSV export (`format=json` by default) |
| `GET /admin/orgs`, `GET /admin/orgs/{org_id}/keys`, `GET /admin/orgs/{org_id}/usage/{month}` | Organisation views |

Costs are computed when the request is recorded, with the price of that month's [pricing grid](pricing.md). Changing a price later does not rewrite past usage.
