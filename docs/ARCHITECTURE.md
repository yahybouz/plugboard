# Architecture

Plugboard is a single FastAPI process. It sits between client applications and LLM backends, authenticates and meters every call, and keeps all state in local files.

```
 Client apps, SDKs, MCP clients
        │  POST /v1/chat/completions   Authorization: Bearer pb_...
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Plugboard (FastAPI, default port 8765)                              │
│                                                                     │
│  middleware: security headers · body size limit · request ID ·      │
│              per-IP rate limit                                      │
│                                                                     │
│  app.py        auth (client keys, admin key/session, auto-login)    │
│                budget / allowed-models / RPM checks · cache lookup  │
│                billing · usage recording · admin API · dashboard    │
│                                                                     │
│  router.py     model ID → provider(s) · retries · fallback ·        │
│                load-balancing score · circuit breaker · model list  │
│                                                                     │
│  background    pricing scheduler · model availability scheduler ·   │
│  tasks         provider watchdog · startup probes                   │
└───────┬───────────────┬──────────────────┬──────────────────────────┘
        │ HTTP          │ HTTP             │ subprocess
        ▼               ▼                  ▼
  Cloud APIs      Ollama / custom      claude · codex · agy CLIs
  (API keys,      standard chat API    (the operator's own sign-in)
  CLI tokens)     servers
```

## Request flow

A `POST /v1/chat/completions` goes through these steps:

1. **Middleware**: per-IP rate limit (`RATE_LIMIT`), body size limit, request ID, security headers.
2. **Client resolution** (`_resolve_client` in `app.py`): an admin session cookie, the `ADMIN_KEY`, or a client key looked up by its SHA-256 hash in `data/api_keys.json`. In strict tenant mode (the default) a chat request without a client is refused. It comes before the cap, so a call without a valid key never takes a slot.
3. **Concurrency cap** (`PLUGBOARD_MAX_INFLIGHT_CHAT`, 8 chats by default, a 10 s wait for a slot, then `503 server_busy`). The request's wait budget starts here (`request_wait`): its waits for a slot, for its session's turn and for a provider add up to 10 s at most by default. The slot is given back while the request waits for its session's turn or for a provider (`request_wait.release_slot`), and taken back right before a provider is called (`request_wait.slot_back`), ahead of the requests that hold no provider's slot (`request_wait.ChatCap`).
4. **Checks**: the shape of `messages`, then for client keys the monthly budget against the month's recorded spend, allowed models, known price and RPM limit.
5. **Session merge** (optional): when the request has a `session_id`, it takes the session's turn (`session_turns`: one turn at a time per client and session, waiting for the previous one within the request's wait budget, then `503 session_busy`), then stored history is prepended. The turn comes after every check, so a refused request never waits for it.
6. **Cache lookup**: exact-match cache keyed on the client, the model, the messages and sampling parameters (`data/cache.db`); then, if enabled, the semantic cache. A hit is returned immediately and is not billed.
7. **Forwarding** (`router.forward` or `router.forward_stream`):
   - the model string is resolved to a list of `(provider, model)` candidates. An explicit `<provider-id>:` prefix selects one provider; otherwise all active providers are candidates, ordered by a score of latency × error rate × in-flight requests;
   - a streamed request whose route fails before its first chunk (an error status, a timeout, a dropped connection) goes to the next candidate; once a chunk is out, a cut ends the stream with an error event (`stream_interrupted`), then `[DONE]`;
   - each candidate takes a slot of its provider (local and CLI providers one at a time by default, `max_concurrency` otherwise); a full one passes the request, streamed or not, to the next, and when all are full the request waits up to `PLUGBOARD_PROVIDER_WAIT_S` for a slot (not for a provider with `PLUGBOARD_PROVIDER_MAX_WAITING` requests already waiting), within its wait budget, then gets `503 providers_busy`;
   - a circuit breaker per route (3 provider failures, 60 s cooldown) shows a failing route in `/admin/circuits` and makes the LLM pricing pass skip it; requests are still sent, to the model asked for, but the route is tried last;
   - a route whose provider answered `429` (or a CLI quota error) is paused for the provider's `Retry-After`, else 30 s, 1 min, then 5 min, and is tried last until then; a paused route is never refused, and a success clears its pause;
   - HTTP providers get `POST <base_url>/v1/chat/completions` with their credentials, retried on `429`, `500`, `502`, `503`, `529` after 2, 5 and 15 seconds by default, except an error saying the context is too long, or a `429` whose `Retry-After` exceeds the next delay, which go straight to the next candidate;
   - CLI providers run the vendor CLI in a neutral working directory, with a per-CLI concurrency limit, and the output is converted to a standard chat response.
