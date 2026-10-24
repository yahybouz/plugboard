# Typed decisions

Decisions use dedicated routes and official decision inference, separate from chat.
The supported IDs are `jev` (the TypeSafe remote API), `laya` (English) and
`laya-multilingual` (the same locally hostable Laya family). Decision 72 of
24 October 2026 removes Kev from the active product; no local Jev download exists.

- `GET /v1/decisions/models`: allowed engines, availability, fixed revisions,
  languages, effective limits and confidence semantics.
- `POST /v1/decisions`: strict named `choice`, `noul` and ordinal `score` questions.
- MCP tools `list_decision_models` and `decide`; Python SDK sync and async equivalents.

Only an active client bearer key is accepted. Admin keys, browser sessions,
legacy environment keys and local auto-login grant no decision access.
Provision a client key using the existing authenticated `POST /admin/keys` route:

```json
{"client_id":"synthetic-decisions","allowed_decision_models":["jev","laya","laya-multilingual"],"rpm_limit":10,"monthly_limit_usd":10}
```

The separate `allowed_decision_models` policy matches exact IDs. Missing/null
means no policy granted; explicit `[]` means explicitly no decisions. Both deny
all engines. Old chat substring policies grant no decisions. Rotation preserves
this policy. No wildcard and no shared MCP admin credential fallback.

Example request (synthetic data only):

```json
{
  "model":"laya-multilingual",
  "state":"Le colis est arrivé cassé. Le paiement est correct.",
  "questions":{
    "department":{"type":"choice","instructions":"Quelle équipe doit traiter ce dossier ?","criteria":{"returns":"Produit cassé, remboursement","billing":"Facturation incorrecte"}},
    "broken":{"type":"noul","instructions":"Le produit est-il cassé ?"},
    "urgency":{"type":"score","instructions":"Quel degré d’urgence ?","criteria":["faible","normal","élevé"]}
  }
}
```

Responses contain exactly the question IDs. Choice probabilities cover exactly
the submitted criteria; `choice` selects a maximum. `noul` is the probability
of true. A score is the expectation of ordinal indices `0..n-1`, with a legend
and probabilities keyed by these indices. All numbers are finite and bounded;
distributions must sum to one. Unknown or nested fields are rejected.

Confidence differs by engine: Laya uses normalized entropy, while Jev uses
chance-corrected top probability for choice and distance to mode for score.
These statistics are not calibrated probabilities of correctness. Noul has no
separate confidence field. Validate thresholds on representative labelled data.

The gateway bounds UTF-8 state at 4000 bytes, questions at 8, instructions and
each option description at 512 bytes, and choices/score levels at 16. Jev score
levels are capped at 10. Engine token budgets are enforced before inference;
the catalogue reports effective conservative budgets (not claimed hardware
capacity). See [local runtime](decisions-runtime.md) for source revisions,
Laya option/head budgets.

For Laya, `head_tokens` (also reported as `question_branch_tokens`) is the
total question head: **192 English, 256 multilingual**, derived from the
bundled configurations whose hashes match the pinned checkpoint manifest.
Instructions share that head with options, separators and the type prefix;
they do not receive the whole head budget. `instruction_tokens: null` indicates
that the remaining allowance depends on the question, checked by the tokenizer.
`timeout_seconds: 30` is the local worker's effective deadline, including
startup, hashing and model loading. `api_timeout_seconds: 45` is the separate
API envelope for queueing, dispatch, cancellation and response handling: time
spent waiting in the queue is deducted from the engine's allowance, so queue
plus engine never exceed 45 seconds (504 beyond). It does not grant the worker
an additional 15 seconds of inference.

Jev always calls the fixed official TLS endpoint
`https://api.typesafe.ai/v1/systemone`, with redirects refused. Configure
`PLUGBOARD_JEV_API_KEY` deliberately on the server. Missing credentials/assets
return 503; invalid input 422; permission failure 403; per-key RPM, spending
or capacity 429; invalid upstream output 502; timeout 504. The gateway returns
static bounded errors without submitted text, upstream bodies or credentials.

Every successful decision is recorded in the usage ledger against the key and
client, as model `decision:<id>`. A Jev call that TypeSafe may have billed is
recorded too, and the client still gets the error: an answer (HTTP 200) that is
rejected (502) bills the usage it reports, else the 64,000-token ceiling; a
timeout after the request was sent (504) bills the ceiling, except when the
API deadline (45 seconds, queue wait included) ends the call first: that 504 is
not recorded (at most the ceiling, about $0.0027, per occurrence). A connection error,
an error status from TypeSafe and any failed Laya call are not recorded. The
key's monthly USD limit therefore counts decisions, with the chat semantics:
checked before the call, refused (429) once spend reaches the limit.

If a usage write fails, the process stops serving decisions to keys with a
monthly limit (503 `Decision usage recording unavailable`, no engine call) until
a write succeeds: any decision recorded for another key closes the breaker, and
every 60 seconds one budgeted call is let through as the probe. Keys without a
limit are served as before (`unrecorded`). Each gateway process has its own
breaker.

