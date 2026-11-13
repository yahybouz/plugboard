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
   - each candidate takes a slot of its provider (local and CLI providers one at a time by default, `max_concurrency` otherwise); a full one passes a non-streamed request to the next (a streamed request has a single route and waits for it only, except one to a CLI provider, routed like a non-streamed one), and when all are full the request waits up to `PLUGBOARD_PROVIDER_WAIT_S` for a slot, within its wait budget, then gets `503 providers_busy`;
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
| `app.py` | HTTP API, authentication, limits, caching and billing glue, admin endpoints, dashboard and login pages |
| `router.py` | Model resolution, forwarding, retries, fallback, model listing and discovery |
| `config.py` | Provider store (`providers.json`), encryption of credentials, provider domain allowlist, `.env` loading |
| `apikeys.py` | Client key store (`api_keys.json`), hashing, rotation |
| `billing.py`, `usage.py` | Monthly pricing grids and cost calculation; usage records (SQLite) |
| `pricing_scraper.py`, `pricing_scheduler.py`, `official_prices.py` | Price sources (including the dated official price table) and the background refresh with its circuit breaker |
| `cache.py`, `semantic_cache.py`, `cache_common.py` | Exact-match and semantic response caches, tenant isolation rules |
| `circuit_breaker.py`, `metrics.py`, `ratelimit.py` | Per-model breaker, in-memory request metrics, rate limits and login lockout |
| `direct_providers.py` | Provider presets shown in the dashboard |
| `cli_subprocess.py`, `cli_auth.py` | Running vendor CLIs; reading credentials stored by CLIs |
| `oauth_manager.py`, `oauth_store.py` | GitHub device flow from the dashboard; encrypted credential store |
| `ollama.py` | Ollama detection, registration and model management |
| `model_availability.py`, `model_quarantine.py`, `model_catalog.py`, `model_ranker.py`, `benchmarks.py` | Connectivity probes, daily catalog refresh, quarantine, aliases, ranking and model metadata |
| `provider_watchdog.py`, `monitoring.py`, `health_probe.py` | Background health checks, health history, provider probes |
| `alerts.py`, `webhooks.py`, `audit.py` | Alert webhook, signed event webhooks, admin audit trail |
| `admin_mfa.py`, `admin_sessions.py`, `session_store.py`, `log_redact.py`, `logging_config.py`, `http_pool.py` | TOTP for admins, revocable admin sessions, chat sessions, log redaction, JSON logging, shared HTTP pool |
| `mcp_server.py` | MCP server and update registry (separate process, started by `scripts/run_mcp.py`) |
| `static/` | Dashboard and client portal (plain HTML, CSS and JavaScript) |

## State

Everything lives under `PLUGBOARD_DATA` (default `data/`):

- JSON files guarded by file locks: `providers.json`, `api_keys.json`, `pricing/YYYY_MM.json`, `model_status.json`, `admin_mfa.json`;
- SQLite databases, most of them in WAL mode: `usage.db`, `cache.db`, `semantic_cache.db`, `audit.db`, `monitoring.db`, `watchdog.db`, `credentials.db`, `sessions.db`, `client_updates.db`.

There is no external database or queue, which keeps the deployment to one process and one directory, and limits it to **one instance per data directory**. Request metrics and circuit breaker states are in memory and reset on restart.

## Security boundaries

- **Admin plane**: `/admin/*`, `/providers*`, `/metrics*`, `/docs` and the dashboard require the `ADMIN_KEY` (bearer) or a signed session cookie obtained by logging in with it; the session is also recorded server-side and revoked at logout or on any MFA change. With MFA enforced, the bearer needs a single-use TOTP code (`X-Plugboard-Admin-TOTP`). Auto-login only applies to a local install opened from the same machine.
- **Client plane**: `/v1/*` and `/v2/*` accept client keys (and the admin key). Cache entries, sessions and usage are scoped to the client.
- **Secrets at rest**: provider tokens, OAuth credentials and the MFA secret are encrypted with Fernet; client keys are stored as SHA-256 hashes.
- **CLI execution**: CLIs are resolved to absolute paths once, run without a shell, from a neutral directory, with a concurrency limit; Codex runs in a read-only sandbox.

Design decisions and their history are recorded in the [ADRs](adr/README.md).
