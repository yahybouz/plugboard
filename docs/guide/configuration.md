# Configuration reference

Plugboard is configured with environment variables. `run.py` and the server load `.env` from the working directory at startup; real environment variables take precedence over `.env`. `.env.example` lists the common ones with comments.

**Naming.** Plugboard-specific variables use the `PLUGBOARD_` prefix. The older `LLM_ENABLER_` prefix is still accepted for every variable below (for example `LLM_ENABLER_DATA` for `PLUGBOARD_DATA`) but is deprecated. Variables without a prefix (`ADMIN_KEY`, `API_KEY`, `HOST`, `PORT`, provider and scheduler settings) keep their names.

Boolean flags accept `1`, `true`, `yes`, `on` unless stated otherwise. Values below are the defaults applied when the variable is unset.

**Numbers.** A numeric setting whose value is not a finite number (text, `1.5` where a whole number is expected, `nan`, `inf`) keeps its default: the gateway logs one warning naming the variable and starts. A list of numbers (`PLUGBOARD_RETRY_DELAYS`) keeps its whole default when one item is not a finite number or is outside the setting's bounds. `PORT` and `MCP_PORT`, read by the launch scripts `run.py` and `scripts/run_mcp.py`, are the exception: a value that is not a whole number stops them.

## Security and authentication