8. **Billing**: token counts from the provider (estimated with a tokenizer when absent), priced with the month's grid, appended to `data/usage.db` with the client and key. The response gets a `billing` object and `X-Cost-*` headers.
9. **Cache write** and **session record** (a streamed HTTP answer once complete; a CLI answer before its replay as a stream, so a hang-up during the replay does not undo it), then the response; the session's turn is given back once the response is sent.

Streaming requests to HTTP providers are proxied chunk by chunk and billed at the end of the stream. Streaming requests to CLI providers are completed first and replayed as server-sent events, which also lets them be cached.

## Modules

| Module | Responsibility |
|---|---|
| `__init__.py` | Package version; loads `.env` (unless `PLUGBOARD_SKIP_DOTENV=1`) and applies `envcompat` before any module reads its settings |
| `app.py` | Authentication, middlewares, chat admission (concurrency cap, session turn), health, contract and dashboard pages; registers the admin routes and the chat path |
| `admin_routes/` | Admin endpoints, one module per area (model probes, monitoring, providers, OAuth, direct providers, Ollama, keys, jobs, pricing, usage, sign-in and MFA), each declared on the app by `register(app, core)` |
| `chat_path/` | Chat path, one module per subject (client hang-up and SSE, answers and errors, spending and model access checks, billing, chat completions, model catalogue and preflight, `/v2` aliases, client account), each declared on the app by `register(app, core)` |
| `router.py` | Model resolution, forwarding, retries, fallback, model listing and discovery |
| `provider_admission.py`, `request_wait.py`, `session_turns.py`, `route_pauses.py` | Per-provider cap on requests in flight and the wait for a slot; one wait budget per chat request; one turn at a time per client session; a route that answered `429` is tried last for a while |
| `live_requests.py` | In-memory list of the chat requests in flight and the last finished ones, with their phase (`/v1/inflight`, `/admin/inflight`) |
| `config.py` | Provider store (`providers.json`), encryption of credentials, provider domain allowlist, `.env` loading |
| `apikeys.py` | Client key store (`api_keys.json`), hashing, rotation |
| `storage.py`, `envcompat.py` | Data directory, atomic JSON writes, owner-only files, SQLite purge and compaction; settings read under their `PLUGBOARD_` or old `LLM_ENABLER_` names, numbers checked against bounds |
| `secrets_rekey.py` | Re-encrypts every stored secret under a new `ENCRYPTION_KEY` (run by `scripts/rotate_encryption_key.py`) |
| `billing.py`, `usage.py` | Monthly pricing grids and cost calculation; usage records (SQLite) |
| `tokenizer.py` | Token estimate (tiktoken `cl100k_base`, else a character count) when a provider reports no usage |
| `pricing_scraper.py`, `pricing_scheduler.py`, `official_prices.py` | Price sources (including the dated official price table) and the background refresh with its circuit breaker |
| `pricing_merge.py`, `pricing_suggestions.py`, `llm_pricing.py` | Which source's price wins, model by model, and price drifts; price suggestions waiting for an admin (`pricing/suggestions.json`); optional pass that reads provider pricing pages with a model and queues the prices as suggestions (`PLUGBOARD_LLM_PRICING=1`) |
| `intervals.py`, `change_events.py` | Refresh cadences of the pricing and model-catalog schedulers; journal (`catalog/changes.jsonl`) and webhooks of the catalog and price changes a refresh finds |
| `cache.py`, `semantic_cache.py`, `cache_common.py` | Exact-match and semantic response caches, tenant isolation rules |
| `circuit_breaker.py`, `metrics.py`, `ratelimit.py` | Per-route breaker (`provider-id:model`), in-memory request metrics, rate limits and login lockout |
| `direct_providers.py` | Provider presets shown in the dashboard |
| `models_dev.py` | models.dev catalog download (the last good copy is kept) and the presets generated from it |
| `endpoints.py` | Builds a provider's endpoint URLs from its `base_url`, adding `/v1` unless the base already names a version |
| `netguard.py` | Egress guard: providers created from the catalog or a built-in HTTP preset (or with `egress_guard`) never connect to a non-public address, checked on every connection |
| `legacy_connectors.py` | Refuses the providers and model IDs of the removed OpenClaw connector |
| `cli_subprocess.py`, `cli_auth.py` | Running vendor CLIs; reading credentials stored by CLIs |
| `cli_limits.py` | Names the chat fields a CLI route cannot serve (tools, response format, non-text parts), refused as `UNSUPPORTED_ON_CLI` |
| `oauth_manager.py`, `oauth_store.py` | GitHub device flow from the dashboard; encrypted credential store |
| `ollama.py` | Ollama detection, registration and model management |
| `ollama_admin.py` | Switch for local model downloads and disk-space guard before an Ollama pull |
| `model_availability.py`, `model_quarantine.py`, `model_catalog.py`, `model_ranker.py`, `benchmarks.py` | Connectivity probes, scheduled catalog refresh (every 6 hours by default), quarantine, aliases, ranking and model metadata |
| `model_diff.py`, `model_exposure.py`, `model_identity.py` | Applies a discovered model list to a provider (delayed removals, anomaly guard); which model versions a provider lists (`PLUGBOARD_MODEL_EXPOSE`); a model ID's owner provider and native name, display aliases and allowed-models matching |
| `probes/` (`internet.py`) | Internet-access probe of a model (today's and yesterday's date, the weekday), used by `/admin/models/check-internet` |
| `provider_watchdog.py`, `monitoring.py`, `health_probe.py` | Background health checks, health history, provider probes |
| `readiness.py` | Checks behind `GET /health/ready` (usage database, CLI binaries, client auth) |
| `jobs.py`, `maintenance.py`, `retention.py` | Jobs page (status, log, run now) of the in-process background tasks; `PeriodicJob` runner with the availability snapshot and data retention jobs; purge of old SQLite rows, with a report in `retention.json` |
| `alerts.py`, `webhooks.py`, `audit.py` | Alert webhook, signed event webhooks, admin audit trail |
| `admin_mfa.py`, `admin_sessions.py`, `session_store.py`, `log_redact.py`, `logging_config.py`, `http_pool.py` | TOTP for admins, revocable admin sessions, chat sessions, log redaction, JSON logging, shared HTTP pool |
| `local_login.py` | Login token and remembered-browser cookies that local auto-login requires |
| `embeddings_api.py`, `embeddings_schema.py` | `POST /v1/embeddings` and `GET /v1/embeddings/models`, client key only, served by a local Ollama; the request model, without web-framework imports |
| `decision_api.py`, `decisions.py`, `decision_runtime.py`, `decision_assets.py` | `/v1/decisions` routes, client key only, never routed to chat; request and answer models, their checks and the hosted Jev transport; local Laya decisions in a one-shot worker process; pinned manifest of the local model files, verified, downloaded only by `scripts/decision_download.py` |
| `api_contract.py` | OpenAPI 3.1 contract of the client API (`GET /v1/openapi.json`, `make contract`) |
| `suivi.py`, `suivi_api.py`, `suivi_import.py` | Ticket board store (`suivi.db`) and its status rules; its admin API (`/admin/suivi/*`, page `/suivi`); import from Superpowers plans and specs and `TODOS.md` |
| `mcp_server.py` | MCP server and update registry (separate process, started by `scripts/run_mcp.py`) |
| `static/` | Dashboard and client portal (plain HTML, CSS and JavaScript) |