Decisions are priced through the same monthly [pricing grid](pricing.md) as
chat calls, under the names `jev`, `laya` and `laya-multilingual`: the grid
entry of the current month wins (an admin's `manual` price included), else the
dated [official price table](pricing.md#sources) for that month, else the
prices below. The price refresh writes the official prices into each grid.
`GET /v1/decisions/models` lists the price actually applied in each engine's
`pricing` (`{"input_usd_per_mtok", "output_usd_per_mtok", "unit": "token"}`).
Their grid entries do not make them chat models. Official prices (checked
2026-10-24):

| Engine | Price | `billing` in the answer | Ledger tokens / `price_source` |
| --- | --- | --- | --- |
| Jev | official: $0.042 per million input tokens, output free ([docs.typesafe.ai/models](https://docs.typesafe.ai/models#current-models), checked 2026-10-24) | `{"status":"billed","cost_usd":<input_tokens × 0.042 / 10⁶>}` | `usage.input_tokens` / `usage.output_tokens` reported by Jev; `official:<url>` |
| Jev | usage missing or invalid (absent, not an integer, negative, above 2²⁰ = 1,048,576) | `{"status":"estimated","cost_usd":0.002688}`: 64,000 tokens, Jev's default request context | 64,000 / `official-estimate:<url>` |
| Laya | free (runs locally) | `{"status":"billed","cost_usd":0.0}` | 0 / `builtin:laya-free` |
| any | the engine answered but the ledger write failed | `{"status":"unrecorded","cost_usd":null}`, answers returned, error logged without the request; budgeted keys then get 503 until a write succeeds | not recorded |

With a grid price other than `official` (for example `manual`), the cost uses
that price and `price_source` is `grid:<source>:<YYYY_MM>`
(`grid-estimate:<source>:<YYYY_MM>` for Jev's 64,000-token estimate). An
invalid grid entry is ignored with a warning and the official price applies.

`PLUGBOARD_JEV_PRICE_PER_CALL_USD` is gone: if still set, it is ignored with one
warning per process (its value is never logged). Jev is no longer refused with
402; without `PLUGBOARD_JEV_API_KEY` it returns 503.

The gateway runs one decision at a time per process. A key may have one
decision in flight; a second one from the same key gets 429 at once. Other keys
wait their turn in arrival order: at most 8 waiting, each for at most 15
seconds, then 429. A cancelled or failed request frees its place. Local workers
also hold an interprocess lock, admitting one model per asset directory.

Decision engines and their official variants are rejected on chat routes
before provider fallback, and filtered from chat model catalogues/Playground.
No dashboard is added.

Registered provider IDs are routing owners, not model identities: providers
named `laya` or `jev` can serve `gpt-5` normally. The longest registered
provider prefix owns the request and remains unchanged during model resolution.
For a request without that owner, a local promotion from native catalogue IDs
happens before display aliases. Decision identity is checked on the native
model, after alias resolution and after the provider codec; a display alias or
transport conversion must not hide a decision model.

Recognized direct transport aliases include Ollama and the CLI aliases.
OpenClaw connector IDs and wrappers are removed and refused without fallback;
they do not enable a decision or chat transport. See [migration](openclaw-removal.md).
Tags after `:` and revisions after `@` remain opaque model metadata, never new
routing owners or model names: `qwen3:laya` is ordinary chat. Unknown prefixes
remain opaque too: `unknown:jev` means model `unknown` with tag `jev`, not an
implicit provider. Registered providers named `Codex`, `Gemini` or `claude-code`
take precedence over display/transport aliases. An explicit owner is never
replaced by another provider's catalogue or alias cache.

The final selected model is checked separately, including defaults. Discovery
uses GET catalogues; its optional chat fallback skips decision defaults. Direct
provider ping tests select an ordinary chat model from the catalogue or report
`completion_skipped: decision_model`. Completion-based monitoring/watchdog and
native connectivity probes refuse decisions without sending inference. Removed
Kev identifiers remain rejection tombstones in chat guards solely to prevent
legacy calls from falling through to a generative provider; they grant no
catalogue, permission, download or runtime capability.

## Historical evidence and remaining limits

The automated suite uses synthetic HTTP/engine ports and subprocesses. The
following pre-decision-72 evidence is historical and does not validate this
revised candidate. Separately,
the parent ran native local inference and real gateway integration on downloaded,
hash-verified assets: six HTTP and three MCP decision calls returned 200
(`*-native-proof.json`, `real-api-mcp-v1.json` in the mission evidence directory).
The parent's subsequent isolated v2 run (`real-api-mcp-v2-isolated.json`) also
records six HTTP, three MCP and two SDK real calls, with Ollama boot probes
disabled. These are parent-run evidence, separate from producer mock tests.
The unchanged smoke script passed 3/3 fixtures for English Laya and the now-removed Kev, and
**2/3 for multilingual Laya: its negative fixture genuinely failed**. Fixtures
and thresholds are preserved. These observations do not establish French
quality or general security, and the parent owns the final reviews. Jev has no
configured key and remains 503; there is no real Jev validation.
The optional ML lock is qualified only for Python 3.12 / macOS arm64 ≥14;
Linux/OVH compatibility remains unproven. The Torch pt2 deserialization reserve
remains open. UI, pentest, SPEC/security reviews and publication are not validated
by these historical proofs. No new inference was run for the F1–F3 correction.

Primary references: [TypeSafe API](https://docs.typesafe.ai/api/system-one),
[score semantics](https://docs.typesafe.ai/primitives/score),
[confidence](https://docs.typesafe.ai/confidence),
[Laya official source](https://github.com/NandhaKishorM/laya).
