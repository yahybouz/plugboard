# Configuration reference

Plugboard is configured with environment variables. `run.py` and the server load `.env` from the working directory at startup; real environment variables take precedence over `.env`. `.env.example` lists the common ones with comments.

**Naming.** Plugboard-specific variables use the `PLUGBOARD_` prefix. The older `LLM_ENABLER_` prefix is still accepted for every variable below (for example `LLM_ENABLER_DATA` for `PLUGBOARD_DATA`) but is deprecated. Variables without a prefix (`ADMIN_KEY`, `API_KEY`, `HOST`, `PORT`, provider and scheduler settings) keep their names.

Boolean flags accept `1`, `true`, `yes`, `on` unless stated otherwise. Values below are the defaults applied when the variable is unset.

## Security and authentication

| Variable | Default | Description |
|---|---|---|
| `ADMIN_KEY` | empty | Admin secret for the dashboard and `/admin/*`. Required: the server refuses to start without it, or with fewer than 16 characters, unless `PLUGBOARD_DEV=1`. `make init` generates one. |
| `API_KEY` | empty | Any non-empty value turns on client authentication for `/v1/*` (clients use `pb_` keys). The value itself is not a client key. Empty: client authentication is off, a critical warning is logged every five minutes and `/health` reports `auth_disabled`. |
| `ENCRYPTION_KEY` | empty | Key used to encrypt provider tokens, OAuth credentials and the MFA secret at rest. Empty: a key is generated once in `data/.encryption_key`. Changing it makes stored credentials unreadable. |
| `PLUGBOARD_DEV` | empty | `1` marks a development instance: the server may start without `ADMIN_KEY`, and the empty-`API_KEY` warning is logged once instead of repeated. |
| `PLUGBOARD_LOCAL_AUTO_LOGIN` | `auto` | Dashboard auto-login. `auto`: only when `run.py` listens on a loopback address. `1`: also when listening on another interface (personal workstation). `0`: never. Unknown values count as `0`. See [Security notes](../../README.md#security-notes). |
| `PLUGBOARD_ADMIN_MFA_REQUIRED` | empty | `1` requires a TOTP code at admin login once a secret is enrolled (`POST /admin/mfa/begin`, then `POST /admin/mfa/setup`, or `make admin-mfa-setup`). Without an enrolled secret it has no effect, so it cannot lock you out. |
| `ADMIN_SESSION_MAX_AGE` | `28800` | Lifetime of the admin session cookie, in seconds. |
| `COOKIE_SECURE` | `auto` | `auto` marks the session cookie `Secure` unless the host is `localhost`, `127.0.0.1` or `::1`; `true` or `false` force it. |
| `PLUGBOARD_ALLOW_PERMISSIVE` | empty | `1` disables strict tenant isolation: cache entries may be shared by anonymous requests. Leave empty unless you run without client keys on purpose. |
| `PLUGBOARD_STRICT_TENANT` | empty | Deprecated: `0` is equivalent to `PLUGBOARD_ALLOW_PERMISSIVE=1`. Setting it to `1` with an empty `API_KEY` stops the server at startup. |
| `PLUGBOARD_CACHE_RESPONSE_FLOOR_MS` | `0` | When above 0, cache hits are delayed to at least this many milliseconds, so response time does not reveal that a prompt was cached. |
| `PLUGBOARD_PROVIDER_DOMAINS_ALLOWED` | empty | Comma-separated allowlist of provider hosts. `api.openai.com` matches exactly, `.anthropic.com` matches the domain and its subdomains; list `localhost` explicitly for local providers. Empty: no restriction. |
| `CORS_ORIGINS` | empty | Comma-separated allowed origins. Empty: no cross-origin access, or `*` when both `API_KEY` and `ADMIN_KEY` are empty. |

The server refuses to start when `API_KEY` or `ADMIN_KEY` holds comment text copied from `.env.example` (a value that starts with `#` followed by a space), and logs a critical message when `ENCRYPTION_KEY` does.

## Server

| Variable | Default | Description |
|---|---|---|
| `HOST` | `0.0.0.0` | Interface `run.py` listens on. `.env.example` sets `127.0.0.1` (local install). |
| `PORT` | `8765` | Listening port. |
| `RELOAD` | `false` | `true` enables hot reload (`make dev` sets it). |
| `PLUGBOARD_DATA` | `data` | Directory for all state (JSON files, SQLite databases, pricing grids). The pricing scheduler's `cb_state.json` always lives in the `data/` directory of the installation. |
| `PLUGBOARD_BIND_HOST` | set by `run.py` | Records the interface the server really listens on; used by auto-login. Do not set it yourself. |
| `PLUGBOARD_SKIP_DOTENV` | empty | `1` stops the configuration module from loading `.env` (used by the test suite). |
| `RATE_LIMIT` | `60` | Requests per minute per client IP address, all routes. `0` disables it. |
| `MAX_BODY_BYTES` | `10485760` | Maximum request body size (10 MB). |
| `PLUGBOARD_MAX_INFLIGHT_CHAT` | `0` | Maximum concurrent chat requests, streaming included. `0` means no cap. |
| `PLUGBOARD_INFLIGHT_WAIT_S` | `0` | How long a request waits for a free slot before `503`. `0` fails immediately. |
| `PLUGBOARD_LOG_FILE` | empty | Also write logs to this file, with rotation. |
| `PLUGBOARD_LOG_MAX_BYTES` | `20971520` | Size of one log file before rotation. |
| `PLUGBOARD_LOG_BACKUPS` | `5` | Rotated log files to keep. |
| `APP_NAME`, `SERVICE_NAME` | `plugboard` | Values of the `application` and `service` fields in JSON logs. |

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
| `PLUGBOARD_RETRY_DELAYS` | `2,5,15` | Seconds to wait before each retry on `429`/`5xx` from an HTTP provider. |
| `PLUGBOARD_LB_ERROR_RATE_WEIGHT` | `5.0` | Weight of the error rate in the provider score used for fallback ordering. |
| `PLUGBOARD_LB_INFLIGHT_WEIGHT` | `0.35` | Weight of in-flight requests in that score. |
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

## Providers bootstrapped from the environment

| Variable | Default | Description |
|---|---|---|
| `OPENCLAW_URL` | empty | Base URL of a standard chat API endpoint, registered when no provider exists yet. |
| `OPENCLAW_KEY` | empty | Its bearer token; both URL and key are required. |
| `OPENCLAW_MODEL` | `claude-sonnet-4-6` | Its default model. |
| `OPENCLAW_NAME` | `OpenClaw` | Its name; the provider ID is derived from it. |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama daemon URL. |
| `OLLAMA_ENABLED` | `auto` | `auto` registers Ollama if it answers, `true` also warns if it does not, `false` never registers it. |
| `OLLAMA_TIMEOUT` | `30` | Timeout of Ollama management calls, in seconds. |
| `OLLAMA_CLOUD_CATALOG_URL` | `https://ollama.com/api/tags` | Catalog used to add Ollama cloud models. |
| `PLUGBOARD_OLLAMA_CLOUD_AUTOSYNC` | `1` | `0` stops adding Ollama cloud models automatically. |
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
| `PLUGBOARD_PRICING_SOURCES` | `external,provider_native,models_dev,litellm,known` | Price sources, highest priority first. An unknown name is logged as an error and the default is used. Unset with a source URL set: `external,known` (previous behaviour). See [pricing sources](pricing.md#sources). |
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
| `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` | `codex,claude,gemini,agy,ollama` | Which CLIs and Ollama to probe (`all` for every kind). |
| `PLUGBOARD_MODEL_EXPOSE` | `latest` | `latest`: per model family, only the newest versions are listed by `/v1/models` and kept by the model refresh. `all`: every model the provider reports, with no other listing filter (curated list, per-provider cap). Default models and models already used are always kept. |
| `PLUGBOARD_MODEL_EXPOSE_VERSIONS` | `2` | How many versions per family `latest` keeps (1 to 10). |
| `PLUGBOARD_MODEL_REFRESH_MAX_PROBES` | `40` | Maximum model probes per CLI per run. |
| `PLUGBOARD_MODEL_REFRESH_TIMEOUT_S` | `45` | Timeout of one CLI model probe. |
| `PLUGBOARD_PROBE_ON_BOOT` | `true` | Probe model connectivity at every startup. `false` reuses the last status if it is less than 24 hours old. |
| `PLUGBOARD_PROBE_TOTAL_TIMEOUT_S` | `60` | Overall timeout of the startup probe (minimum 10). |
| `PLUGBOARD_OLLAMA_RECHECK_INTERVAL_S` | `300` | Interval for re-probing offline Ollama models (minimum 30). |
| `PLUGBOARD_IGNORE_MODEL_STATUS` | `false` | `true` ignores `model_status.json` when listing models. |
| `MODEL_STATUS_PATH` | `data/model_status.json` | Location of the model status file. |
| `PLUGBOARD_REFRESH_COOLDOWN_S` | `60` | Minimum interval between two `POST /admin/models/refresh`. |
| `PLUGBOARD_INTERNET_CHECK_ON_BOOT` | `1` | `0` skips the startup internet-access check, which sends one request to every model. |
| `PLUGBOARD_BULK_INTERNET_CONCURRENCY` | `3` | Concurrency of the internet-access check over all models. |
| `PLUGBOARD_BULK_INTERNET_PER_MODEL_TIMEOUT_S` | `25` | Timeout per model in that check. |

## Watchdog

| Variable | Default | Description |
|---|---|---|
| `WATCHDOG_INTERVAL` | `30` | Seconds between two health checks. |
| `WATCHDOG_FAILURES` | `3` | Consecutive failures before a restart attempt. |
| `WATCHDOG_RESTART_COOLDOWN` | `120` | Minimum seconds between two restarts of a provider. |
| `WATCHDOG_MAX_RESTARTS` | `5` | Maximum restarts per provider per hour. |
| `WATCHDOG_HEALTH_TIMEOUT` | `10` | Timeout of a shallow check. |
| `WATCHDOG_DEEP_TIMEOUT` | `60` | Timeout of a deep check (real completion). |
| `WATCHDOG_DEEP_INTERVAL` | `3` | Run a deep check every N checks. |
| `WATCHDOG_RESTART_TIMEOUT` | `120` | Timeout of the launcher script. |

## Alerts and webhooks

| Variable | Default | Description |
|---|---|---|
| `ALERT_WEBHOOK_URL` | empty | URL receiving `provider_down` and `spending_alert` JSON alerts. |
| `ALERT_FAILURE_THRESHOLD` | `3` | Consecutive provider failures before a `provider_down` alert. |
| `WEBHOOK_URLS` | empty | Comma-separated URLs receiving lifecycle events. |
| `WEBHOOK_EVENTS` | empty (all) | Comma-separated list of events to send. |
| `WEBHOOK_SECRET` | empty | Secret used to sign event payloads with HMAC-SHA256. |
| `PLUGBOARD_WEBHOOK_MAX_PER_RUN` | `50` | Maximum `models.changed` / `pricing.changed` events sent by one automatic update; the rest is summarised as `{"truncated": N}`. |

## Data and integrations

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_REGISTRY_PATH` | `data/registry.json` | Update registry served to client projects over MCP. Format: `plugboard/registry.example.json`. |
| `PLUGBOARD_JOB_PYTHON` | the server's interpreter | Python used by scheduled jobs (pricing, model refresh). |
| `GEMINI_OAUTH_CLIENT_ID`, `GEMINI_OAUTH_CLIENT_SECRET` | empty | Google OAuth desktop client used to sign in to Gemini from the dashboard and to refresh Gemini CLI tokens. Not shipped with Plugboard. |

## MCP server and scripts

These are read by `scripts/run_mcp.py` and the maintenance scripts, not by the server.

| Variable | Default | Description |
|---|---|---|
| `PLUGBOARD_URL` | `http://localhost:8765` | URL of the Plugboard instance to call. |
| `PLUGBOARD_API_KEY` | empty | Client key used by the MCP server for chat and usage. |
| `PLUGBOARD_ADMIN_KEY` | empty | Admin key used by the MCP server for provider status, and by `scripts/setup_railway_providers.py`. |
| `PLUGBOARD_MCP_TIMEOUT` | `900` | Timeout of MCP chat calls, in seconds. |
| `MCP_PORT` | `8766` | Port of the MCP server in SSE or HTTP mode. |
| `SETUP_ANTHROPIC_API_KEY`, `SETUP_OPENAI_API_KEY`, `SETUP_GEMINI_API_KEY`, `SETUP_GITHUB_PAT` | empty | Keys that `scripts/setup_railway_providers.py` turns into providers. |