### Route table guard

`tests/test_admin_routes_guard.py` compares the app with `tests/fixtures/admin_routes_snapshot.json`: the route table (paths, methods, dependencies, parameters, include context of the routers), the admin OpenAPI document and the effects of a cold import of `plugboard.app` (logs, loaded modules, data files). Any change to a route fails it, on purpose, and so does a new `plugboard` module loaded when the app is imported (the modules of `plugboard/admin_routes/` and `plugboard/chat_path/` do not count among these import effects). When you add, remove or change a route (or such a module) deliberately, regenerate the fixture from the repository root, then review its diff and commit it with the change:

```bash
.venv/bin/python -m tests.route_snapshot
git diff tests/fixtures/admin_routes_snapshot.json
```

The diff must show only the change you meant. Never regenerate it to silence a failure you cannot explain: that failure is what the guard is for.

`tests/test_chat_path_guard.py` compares the app with `tests/fixtures/chat_path_snapshot.json`: the client routes and their place in the route table, the client contract (`build_openapi()`, its version included), the exception handlers and the middleware stack, in order. The same rule applies: regenerate it only for a change you meant, then review its diff and commit it with the change:

```bash
.venv/bin/python -m tests.chat_path_snapshot
git diff tests/fixtures/chat_path_snapshot.json
```

