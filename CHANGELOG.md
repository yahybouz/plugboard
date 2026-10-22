# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [4.1.1] — 2026-10-22

### Fixed

- Dashboard lists (provider groups, price review reasons, provider prices, the pricing grid, the portal's daily chart) are sorted with the locale, so accented names no longer land after `z`.
- The CLI warm-up and the boot-time internet probe can no longer be garbage-collected while they run, and no longer outlive a shutdown.
- The model-availability scheduler and the Ollama late-start watcher end cancelled when stopped, instead of swallowing the cancellation.
- Semantic cache: vectors with a near-zero norm are skipped instead of producing a meaningless similarity score.

### Changed

- Dashboard accessibility: every form label is tied to its control, and the live status regions are `<output>` elements.
- Dashboard background calls that fail now record the cause in the browser console (`debug` for an expected fallback, `warn` when the page shows an error) instead of discarding it. Nothing else changes for the user.

### Development

- SonarQube analysis: `make sonar-up`, `make sonar-scan`, `make sonar-issues`, `make sonar-down` against a local server (Docker, `127.0.0.1:9000`), and a GitLab CI `sonarqube` job, created only when `SONAR_HOST_URL` and `SONAR_TOKEN` are set. See [CONTRIBUTING.md](CONTRIBUTING.md).
- The dashboard end-to-end suite (`make test-e2e`) boots again: its isolated server gets a throwaway admin key.
- Dashboard JavaScript uses `let`/`const`, optional chaining and no nested ternaries; mechanical SonarQube code smells are cleared in Python.

## [4.1.0] — 2026-10-22

### Upgrading

Defaults that change, and how to keep the previous behaviour:

| Change | To keep the previous behaviour |
|---|---|
| Prices and model lists refresh every 6 hours instead of once a day. | `PLUGBOARD_PRICING_REFRESH_INTERVAL_H=0` (and `PLUGBOARD_MODEL_AVAILABILITY_INTERVAL_H=0`). |
| Prices come from several sources (OpenRouter, models.dev, LiteLLM, built-in table). | Nothing to do if `PRICING_SOURCE_URL` is set: only `external,known` are used. Otherwise `PLUGBOARD_PRICING_SOURCES=litellm,known`. |
| A third-party price that moves by more than 20 % waits for review and is not billed until applied. | `PLUGBOARD_PRICE_DRIFT_ACTION=apply`. |
| Prices edited by an admin are kept (`source: manual`) instead of being overwritten by the next update. | **Revert to automatic** on the row, or `POST /admin/pricing/{month}/revert`. |
| The models.dev catalog is downloaded with each price update. | `PLUGBOARD_CATALOG=off`. |
| An automatic refresh removes a model only after two refreshes without it; a manual **Discover** removes at once. | `PLUGBOARD_MODEL_REMOVAL_PASSES=1` removes after one refresh. |
| Calls are billed with the price of the provider that served them (after fallback). | Remove `models_by_provider` from the grid. Run `make billing-reconcile` to see the difference on last month's usage first. |

Prices already in the grid without a `source` (written by an earlier update or an earlier `PUT`) stay under the automatic sources, as before: a source may still update them, but a change above `PLUGBOARD_PRICE_DRIFT_PCT` now waits for review. To freeze one, edit it once (it becomes `manual`). `PLUGBOARD_PRICING_SOURCE_URL` must now be https (http only for localhost). Existing providers keep `auto_discover` off: their model lists only change as before. The dashboard Pricing page now saves only the rows you changed.

### Added

- **Provider catalog from [models.dev](https://models.dev)** (MIT): every OpenAI-compatible provider it lists (OpenRouter, OpenCode Zen, Moonshot/Kimi, Groq, DeepSeek, Mistral, xAI, Together, Fireworks, DeepInfra, Cerebras and many more) becomes a one-click preset, marked *community, unverified*. The catalog is fetched with the pricing scheduler, validated entry by entry, and the last good copy is kept when the download fails or shrinks by more than half. `PLUGBOARD_CATALOG=off` (or an empty `PLUGBOARD_CATALOG_URL`) turns the outbound request off; `PLUGBOARD_CATALOG_URL` points to a mirror; `PLUGBOARD_CATALOG_EXCLUDE` hides providers. A provider created from a catalog preset keeps the base URL it was created with: a later catalog change never redirects its key.
- `POST /admin/direct-providers/test` checks a key against a preset or custom endpoint before anything is created (`ok`, `ok_no_models`, `invalid_key`, `not_openai_compatible`, `unreachable`).
- `make catalog-check` lists the presets generated from the live catalog.
- **Prices merged from several sources**, model by model, in the order of `PLUGBOARD_PRICING_SOURCES` (default `external,provider_native,models_dev,litellm,known`): your own document, OpenRouter's published prices, models.dev, LiteLLM, then the built-in table. Each grid entry records its `source`; a source that does not answer leaves its entries unchanged, and prices outside 0 to 1000 USD per million tokens (or with an output/input ratio outside 0.1 to 20) are ignored. If `PRICING_SOURCE_URL` is set and `PLUGBOARD_PRICING_SOURCES` is not, nothing changes: `external` then `known`, with a startup warning. `PLUGBOARD_PRICING_SOURCE_URL` is the new name of `PRICING_SOURCE_URL`.
- **Large price changes are held for review**: a change above `PLUGBOARD_PRICE_DRIFT_PCT` (20 %) from a third-party source waits in `GET /admin/pricing/suggestions` and is not billed until applied (`POST /admin/pricing/suggestions/{id}/apply|reject`, or in bulk with `{"ids": [...]}`). `PLUGBOARD_PRICE_DRIFT_ACTION=apply` writes them directly.
- `PATCH /admin/pricing/{month}` sets some prices without sending the whole grid, and `POST /admin/pricing/{month}/revert` gives a model back to the sources.
- **Automatic updates every 6 hours** for prices and model lists (`PLUGBOARD_PRICING_REFRESH_INTERVAL_H`, `PLUGBOARD_MODEL_AVAILABILITY_INTERVAL_H`; `0` keeps the daily run). Providers with `auto_discover: true` (catalog presets) are refreshed through `GET /models`; CLI providers at most every `PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H` hours (24). A model leaves a list only after two refreshes in a row without it, a refresh that would drop more than half of a list is not applied, and `default_model` / `pinned_models` are never removed. `POST /providers/{id}/discover` goes through the same rules and accepts `?dry_run=true`.
- **Change journal and webhooks**: `data/catalog/changes.jsonl` records every automatic change; `models.changed` and `pricing.changed` (`status: pending|applied`) webhooks, at most `PLUGBOARD_WEBHOOK_MAX_PER_RUN` (50) per update, never for a first population.
- `GET /admin/auto-update/status` groups both schedulers, the catalog, pending suggestions and recent changes; `POST /admin/auto-update/run` runs both now (`409` with `running_since` while busy). Scheduler statuses gain `stale`, `last_success_at` and, for models, `last_changes`.
- **Prices read by a model** (off by default, `PLUGBOARD_LLM_PRICING=1`): for advertised models that no source prices, once a day a model reads the provider's pricing page and its findings wait in the review queue; nothing is billed until applied. Hardened page fetch (https, exact host, pinned public address, no environment proxy, 2 MB), strict JSON answers, a daily budget (`PLUGBOARD_LLM_PRICING_MAX_USD`, 0.50) kept across restarts, calls billed to `plugboard-internal`. `GET /admin/pricing/llm/status`, `POST /admin/pricing/llm/run`.
- **Dashboard**: provider search with built-in and catalog groups, **Test connection** before creating a provider (with **Save anyway**), **Discovering…** state, `auto_discover` and pinned models in the provider form, discovery preview before removals; Pricing page with the automatic-update status, the **Needs your review** queue (apply or reject, one by one or in bulk), a searchable paginated grid with source filter, **Missing price only**, provider prices per model and **Revert to automatic**; an **Automatic updates** card and the recent changes on Monitoring; the price key and source of recent requests on Usage & Billing.
- **Prices per provider**: a grid can hold reseller prices in `models_by_provider` (filled automatically for OpenRouter, OpenCode, DeepInfra, Together and Fireworks when they differ from the vendor price). A call is priced with the provider that served it, after fallback, including streaming. Usage entries record `price_key` and `price_source`; admin keys get an `X-Price-Key` header. `make billing-reconcile` compares last month's costs with and without provider prices.

### Changed

- Prices and model lists are refreshed every 6 hours instead of once a day. Set `PLUGBOARD_PRICING_REFRESH_INTERVAL_H=0` to keep the daily run. `PRICING_REFRESH_INTERVAL_H` and `MODEL_AVAILABILITY_INTERVAL_H` are renamed with the `PLUGBOARD_` prefix; the old names still work.
- A price edited by an admin (dashboard, `PATCH` or `PUT`) is marked `source: manual` and is no longer overwritten by the next update. `PUT` only marks the rows whose price changed.
- `PUT /admin/pricing/{month}` now checks every row: both prices must be numbers between 0 and 1000 USD per million tokens, and the month must look like `YYYY_MM` (422 otherwise). Price edits on a grid file that cannot be read answer 503 instead of replacing it.
- `GET /usage/me` (client keys) does not return the price key or source of each entry; admins see them in `/admin/usage`.

### Removed

- The dashboard **Connections** tab. A connected OAuth account (GitHub Models, GitHub Copilot) is now shown in the provider form when you pick its preset, with **Reconnect** and **Disconnect**. `GET /admin/oauth/connections` and `DELETE /admin/oauth/{source}` are unchanged.

### Fixed

- Codex CLI sessions failed inside the hardened container (`bwrap: No permissions to create a new namespace`). The Linux sandbox profile now grants the CLI's read-only runtime roots (`:minimal`) while masking `/etc`; `deploy/ovh` ships a service-scoped AppArmor profile, a pinned Docker-default seccomp with the seven syscalls bubblewrap needs, an installer and an offline preflight. Host user-namespace sysctls, `cap_drop: ALL` and `no-new-privileges` are unchanged.
- In the Add Direct Provider form, a slow check for the previously selected preset could overwrite the status (and buttons) of the preset now selected.
- The client portal loaded Chart.js from a CDN without an integrity check; it is now vendored (`static/vendor/chartjs`, 4.4.1, MIT, verified against the npm tarball integrity).
- With `CORS_ORIGINS` set, rate-limit (429) and body-size (413) answers had no CORS headers, so browsers reported a CORS error instead of the real status: CORS is now the outermost middleware.
- Dependency security updates: `aiohttp` 3.14.5 and `certifi` 2026.10.19 (pip-audit findings).
- `deploy/ovh`: the gateway reaches the host's loopback-only Ollama through an internal Docker network and a socket-activated proxy bound to that bridge only; nothing listens on public or Tailscale addresses.
- The setup steps of the built-in presets were in French; they are now in English.
- On narrow screens wide tables pushed the whole dashboard sideways; they now scroll inside their frame.
- Usage was recorded against the provider named in the request even when another provider served it after a fallback or a circuit-breaker rewrite.
- Price lookups no longer re-read and scan up to twelve monthly grids on every call: grids are cached by file modification time, with an index and a cache of misses.
- Providers whose base URL already names the API version (`.../v1`, `.../v1beta/openai`, `.../openai/v1`) were called at `.../v1/v1/chat/completions`. The router now adds `/v1` only to bare hosts.

## [4.0.0] — 2026-10-18 — first public release

First public release of Plugboard, a self-hosted, OpenAI-compatible LLM gateway. The development history before this release is not published; this entry summarises what the release contains.

### Upgrading an existing install (LLM Enabler 3.x)

This release renames the project and changes several defaults. Before pulling it into an existing checkout:

0. **Start from a clean working tree**: `git status` must show no modified tracked file. Commit or copy aside local changes first; files removed by this release (internal docs, monthly pricing snapshots under `data/pricing/` that were tracked) disappear on pull.
1. **Back up local files that are no longer tracked**: `cp -a data data.bak`, and move the update registry: `cp docs/registry.json data/registry.json` (or set `PLUGBOARD_REGISTRY_PATH`). `git pull` stops if these files have local changes; restore them from the backup after the pull. Until moved, the registry is still read from `docs/registry.json`, with a warning.
2. **Start command**: the package is now `plugboard`. Use `python run.py` (or `uvicorn plugboard.app:app`); `llm_enabler` imports and `python -m llm_enabler...` no longer work.
3. **Environment**: `LLM_ENABLER_*` names keep working (deprecation warning); rename them to `PLUGBOARD_*` when convenient. A new name always wins over the old one.
4. **`ADMIN_KEY` is required**: the server refuses to start without it, or with fewer than 16 characters, unless `PLUGBOARD_DEV=1`. `LLM_ENABLER_DEV` is not carried over: remove it from production `.env` files.
5. **Dashboard**: the session cookie is renamed, so admins sign in once more. Auto-login only applies to a local install (`HOST=127.0.0.1`, or `PLUGBOARD_LOCAL_AUTO_LOGIN=1`).
6. **`/v1/models` lists every discovered model**; set `PLUGBOARD_MODEL_FILTER=curated` to keep the previous short list.
7. **Gemini OAuth**: set `GEMINI_OAUTH_CLIENT_ID` / `GEMINI_OAUTH_CLIENT_SECRET` if you sign in to Gemini from the dashboard or rely on automatic Gemini CLI token refresh.
8. **Integrations**: webhooks send `X-Plugboard-Event` / `X-Plugboard-Signature` (the previous header names are still sent for now); Prometheus metrics are named `plugboard_*` instead of `llm_enabler_*`; the Python SDK is `plugboard_sdk` with `PlugboardClient` (`LLMEnablerClient` stays as an alias); the MCP server is named `plugboard`. New client keys start with `pb_`; existing `lle_` keys keep working.
9. **Docker**: `docker compose` builds the slim `api` image (no provider CLIs); build the `full` target for CLI subscriptions.
10. **CLI providers get an allowlisted environment**: `claude`, `codex` and `gemini` no longer inherit the whole server environment. Credentials they need beyond their own (Bedrock or Vertex auth for `claude`, a custom CA bundle...) must be listed in `PLUGBOARD_SUBPROCESS_ENV_PASSTHROUGH`, comma-separated.
11. **Python SDK**: the package is now `plugboard_sdk` (`from plugboard_sdk import PlugboardClient`); `LLMEnablerClient` remains as an alias inside it.

### Gateway

- OpenAI-compatible API: `POST /v1/chat/completions` (with server-sent-event streaming), `GET /v1/models`, `GET /v1/models/benchmarks`, `GET /v1/models/{id}/info`, and `/v2` aliases.
- Models addressed as `<provider-id>:<model>`; retries with backoff on `429`/`5xx`, fallback across providers for unprefixed model names, latency- and error-aware ordering, per-model circuit breaker.
- Optional server-side chat sessions (`session_id`) and a global concurrency cap for chat requests.

### Providers

- API-key presets for Anthropic, OpenAI, Google Gemini and GitHub Models; custom OpenAI-compatible endpoints; Ollama detected and registered automatically, with model management from the dashboard.
- Subscription access through the official Claude Code, Codex and Gemini CLIs (run as subprocesses), through credentials stored by the Codex, Gemini and GitHub CLIs, and through GitHub's device flow. These modes may conflict with the providers' terms; see the README.
- Model discovery, daily account-aware catalog refresh, connectivity probes, quarantine of unavailable models, internet-access check.

### Clients, billing and pricing

- Per-client keys (`pb_` prefix; earlier `lle_` keys still accepted) stored as SHA-256 hashes, with monthly USD budget, requests-per-minute limit, allowed models and organisation grouping; revocation and rotation.
- Usage and cost recorded per request, client, key and month; `billing` object and cost headers in responses; CSV export; client self-service portal and `/v1/me`.
- Monthly pricing grids refreshed automatically by an in-process scheduler from a public price list or a custom JSON source, inherited from the previous month, editable in the dashboard; past months are never modified.

### Caching

- Exact-match response cache scoped per client; optional semantic cache with an optional embedding layer; cache replay for streaming CLI requests.

### Operations

- Admin dashboard: overview, keys, usage, providers, OAuth connections, pricing, playground, Ollama, monitoring, jobs.
- Health endpoints, JSON and Prometheus metrics, provider watchdog, health history, admin audit log, alert webhook for failing providers and spending, signed event webhooks, JSON logs with request IDs.
- MCP server with chat, model, usage and provider-status tools, and an update registry that tells client projects what to change.
- Docker image with the provider CLIs preinstalled; Docker Compose setup bound to `127.0.0.1`; Makefile targets for setup, development, tests and secret scanning.

### Security

- Dashboard auto-login limited to a local install opened in the local browser; `ADMIN_KEY` login with brute-force lockout and optional TOTP MFA elsewhere.
- Provider credentials, OAuth credentials and the MFA secret encrypted at rest; strict tenant isolation of caches by default; Content Security Policy and security headers; body size limits; per-IP and per-key rate limits; optional provider domain allowlist.
- Startup refused when `API_KEY` or `ADMIN_KEY` contains comment text copied from `.env.example`.
- CLI providers (codex, gemini, claude) run a client's prompt in isolation: an allowlisted environment instead of a copy of the server environment (`ADMIN_KEY`, `API_KEY` and `ENCRYPTION_KEY` never reach them; `PLUGBOARD_SUBPROCESS_ENV_PASSTHROUGH` for extra names), a private `0700` spawn directory, a codex permission profile that denies the rest of the disk, no user configuration, MCP servers, extensions or session history. gemini runs from a private `GEMINI_CLI_HOME` created per call: the operator's settings, memory, skills and MCP servers do not apply, and no gemini tool is exposed.
- Startup warning when `.env` is readable by the group or other users; `make init` creates it with mode `0600`.

### Configuration

- Environment variables use the `PLUGBOARD_` prefix; the previous `LLM_ENABLER_` prefix is still accepted and deprecated.