| Variable | Default | Description |
|---|---|---|
| `ADMIN_KEY` | empty | Admin secret for the dashboard and `/admin/*`. Required: the server refuses to start without it, with fewer than 16 characters, or with a placeholder written `<...>` as in these guides, unless `PLUGBOARD_DEV=1`. `make init` generates one. |
| `API_KEY` | empty | Any non-empty value turns on client authentication for `/v1/*` (clients use `pb_` keys). The value itself is not a client key. Empty: client authentication is off, a critical warning is logged every five minutes and `/health/ready` answers `503` with `checks.auth: "fail"` (`"dev"` with `PLUGBOARD_DEV=1`). |
| `ENCRYPTION_KEY` | empty | Key used to encrypt provider tokens, OAuth credentials and the MFA secret at rest. Empty: a key is generated once in `data/.encryption_key`. Changing it makes stored credentials unreadable. |
| `PLUGBOARD_DEV` | empty | `1` marks a development instance: the server may start without `ADMIN_KEY`, and the empty-`API_KEY` warning is logged once instead of repeated. With `API_KEY` empty too, every route is open, so `run.py` then listens on a loopback `HOST` only, and the server answers only requests that arrive on a loopback interface. |
| `PLUGBOARD_LOCAL_AUTO_LOGIN` | `auto` | Dashboard auto-login. `auto`: only when `run.py` listens on a loopback address. `1`: also when listening on another interface (personal workstation). `0`: never. Unknown values count as `0`. See [Security notes](../../README.md#security-notes). |
| `PLUGBOARD_OPEN_BROWSER` | empty | `1` (set by `make dev`): once the server answers, `run.py` opens the dashboard with the one-time login token from `data/.login_token`. Loopback bind only. |
| `PLUGBOARD_ADMIN_MFA_REQUIRED` | empty | `1` requires a TOTP code at admin login, and refuses the admin key alone as a bearer ([admin API with MFA](monitoring.md#admin-api-with-mfa)), once a secret is enrolled (`POST /admin/mfa/begin`, then `POST /admin/mfa/setup`, or `make admin-mfa-setup`). Without an enrolled secret it has no effect, so it cannot lock you out. |
| `ADMIN_SESSION_MAX_AGE` | `28800` | Lifetime of the admin session cookie, in seconds. |
| `COOKIE_SECURE` | `auto` | `auto` marks the session cookie `Secure` unless the host is `localhost`, `127.0.0.1` or `::1`; `true` or `false` force it. |
| `PLUGBOARD_ALLOW_PERMISSIVE` | empty | `1` disables strict tenant isolation: cache entries may be shared by anonymous requests. Leave empty unless you run without client keys on purpose. |
| `PLUGBOARD_STRICT_TENANT` | empty | Deprecated: `0` is equivalent to `PLUGBOARD_ALLOW_PERMISSIVE=1`. Setting it to `1` with an empty `API_KEY` stops the server at startup. |
| `PLUGBOARD_CACHE_RESPONSE_FLOOR_MS` | `0` | When above 0, cache hits are delayed to at least this many milliseconds, so response time does not reveal that a prompt was cached. |
| `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED` | empty | Comma-separated allowlist of provider hosts. `api.openai.com` matches exactly, `.anthropic.com` matches the domain and its subdomains; list `localhost` explicitly for local providers. Empty: no restriction. |
| `CORS_ORIGINS` | empty | Comma-separated allowed origins. Empty: no cross-origin access, or `*` when both `API_KEY` and `ADMIN_KEY` are empty. |

The server refuses to start when `API_KEY` or `ADMIN_KEY` holds comment text copied from `.env.example` (a value that starts with `#` followed by a space), and logs a critical message when `ENCRYPTION_KEY` does.

### Changing `ENCRYPTION_KEY`

Changing the key directly makes the stored provider tokens, OAuth credentials and admin MFA secret unreadable. Rotate it instead, with the server stopped:

```bash
make rotate-encryption-key ARGS="--new-key-file ~/plugboard-new.key --generate --dry-run"   # check first
make rotate-encryption-key ARGS="--new-key-file ~/plugboard-new.key --generate"
```

The command reads every secret with the current key and re-encrypts it with the new one, after copying the files to `data/backups/rekey-<time>/`. If one secret cannot be read, nothing is written and the command lists where it is. The new key is written to the file you name (mode 0600), never printed. When `ENCRYPTION_KEY` is set, put the content of that file in `ENCRYPTION_KEY` before starting the server; when it is empty, the command replaces `data/.encryption_key` itself. Use it to replace a key that held comment text from an older `.env.example`.

A completed rotation also signs out every dashboard session and forgets the browsers remembered for local auto-login (a dry run or a refused rotation signs out nobody). If the session store cannot be rewritten, the rotation still completes and says so: use **Log out everywhere** in the dashboard once the server runs again.

## Server

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Interface `run.py` listens on. `.env.example` sets `127.0.0.1` (local install). With `API_KEY` and `ADMIN_KEY` both empty, `run.py` refuses any value other than `127.0.0.1`, `localhost` or `::1` ([details](troubleshooting.md#api_key-and-admin_key-are-both-empty-refusing-to-listen-on-0000)). |
| `PORT` | `8765` | Listening port. |
| `RELOAD` | `false` | `true` enables hot reload (`make dev` sets it). |
| `PLUGBOARD_DATA` | `data` | Directory for all state (JSON files, SQLite databases, pricing grids). The pricing scheduler's `cb_state.json` always lives in the `data/` directory of the installation. |
| `PLUGBOARD_BIND_HOST` | set by `run.py` | Records the interface the server really listens on; used by auto-login. Do not set it yourself. |
| `PLUGBOARD_SKIP_DOTENV` | empty | `1` stops the configuration module from loading `.env` (used by the test suite). |
| `RATE_LIMIT` | `60` | Requests per minute per client IP address, all routes. `0` disables it. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Addresses (or networks) of the reverse proxies whose `X-Forwarded-For` gives the client address for the rate limit, the admin-login lockout and the audit log (uvicorn setting, read by `run.py`; with the `uvicorn` command use `--env-file`). Never `*` or a `/0` network: the boot log warns. |
| `MAX_BODY_BYTES` | `10485760` | Maximum request body size (10 MB). |
| `PLUGBOARD_MAX_INFLIGHT_CHAT` | `8` | Maximum concurrent chat requests, streaming included. `0` means no cap. |
| `PLUGBOARD_INFLIGHT_WAIT_S` | `10` | How long a request waits for a free slot before `503`. `0` fails immediately. A request's waits (slot, session turn, provider) share one budget, the longer of this and `PLUGBOARD_PROVIDER_WAIT_S`; with both at `0`, a session turn sent while the previous one runs gets `503` at once ([one wait per request](clients-and-keys.md#how-limits-are-enforced)). |
| `PLUGBOARD_LOG_FILE` | empty | Also write logs to this file, with rotation. |
| `PLUGBOARD_LOG_MAX_BYTES` | `20971520` | Size of one log file before rotation. |
| `PLUGBOARD_LOG_BACKUPS` | `5` | Rotated log files to keep. |
| `APP_NAME`, `SERVICE_NAME` | `plugboard` | Values of the `application` and `service` fields in JSON logs. |
| `PLUGBOARD_DOCS_BASE_URL` | empty | Where this repository's files are published, for example `https://gitlab.com/<group>/plugboard/-/blob/main`. The dashboard then shows the `docs/guide/...` references it gets (provider connection test) as links to `<base>/docs/guide/...`. Empty: they stay repository paths. Only an `http(s)` URL with a host is used; anything else is logged and ignored. No guide is served by Plugboard itself. |

## Client keys

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_DEFAULT_RPM_LIMIT` | `60` | RPM limit applied to a new key created without one. `0` means unlimited. |
| `PLUGBOARD_DEFAULT_MONTHLY_LIMIT_USD` | `10` | Monthly USD budget applied to a new key created without one. `0` means unlimited. |
| `PLUGBOARD_SESSION_TTL_S` | `86400` | Lifetime of server-side chat sessions (`session_id`). |
| `PLUGBOARD_SESSION_MAX_TURNS` | `50` | Turns kept per session. |

## Routing

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_RETRY_DELAYS` | `2,5,15` | Comma-separated seconds to wait before each retry on `429`, `500`, `502`, `503` or `529` from an HTTP provider. Each item must be a finite number from `0` to `604800` (7 days): `inf`, `nan`, a negative item or a longer delay gives the default, with a warning (see **Numbers** at the top of this page). An error saying the context is too long is not retried, nor is a `429` whose `Retry-After` exceeds the next delay, nor a timeout: the request already waited its whole timeout (900 s for a chat request), so the next provider is tried at once. After a final `429` (or a CLI quota error), the route (provider and model) is paused for the `Retry-After` the provider gave, else for 30 s, then 1 min, then 5 min as its `429`s follow one another, and is tried after the other candidates until then; a success clears the pause. |
| `PLUGBOARD_LB_ERROR_RATE_WEIGHT` | `5.0` | Weight of the error rate in the provider score used for fallback ordering. |
| `PLUGBOARD_LB_INFLIGHT_WEIGHT` | `0.35` | Weight of in-flight requests in that score. Streamed requests and CLI calls count as in flight. |
| `PLUGBOARD_LOCAL_PROVIDER_MAX_INFLIGHT` | `1` | Concurrent requests a local provider (`sovereign`, a loopback or private address, or an Ollama over plain `http://`, whatever its host; see [concurrent requests per provider](providers-and-subscriptions.md#concurrent-requests-per-provider)) takes when its record sets no `max_concurrency`. `0` means no cap. |
| `PLUGBOARD_CLI_PROVIDER_MAX_INFLIGHT` | `1` | Concurrent requests a CLI provider (`auth_source` `subprocess:`: Claude Code, Codex, Gemini CLI, agy) takes when its record sets no `max_concurrency`; the next ones wait for it within the request's wait budget, then get `503 providers_busy`. `0` means no cap. |
| `PLUGBOARD_PROVIDER_WAIT_S` | `10` | How long a request waits when every candidate provider is at its cap, before `503 providers_busy`. `0` answers at once. The wait also takes no more than what is left of the request's wait budget, shared with the waits for a global slot and for the session's turn (see `PLUGBOARD_INFLIGHT_WAIT_S`). |
| `PLUGBOARD_HTTP_POOL_SIZE` | `100` | Shared HTTP connection pool size. |
| `PLUGBOARD_HTTP_POOL_PER_HOST` | `20` | Connections per upstream host. |
| `PLUGBOARD_HTTP_DNS_TTL` | `300` | DNS cache lifetime, in seconds. |
| `PLUGBOARD_MODEL_DENYLIST` | empty | Comma-separated model names (the part after `<provider>:`) to hide from `/v1/models`. |
| `PLUGBOARD_CLAUDE_LATEST_N` | `2` | Claude models listed per family (Opus, Sonnet, Haiku). |
| `PLUGBOARD_TOP_N_PER_PROVIDER` | `0` | Optional cap on the models listed per provider in `/v1/models` (`0`: no cap). Ignored with `PLUGBOARD_MODEL_EXPOSE=all`. |
| `PLUGBOARD_MODEL_FILTER` | empty (`all`) | `curated` keeps a short list per built-in provider in `/v1/models` (see [How models are listed](providers-and-subscriptions.md#how-models-are-listed)). |

## CLI-based providers

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_SUBPROCESS_LIMIT` | `4` | Concurrent CLI processes per CLI (the Docker image sets 10). |
| `PLUGBOARD_SUBPROCESS_CWD` | a private temporary directory (mode `0700`) | Working directory of CLI processes. Everything in it becomes readable by client prompts. |
| `PLUGBOARD_SUBPROCESS_ENV_PASSTHROUGH` | empty | Extra environment variable names passed to CLI processes, comma-separated. CLI processes otherwise get an allowlist; server secrets and `PLUGBOARD_*` names are always refused. |
| `PLUGBOARD_CODEX_SANDBOX` | `read-only` | codex access to its spawn directory: `read-only` or `workspace-write`; the rest of the disk is denied. `danger-full-access` disables the sandbox. |
| `PLUGBOARD_OAUTH_CALLBACK_BASE` | `http://localhost:8765` | Public base URL used to build OAuth callback URLs (`/oauth/callback/<source>`). |
| `GEMINI_CLI_HOME` | the server account's home | The Gemini CLI's own variable, read from the server's environment: the operator's `.gemini` directory under it holds the sign-in that each Gemini call links to and the auth method it keeps. Each call still runs with a private `GEMINI_CLI_HOME` of its own. |
| `CODEX_HOME` | `~/.codex` | The codex CLI's own variable: Plugboard reads the model list the signed-in account caches there (`models_cache.json`). |

## Providers bootstrapped from the environment

The removed connector's `OPENCLAW_URL`, `OPENCLAW_KEY`, `OPENCLAW_MODEL` and
`OPENCLAW_NAME` variables are ignored; they create no provider. See the
[manual migration guide](openclaw-removal.md) for existing installations.

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama daemon URL. |
| `PLUGBOARD_OLLAMA_TOKEN` | empty | Bearer token for an Ollama behind an authenticating proxy: sent only to `OLLAMA_URL` (never to ollama.com) and stored, encrypted, on the `ollama` provider. `POST /v1/embeddings` sends it too when the engine is exactly `OLLAMA_URL`. |
| `OLLAMA_ENABLED` | `auto` | At startup only: `auto` registers Ollama if it answers, `true` also warns if it does not, `false` never registers it. `false` does not turn off a provider already registered: use the switch on the Ollama page. |
| `OLLAMA_TIMEOUT` | `30` | Timeout of Ollama management calls, in seconds. |
| `OLLAMA_CLOUD_CATALOG_URL` | `https://ollama.com/api/tags` | Catalog used to add Ollama cloud models. |
| `PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC` | `1` | `0` stops adding Ollama cloud models automatically. |
| `PLUGBOARD_LOCAL_MODEL_DOWNLOADS` | `1` | `0` refuses Ollama pulls of local (non-cloud) models; only `:cloud` / `-cloud` ids of the official library (no `user/` namespace, no other registry) can be added, and such a pull is stopped if its layers exceed 64 MB. An admin can flip it on the **Ollama** page; that choice is saved in `<data>/ollama_settings.json` and wins over this variable. |
| `PLUGBOARD_OLLAMA_MIN_FREE_GB` | `5` | Free disk space, in GB, that a local pull must leave. A pull whose registry size does not fit is refused with 507; a pull of unknown size is stopped as soon as it would cross this margin. |
| `PLUGBOARD_OLLAMA_DISK_PATH` | data dir | Path whose filesystem is checked for free space (must be on the same disk as the Ollama models). |
| `PLUGBOARD_OLLAMA_REGISTRY_URL` | `https://registry.ollama.ai` | Registry asked for a model's manifest to learn its download size before a pull (redirects are not followed). A model named `host/user/model` comes from another registry: its size is unknown. |
| `PLUGBOARD_OLLAMA_CLOUD_FAMILIES` | deepseek, kimi, qwen, glm, gemma, nemotron, minimax, gpt-oss, gemini, beam | Ollama cloud model families added automatically (comma-separated, `all` for the whole catalogue). |

## Caching

| Variable | Default | Description |
|---|---|---|
| `CACHE_TTL` | `300` | Lifetime of exact-match cache entries, in seconds. |
| `CACHE_MAX_SIZE` | `10000` | Maximum entries in the exact-match cache. |
| `PLUGBOARD_SEMANTIC_CACHE` | empty | `1` enables the semantic cache (near-duplicate prompts, computed locally). |
| `SEMANTIC_CACHE_TTL` | `3600` | Lifetime of semantic cache entries. |
| `SEMANTIC_CACHE_MAX_SIZE` | `5000` | Maximum semantic cache entries. |
| `SEMANTIC_CACHE_THRESHOLD` | `0.85` | Similarity threshold (0 to 1). |
| `PLUGBOARD_SEMANTIC_EMBED` | empty | `1` adds an embedding-based match on top of the semantic cache. This sends prompts to the embedding provider: check that provider's data-processing terms first. |
| `PLUGBOARD_EMBED_PROVIDER_ID` | empty | Provider used for embeddings. |
| `PLUGBOARD_EMBED_MODEL` | empty | Embedding model, for example `text-embedding-3-small`. |
| `PLUGBOARD_EMBED_SIMILARITY_THRESHOLD` | `0.92` | Cosine similarity threshold. |
| `PLUGBOARD_EMBED_TIMEOUT` | `10` | Embedding call timeout, in seconds. |

The unprefixed `EMBED_PROVIDER_ID`, `EMBED_MODEL`, `EMBED_SIMILARITY_THRESHOLD` and `EMBED_TIMEOUT` are still read, with a deprecation warning.

## Embeddings route

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_EMBEDDING_MODELS` | empty | Comma-separated exact `<provider-id>:<native-model>` identities served by `POST /v1/embeddings` (empty: route closed). Only active `sovereign` providers on a loopback address, or exactly at a private-IP `OLLAMA_URL`, are served. See [Embeddings](embeddings.md). |
| `PLUGBOARD_EMBEDDING_CONCURRENCY` | `2` | Embedding calls served at the same time (1–64); beyond that `POST /v1/embeddings` answers 429. |
| `PLUGBOARD_EMBEDDING_TIMEOUT_S` | `60` | Engine timeout for one embeddings call, in seconds. |

## Pricing

| Variable | Default | Description |
|---|---|---|
| `PRICING_SCHEDULER_DISABLED` | empty | `1` turns the pricing scheduler off. |
| `PRICING_DAILY_HOUR_UTC` | `3` | Hour (UTC) of the daily pricing update. |
| `PLUGBOARD_PRICING_REFRESH_INTERVAL_H` | `6` | Hours between price updates, 1 to 24 (other values are clamped, with a warning); `0` = once a day at `PRICING_DAILY_HOUR_UTC`. The older `PRICING_REFRESH_INTERVAL_H` is still read. |
| `PRICING_UPDATE_TIMEOUT_S` | `120` | Timeout of one update. |
| `PRICING_CB_COOLDOWN_S` | `3600` | Pause after 3 consecutive failures. |
| `PRICING_PROVIDERS_READY_MAX_RETRIES` | `3` | Startup retries while waiting for providers to load. |
| `PRICING_PROVIDERS_READY_BACKOFF_S` | `2` | Delay between those retries. |
| `PLUGBOARD_PRICING_SOURCES` | `external,official,provider_native,models_dev,litellm,known` | Price sources, highest priority first. An unknown name is logged as an error and the default is used. Unset with a source URL set: `external,official,known`. See [pricing sources](pricing.md#sources). |
| `PLUGBOARD_OFFICIAL_PRICES_MAX_AGE_DAYS` | `35` | Days after the official price table's last check before the scheduler status reports it `stale` (each refresh then logs a warning). A value that is not a positive integer falls back to 35. |
| `PLUGBOARD_PRICING_SOURCE_URL` | empty | https URL of your own JSON price document (the `external` source; http is refused except for localhost). The older `PRICING_SOURCE_URL` is still read, with a deprecation warning. |
| `PLUGBOARD_PRICE_DRIFT_PCT` | `20` | Percent change above which a price from a third-party source is held for review. A value that is not a positive number falls back to 20. |
| `PLUGBOARD_PRICE_DRIFT_ACTION` | `review` | `review` queues large changes as suggestions (not billed until applied); `apply` writes them. Any other value falls back to `review`. |
| `PLUGBOARD_LLM_PRICING` | empty (off) | `1` lets a model read prices from provider pages and queue them for review. See [prices read by a model](pricing.md#prices-read-by-a-model). |
| `PLUGBOARD_LLM_PRICING_URLS` | `groq=https://groq.com/pricing` | Pricing page per provider, `provider-id=https://...` separated by `;`. A malformed entry is logged as an error and ignored. Providers without an entry use their catalog `doc` link. |
| `PLUGBOARD_LLM_PRICING_PROVIDER` | first active HTTP provider | Provider that reads the pages (CLI providers are never used). |
| `PLUGBOARD_LLM_PRICING_MODEL` | that provider's default model | Model that reads the pages; its price must be known. |
| `PLUGBOARD_LLM_PRICING_MAX_USD` | `0.50` | Daily budget for these calls, kept across restarts. A negative value falls back to the default. |
| `PLUGBOARD_LLM_PRICING_MAX_MODELS` | `20` | Models looked up per pass. |
| `BENCHMARK_SOURCE_URL` | empty | Optional JSON source of model benchmark data used to rank models. |

## Provider catalog (models.dev)

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_CATALOG` | empty | `off` turns the catalog off: no download, only built-in presets. |
| `PLUGBOARD_CATALOG_URL` | `https://models.dev/api.json` | Catalog location, for a mirror. An empty value also turns the catalog off. |
| `PLUGBOARD_CATALOG_EXCLUDE` | empty | Comma-separated models.dev provider ids to hide. |
| `PLUGBOARD_CATALOG_TIMEOUT_S` | `20` | Download timeout. |
| `PLUGBOARD_CATALOG_MAX_MB` | `20` | Largest download accepted; a bigger answer keeps the last good copy. |

The catalog is downloaded with each price update. A download that fails, is invalid, or loses more than half of the providers or models keeps the last good copy (`data/catalog/models_dev.json`).

## Model catalog and probes

| Variable | Default | Description |
|---|---|---|
| `MODEL_AVAILABILITY_SCHEDULER_DISABLED` | empty | `1` turns the model-list refresh off. |
| `MODEL_AVAILABILITY_DAILY_HOUR_UTC` | `PRICING_DAILY_HOUR_UTC` + 1 | Hour (UTC) of the daily catalog refresh. |
| `PLUGBOARD_MODEL_AVAILABILITY_INTERVAL_H` | value of `PLUGBOARD_PRICING_REFRESH_INTERVAL_H` (6) | Hours between model-list refreshes, 1 to 24; `0` = once a day at `MODEL_AVAILABILITY_DAILY_HOUR_UTC`. The older `MODEL_AVAILABILITY_INTERVAL_H` is still read. |
| `PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H` | `24` | Minimum hours between two refreshes of a CLI provider (each one spawns the CLI), 1 to 24. A manual run ignores it. |
| `PLUGBOARD_MODEL_REMOVAL_PASSES` | `2` | Refreshes in a row without a model before it is removed from a provider's list (minimum 1). |
| `MODEL_AVAILABILITY_BOOTSTRAP_DELAY_S` | `8` | Delay after startup before the first refresh. |
| `MODEL_AVAILABILITY_UPDATE_TIMEOUT_S` | `900` | Timeout of one refresh. |
| `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` | empty | Which CLIs and Ollama to probe with a completion, at startup and during the refresh: `codex,claude,gemini,agy,ollama`, or `all` for every kind. Empty (the default) runs no probe: the CLIs keep their saved lists, the agy account list (`agy models`) and Ollama's installed list (`/api/tags`) are still synced, without running a model. A probe spends the CLI subscription's quota, and on Ollama loads each installed model in turn. Without `codex`, `claude` or `gemini` here, the dashboard's *Discover* keeps that CLI's saved list. |
| `PLUGBOARD_MODEL_EXPOSE` | `latest` | `latest`: per model family, only the newest versions are listed by `/v1/models` and kept by the model refresh. `all`: every model the provider reports, with no other listing filter (curated list, per-provider cap). Default models and models already used are always kept. |
| `PLUGBOARD_MODEL_EXPOSE_VERSIONS` | `2` | How many versions per family `latest` keeps (1 to 10). |
| `PLUGBOARD_MODEL_REFRESH_MAX_PROBES` | `40` | Maximum model probes per CLI per run. |
| `PLUGBOARD_MODEL_REFRESH_TIMEOUT_S` | `45` | Timeout of one CLI model probe. |
| `PLUGBOARD_PROBE_ON_BOOT` | `true` | Run the startup model refresh at every start (it probes only the kinds in `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS`). `false` skips it when the last one is less than 24 hours old. |
| `PLUGBOARD_PROBE_TOTAL_TIMEOUT_S` | `60` | Overall timeout of the startup probe (minimum 10). |
| `PLUGBOARD_OLLAMA_RECHECK_INTERVAL_S` | `300` | Interval for re-probing offline Ollama models (minimum 30), when `ollama` is in `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS`. |
| `PLUGBOARD_IGNORE_MODEL_STATUS` | `false` | `true` ignores `model_status.json` when listing models. |
| `MODEL_STATUS_PATH` | `data/model_status.json` | Location of the model status file. |
| `PLUGBOARD_REFRESH_COOLDOWN_S` | `60` | Minimum interval between two `POST /admin/models/refresh`. |
| `PLUGBOARD_INTERNET_CHECK_ON_BOOT` | `0` | `1` (or `true`, `yes`, `on`) runs the internet-access check at startup, which sends one chat request to every model except local Ollama ones. Off by default, like the probes: the startup calls no model (the watchdog's deep check, below, comes about 60 seconds later). |
| `PLUGBOARD_BULK_INTERNET_CONCURRENCY` | `3` | Concurrency of the internet-access check over all models. |
| `PLUGBOARD_BULK_INTERNET_PER_MODEL_TIMEOUT_S` | `25` | Timeout per model in that check. |

## Watchdog

| Variable | Default | Description |
|---|---|---|
| `WATCHDOG_INTERVAL` | `30` | Seconds between two health checks. |
| `WATCHDOG_FAILURES` | `3` | Consecutive failures before a restart attempt. |
| `WATCHDOG_RESTART_COOLDOWN` | `120` | Minimum seconds between two restart attempts (native restart is unsupported). |
| `WATCHDOG_MAX_RESTARTS` | `5` | Maximum restart attempts per provider per hour. |
| `WATCHDOG_HEALTH_TIMEOUT` | `10` | Timeout of a shallow check. |
| `WATCHDOG_DEEP_TIMEOUT` | `60` | Timeout of a deep check (real completion). |
| `WATCHDOG_DEEP_INTERVAL` | `3` | Run a deep check every N checks. |

## Availability snapshots

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_MONITORING_INTERVAL_S` | `300` | Seconds between two availability snapshots (one probe of every active provider, stored in `data/monitoring.db`), from which the Monitoring page computes uptime. `0` turns the snapshots off; a value from `1` to `59` gives `60`. The job is listed on the Jobs page ([monitoring](monitoring.md#dashboard-pages)). |

## Alerts and webhooks

| Variable | Default | Description |
|---|---|---|
| `ALERT_WEBHOOK_URL` | empty | URL receiving `provider_down`, `spending_alert`, `provider_egress_refused` and `provider_secret_unreadable` JSON alerts ([alert webhook](monitoring.md#alert-webhook)). |
| `ALERT_FAILURE_THRESHOLD` | `3` | Consecutive provider failures (circuit breaker's rule, never an error the request caused) before a `provider_down` alert. |
| `WEBHOOK_URLS` | empty | Comma-separated URLs receiving lifecycle events. |
| `WEBHOOK_EVENTS` | empty (all) | Comma-separated list of events to send. |
| `WEBHOOK_SECRET` | empty | Secret used to sign event payloads with HMAC-SHA256. |
| `PLUGBOARD_WEBHOOK_MAX_PER_RUN` | `50` | Maximum `models.changed` / `pricing.changed` events sent by one automatic update; the rest is summarised as `{"truncated": N}`. |
| `PLUGBOARD_CHANGE_JOURNAL_DAYS` | `365` | Days the change journal (`data/catalog/changes.jsonl`) keeps a change; `0` keeps changes until the 10 000-line cap. Lowering it deletes the older changes for good at the next write. |

## Decision engines

See [typed decisions](decisions.md) and the [local decision runtime](decisions-runtime.md).

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_JEV_API_KEY` | empty | TypeSafe credential for the hosted `jev` engine on `POST /v1/decisions`. Without it, `jev` answers 503. The network MCP server drops it from its environment. |
| `PLUGBOARD_DECISION_PYTHON` | empty | Absolute path to the Python of the isolated environment that runs the local `laya` and `laya-multilingual` engines. Never the gateway's own environment. |
| `PLUGBOARD_DECISION_ASSETS` | empty | Directory of the pinned assets downloaded with `scripts/decision_download.py`. Without it or `PLUGBOARD_DECISION_PYTHON`, the local engines are unavailable. |
| `PLUGBOARD_JEV_PRICE_PER_CALL_USD` | unset | Obsolete and ignored: Jev is billed at its official per-token price. If it is set, the server logs one warning per process (never its value). |

## Data and integrations

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_REGISTRY_PATH` | `data/registry.json` | Update registry served to client projects over MCP. Format: `plugboard/registry.example.json`. |
| `PLUGBOARD_JOB_PYTHON` | the server's interpreter | Python used by scheduled jobs (pricing, model refresh). |
| `GEMINI_OAUTH_CLIENT_ID`, `GEMINI_OAUTH_CLIENT_SECRET` | empty | Google OAuth desktop client used to sign in to Gemini from the dashboard and to refresh Gemini CLI tokens. Not shipped with Plugboard. |

## Data retention

A retention pass deletes the rows older than each store's retention, then compacts the store when at least 20 % of its pages are free. Its last report is written to `data/retention.json`, and a pass that deleted rows records a `retention.run` line in the audit trail. For the four retentions below, `0` keeps every row, and a value that is negative or not a whole number gives the default. Chat sessions are purged after `PLUGBOARD_SESSION_TTL_S` ([client keys](#client-keys)).

The pass runs inside the server once a day, as the *Data retention* job of the Jobs page, where **Run now** starts it at once, even when the daily pass is turned off. `GET /admin/retention` (admin) returns the settings in force and the last report. Back up `data/` before you shorten a retention: the rows it no longer covers are deleted for good at the next pass.

Rows are deleted in short batches, so the server's own writes get through during a long purge. Compaction is different: it locks the store for as long as it runs, and it happens only when at least 20 % of the file is free at the time of the pass, typically after a large purge such as the first one. While `data/usage.db` is compacted, billing writes can wait or fail, and `GET /health/ready` can wait up to 5 seconds, then answer `503` with `usage_db: "fail"` ([health endpoints](monitoring.md#health-endpoints)).

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_USAGE_RETENTION_MONTHS` | `13` | Whole months of usage entries kept before the current month (`data/usage.db`): with `13`, in November 2026 the oldest month kept is October 2025. Lowering it deletes the older months, and their cost history, for good. |
| `PLUGBOARD_AUDIT_RETENTION_DAYS` | `365` | Days an audit entry is kept (`data/audit.db`). |
| `PLUGBOARD_WATCHDOG_RETENTION_DAYS` | `14` | Days a watchdog event is kept (`data/watchdog.db`). |
| `PLUGBOARD_MONITORING_RETENTION_DAYS` | `90` | Days a monitoring snapshot is kept (`data/monitoring.db`). |
| `PLUGBOARD_RETENTION_HOUR_UTC` | `4` | Hour (UTC, `0` to `23`) of the daily pass; a value above `23` gives `23`. |
| `PLUGBOARD_RETENTION_DISABLED` | empty | `1`, `true` or `yes` turns the daily pass off; **Run now** still runs it. |

## MCP server and scripts

These are read by `scripts/run_mcp.py` and the maintenance scripts, not by the server.

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_URL` | `http://localhost:8765` | URL of the Plugboard instance to call. |
| `PLUGBOARD_API_KEY` | empty | Client key used by the MCP server for chat and usage. |
| `PLUGBOARD_ADMIN_KEY` | empty | Admin key used by the MCP server for provider status and the MCP admin tools (stdio only), and by `scripts/setup_railway_providers.py`. |
| `PLUGBOARD_MCP_ADMIN_TOOLS` | empty | `1` turns on the MCP admin tools `add_provider` and `refresh_catalog` (stdio only, with `PLUGBOARD_ADMIN_KEY`); see [MCP admin tools](mcp.md#admin-tools-stdio-only). |
| `PLUGBOARD_MCP_TIMEOUT` | `900` | Timeout of MCP chat calls, in seconds. |
| `PLUGBOARD_MCP_TOKEN` | empty | Bearer token the MCP server requires in SSE or HTTP mode (32+ characters); it refuses to start without it. |
| `MCP_HOST` | `127.0.0.1` | Bind address of the MCP server in SSE or HTTP mode. |
| `MCP_PORT` | `8766` | Port of the MCP server in SSE or HTTP mode. |
| `PLUGBOARD_MCP_ALLOWED_HOSTS`, `PLUGBOARD_MCP_ALLOWED_ORIGINS` | loopback | Comma-separated `Host` and `Origin` values the MCP server accepts in SSE or HTTP mode. |
| `SETUP_ANTHROPIC_API_KEY`, `SETUP_OPENAI_API_KEY`, `SETUP_GEMINI_API_KEY`, `SETUP_GITHUB_PAT` | empty | Keys that `scripts/setup_railway_providers.py` turns into providers. |
| `PLUGBOARD_SMOKE_KEY` | empty | Client key used by the post-deployment smoke `scripts/smoke_consumers.py`: a dedicated key whose `allowed_models` holds a local chat model and, where embeddings are served, the identity of `PLUGBOARD_EMBEDDING_MODELS` (an unlimited key fails the smoke). It is never printed. |
| `PLUGBOARD_SMOKE_MODEL` | empty | Chat model the smoke calls, as `/v1/models` lists it for that key (`--model` overrides it). |
| `PLUGBOARD_SMOKE_EMBEDDING_MODEL` | empty | Embedding model the smoke checks; it must be declared for the key. Empty: the first one the key may use; when there is none, the embeddings check is skipped. |