## State

Everything lives under `PLUGBOARD_DATA` (default `data/`):

- JSON files, each replaced atomically: `providers.json`, `api_keys.json` and `pricing/YYYY_MM.json` (also guarded by file locks), `model_status.json`, `admin_mfa.json`, `admin_people.json` (personal dashboard keys);
- SQLite databases, most of them in WAL mode: `usage.db`, `cache.db`, `semantic_cache.db`, `audit.db`, `monitoring.db`, `watchdog.db`, `credentials.db`, `sessions.db`, `client_updates.db`.

There is no external database or queue, which keeps the deployment to one process and one directory, and limits it to **one instance per data directory**. Request metrics and circuit breaker states are in memory and reset on restart.

## Security boundaries

- **Admin plane**: `/admin/*`, `/providers*`, `/metrics*`, `/docs` and the dashboard require the `ADMIN_KEY` (bearer) or a signed session cookie obtained by logging in with it; the session is also recorded server-side and revoked at logout or on any MFA change. With MFA enforced, the bearer needs a single-use TOTP code (`X-Plugboard-Admin-TOTP`). A personal key (`pba_…`, `data/admin_people.json`) opens the same plane, always with a TOTP code of its own secret, and names its holder in the audit log; it never changes the second factor of `ADMIN_KEY`. As a bearer, a personal key is not accepted on `/v1/*`; a session it opened reaches `/v1/*` (the Playground) as the `admin` client, like any admin session, and that usage is recorded under `admin`, not under the person. Auto-login only applies to a local install opened from the same machine.
- **Client plane**: `/v1/*` and `/v2/*` accept client keys (and the admin key, except `/v1/embeddings` and `/v1/decisions`, which take a client key only). Cache entries, sessions and usage are scoped to the client.
- **Secrets at rest**: provider tokens, OAuth credentials and the MFA secret are encrypted with Fernet; client keys are stored as SHA-256 hashes.
- **CLI execution**: CLIs are resolved to absolute paths once, run without a shell, from a neutral directory, with a concurrency limit; Codex runs in a read-only sandbox, and every agy launch by the gateway runs in a bubblewrap cage (`plugboard/agy_cage.py`), never without it (the operator's `scripts/bench_latence.py` runs agy bare, BUG-110).

Design decisions and their history are recorded in the [ADRs](adr/README.md).
