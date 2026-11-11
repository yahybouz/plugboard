# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **Log out everywhere**: the admin can now sign out every dashboard session at once (stolen cookie, lost phone, shared computer) without changing MFA or `ADMIN_KEY`. `POST /auth/admin/logout-all` (admin only, MFA rules included) revokes every session, the caller's included, forgets every browser remembered for local auto-login (otherwise a remembered browser would be signed in again on its next visit), records `auth.logout_all` in the audit log and clears the session cookie; when the session store or the remembered browsers cannot be rewritten it answers `503` and records nothing. The dashboard sidebar gets a **Log out everywhere** button that asks for confirmation first. A completed `ENCRYPTION_KEY` rotation (`make rotate-encryption-key`) also signs out every session; a dry run or a refused rotation signs out nobody, and if the session store cannot be rewritten the rotation still succeeds and tells the operator to use the button once the server runs again ([guide](docs/guide/monitoring.md#admin-api-with-mfa)).

## [4.5.1] — 2026-11-10

### Security

- **HTTP provider errors no longer reach client keys**: when an HTTP provider answered an error other than 429 (400, 401, 403, 404, 500, 503...), `POST /v1/chat/completions` (and `/v2`), streamed or not, gave every caller `[provider] HTTP <status>: <body>`, the provider's own text, which can name its account or organisation; a body that contained the word "timeout" even came back as `503`. A client key now gets a message written by the gateway with an `error_ref` to find the full error in the gateway log: `400` when the provider said the request itself is at fault (`413`, or a `400` or `422` whose error type or code names the request: `invalid_request_error`, `context_length_exceeded`), `502` otherwise. The status alone is not enough: a provider also answers `400` for its own key or region (Gemini: `INVALID_ARGUMENT` "API key not valid", `FAILED_PRECONDITION`), which stays a `502` and never tells the client to change its request. The admin (dashboard Playground, admin key) still gets the provider's message, with the same status and the `error_ref`. Fallback to the other candidate providers is unchanged.
- **Network errors no longer give the provider's address to client keys**: when an HTTP provider could not be reached, `POST /v1/chat/completions` (and `/v2`) answered `502` with `[provider] network error: Cannot connect to host <host>:<port> ...`, and a stream with the bare aiohttp text; a connection timeout in a stream came back as `503` with the provider's full URL. For a local Ollama or a private endpoint, that is an internal address. A client key now gets `502` with a message written by the gateway and an `error_ref` (a stream timeout: `503` with `[provider] timeout`, as without streaming); the admin still gets the detail, with the `error_ref`.

### Changed

- **`allowed_models` accepts the ID shown by `/v1/models`**: an entry may now be the published ID of a CLI model (`claude-code:<model>`, `Codex:<model>`, `Gemini:<model>`), next to the native ID and the owner-qualified ID (`chatgpt-via-codex-cli:<model>`). Since 4.3.1 such an entry granted nothing, although it is the ID `/v1/models` shows: `403` on chat, and in `/v1/models` the model was flagged `account_compatible: false` up to 4.4.1, then left out of the key's model lists in 4.5.0. The match stays exact and covers that one model: a bare alias (`Codex:`), a prefix, a wildcard, another case or added whitespace still grant nothing, and when a provider's ID is the alias itself (same case), the entry designates that provider, never the CLI. **A request without a model name is never granted by a limited key**: for a CLI registered without a default model, `Codex:` or `chatgpt-via-codex-cli:` resolves to an empty model name, and since 4.3.1 an entry `chatgpt-via-codex-cli:` (provider ID alone) or `""` let it through: Plugboard then ran the CLI with `--model` set to the default model it hard-codes for that kind of CLI, a model the key never named. Such a request now gets `403` on chat, streamed or not, and `forbidden_model` from `/v1/preflight`, without running the CLI. A key without `allowed_models` is unchanged. **Keys already written with published IDs get access to those models again on upgrade, without migration: review them before deploying** ([details](docs/model-access-correction.md#published-model-id-as-a-third-spelling)).

### Fixed

- **Claude high load, model block and server throttling answered `429` quota**: claude sets `error: "rate_limit"` on more than the account's usage limit: on its Opus/Fable high-load switch ("is experiencing high load") and a per-model block, which it raises itself without an HTTP status, and on an API 429 it reports as "Server is temporarily limiting requests (not your usage limit)". The gateway read all of them as an exhausted quota and answered `429` "quota or rate limit exhausted" when no other provider could serve. They are now a provider failure: the other candidate providers are tried, then `502`. A usage or spend limit (`api_error_status: 429` and an `API Error` text or a usage-limit message) still gives `429`.
- **Circuit breaker blind to provider-qualified models**: `forward()` recorded failures and successes under the native model name but read the breaker under the model string the client sent, so a model asked as `provider-id:model` never saw its own failures. The native circuit it opened never closed again (`/admin/circuits` showed it open for good), and the LLM pricing pass, which reads `provider-id:model`, kept choosing a provider whose calls were failing. The breaker is now read and recorded under one key per route, `provider-id:model`; an unqualified model counts as open only when every provider it is tried on is. A request to an open circuit is still sent, as before.
- **Open circuit pinned other callers to one provider**: when every route of a model was open, the fallback rewrite looked for the `FALLBACK_MAP` entry as a substring of the native name. For a model whose fallback is a prefix of its own name (`gpt-5.5-pro` → `gpt-5.5`, `o3-pro` → `o3`, `qwen3.5` → `qwen3`…) it found the same model and narrowed the request to its first provider, so for 60 s no other provider was tried, even one that answered. Three requests refused for their own content (HTTP 400 "prompt is too long") were enough to trigger it for every caller. The rewrite now needs the model suffix to equal the fallback and no longer narrows a request to the model whose circuits are open.
- **A hung `<cli> --version` kept running**: the version check run at start-up (CLI warm-up), by the health watchdog and by the dashboard's CLI check gave up after its delay but left the CLI and its children running. They are now started in their own process group and killed with it when the delay runs out, as for a chat request. The check still gives up within two seconds of its delay when a child that left the group (a daemon) keeps its output open, so it can no longer stall the watchdog.
- **Exhausted TypeSafe quota answered 502 on `/v1/decisions`**: when TypeSafe answered HTTP 429 to a Jev decision, the client got the generic `502 Invalid decision engine response`. It now gets `429` as on `POST /v1/chat/completions` since 4.5.0: a reason written by the gateway naming `typesafe`, and TypeSafe's delay in `Retry-After` and `retry_after` when it gave one (at most 7 days). TypeSafe's body is never read nor returned, and nothing is billed. Other TypeSafe error statuses still answer `502` ([decisions](docs/guide/decisions.md)).

## [4.5.0] — 2026-11-10

### Security

- **CLI provider errors no longer reach client keys**: a failed CLI provider (claude, codex, gemini, agy) answered every caller with its stderr, process id, exit code and its own message, which can quote local paths, account names or tokens. A client key now gets `502` with a generic `detail` and an `error_ref` to find the full error in the gateway log; the admin (dashboard Playground, admin key) still gets the details.
- **Local auto-login needs the operator's secret**: on top of the loopback and browser checks, the dashboard signs in only a browser that opens the single-use token the server writes at each start to `data/.login_token` (0600; `make dev` opens the link), and then remembers that browser for 30 days with a cookie sent only to `/login` (digests in `data/local_devices.json`). A same-machine container or VM, a forwarder in front of a loopback-bound install, or a local process forging headers no longer gets an admin session. New refusal reasons `no_local_secret` and `bad_token` ([troubleshooting](docs/guide/troubleshooting.md#dashboard-auto-login-does-not-happen)).
- **MFA step-up**: once a TOTP secret is enrolled, disabling it (`DELETE /admin/mfa`) or rotating it (`POST /admin/mfa/setup`) needs a current code from it (`X-Plugboard-Admin-TOTP` header, or `current_code` in the setup body), even from an open admin session; before, any session, an auto-login one included, could remove the second factor. A code already checked by the request's own authentication counts ([guide](docs/guide/monitoring.md#admin-api-with-mfa)).
- **Model lists showed every model to a limited key**: a key whose `allowed_models` limits it got the whole catalogue from `/v1/models` (the models it may not call only flagged `account_compatible: false`), `/v1/models/benchmarks`, `/v2/models` and `/v2/models/benchmarks`, and the curated profile of any model from `/v1/models/{model_id}/info`. The four lists now hold only the models `/v1/chat/completions` would accept from that key, under the same exact-match rule, each with `account_compatible: true`; for a model it may not call, `/info` gives the answer of an unknown model (`200`, `info_source: "derived"`). A key without `allowed_models` and the admin still see everything.

### Added

- **Ticket board at `/suivi`**: a Kanban of Plugboard tickets behind the admin session, linked from the dashboard sidebar. Status by list (phone and keyboard friendly), a reason required for *Bloqué*/*Abandonné*, a proof required for *Terminé*, history of moves in `data/suivi.db`, `suivi.*` audit rows. **Importer** turns Superpowers plans and specs and `TODOS.md` into tickets, read-only and without duplicates ([guide](docs/guide/monitoring.md#ticket-board-suivi)).
- **`ENCRYPTION_KEY` rotation** (`make rotate-encryption-key`, `scripts/rotate_encryption_key.py`): re-encrypts provider tokens, OAuth credentials and the admin MFA secret under a new key, after a backup in `data/backups/rekey-<time>/`. All or nothing: if one secret cannot be read with the current key, nothing is written. Can be run again after an interruption. The new key is generated into a 0600 file and never printed; `data/.encryption_key` is replaced when `ENCRYPTION_KEY` is not set ([guide](docs/guide/configuration.md#changing-encryption_key)).

### Fixed

- **Clients and keys guide described `allowed_models` as substrings**: the guide still said a request is allowed when the model string contains an entry, with a provider-prefix example that has allowed none of the models it covered since 4.3.1. It now states the exact-ID rule, shows full IDs, and says how to find and replace a key with partial entries ([guide](docs/guide/clients-and-keys.md#allowed-models)). Documentation only; the rule itself is unchanged.
- **A CLI error at exit 0 was served as the answer**: claude, codex and gemini can end a run in error and still exit 0 (claude's `result` with `is_error` or an `error_*` subtype, codex `turn.failed`, a gemini `error` object next to a `response`). The text printed around that error (an `is_error` result, a partial answer) was returned as `200`, billed to the key, written to the response cache and stored as the session's assistant turn. Such a run is now a provider failure, as on a non-zero exit: the other candidate providers are tried first, then the client gets `502`, or `429` when the run ended on a quota. A quota is claude's structured sign (`error: "rate_limit"` on an assistant event or `api_error_status: 429` on the result, now read on a non-zero exit too; claude 2.1.321 also sets it on its Opus/Fable high-load switch and on a per-model block) or a quota named by the error that ended the run (by stderr when the CLI gave no message), never by a codex reconnect the CLI answered after. Nothing is billed, cached or recorded, streamed requests included. Without a message from the CLI, its stderr is the logged reason, as on a non-zero exit.
- **An abandoned chat blocked every model**: with `PLUGBOARD_MAX_INFLIGHT_CHAT`, a request whose client hung up (model switch, client timeout) kept its slot until the provider answered, up to 900 s for a CLI, so every other call got `503`; the CLI kept running, and its late answer was billed, cached and recorded in the session. The gateway now watches the client while the provider works, on `/v1/chat/completions` and `/v2/chat/completions` (streamed or not) and `/v1/preflight` (the rest of the batch is skipped): on a hang-up it kills the CLI and its children or cancels the HTTP request, bills, caches and records nothing, and frees the slot at once. A stream cut after its first bytes is still billed for what was received. The `503` of a full cap keeps `Retry-After: 5` and now says that the limit of concurrent chat requests is reached. `PLUGBOARD_MAX_INFLIGHT_CHAT` and `PLUGBOARD_INFLIGHT_WAIT_S` keep their meaning ([clients and keys](docs/guide/clients-and-keys.md#how-limits-are-enforced)).
- **Temporary directories left in /tmp**: every gateway process left its `plugboard-spawn-*` directory (CLI working directory) and every test run its `plugboard-tests-*` data directory and a `spec-v7b-*` directory (1.5 MB); all are now removed at exit, and pytest keeps `tmp_path` directories only for failed tests.
- **Ollama embeddings behind an authenticating proxy**: `POST /v1/embeddings` called the engine's `/api/tags` and `/api/embed` without credentials, so an Ollama reachable only through a proxy that asks for a bearer token answered `502 Embedding engine error (HTTP 401)` while chat worked. Both calls now carry `PLUGBOARD_OLLAMA_TOKEN` when the engine is exactly `OLLAMA_URL`; no other engine gets it, and the route still never contacts an address supplied by the caller ([embeddings](docs/guide/embeddings.md)).
- **Exhausted provider quota answered 502**: when the last provider tried answered HTTP 429, or a subscription CLI reported an exhausted quota or rate limit (gemini/agy `Resource has been exhausted`, `RESOURCE_EXHAUSTED`, `rateLimitExceeded`; codex `You've hit your usage limit`, `exceeded retry limit, last status: 429`; Claude Code `You've hit your limit`, `usage limit reached`, `5-hour limit reached`, `You've reached your weekly limit`; `rate_limit_error`, `insufficient_quota`), `POST /v1/chat/completions` returned a generic `502` (with the provider's raw body for HTTP providers), and a Claude Code limit notice on exit 0 was even served and billed as the answer. It now returns `429` with a reason written by the gateway: provider, quota or limit exhausted, and the delay when known, in `retry_after` and `Retry-After` (the provider's header, or the time until the hour codex prints, read in the gateway's local time; at most 7 days). The quota is read from the CLI's own error (stderr only when the CLI gives none; on exit 0, only an event that ends the run, so a codex reconnect followed by an answer is served), never from the model's output, and a full disk (`Disk quota exceeded`) is not one. Client keys never get the upstream text or the CLI's process details; the admin keeps them. A non-streamed request is still offered to the other candidate providers first; a streamed request to an HTTP provider has no fallback.
- **Ollama page offered cloud models while Ollama was offline**: the cloud catalogue showed every model as *available* with an *Add* button although adding one goes through the local Ollama. While Ollama does not answer, the page now says so and offers no *Add*.
- **Ollama behind an authenticating proxy**: the Ollama page, the `ollama` provider and its checks called `OLLAMA_URL` without credentials, so an Ollama reachable only through a proxy that asks for a bearer token showed *Offline*. New `PLUGBOARD_OLLAMA_TOKEN`: sent only to `OLLAMA_URL` (never to the public ollama.com catalogue) and stored, encrypted, on the `ollama` provider ([configuration](docs/guide/configuration.md)).
- **A failed model discovery says why**: *Discover* on a provider that kept no model answered only `Could not discover models`. It now says how many models were tested and the first reasons. A signed-out `claude` or `codex` CLI is named as such, with the command to sign in (`claude` then `/login`, `codex login --device-auth`), instead of the CLI's first log line; the refresh report keeps the reason of every failed model (`dropped[].detail`).
- **Semantic cache answered from another conversation**: only the last user message was compared, so the same question under another system prompt, after another history, with other images or other `tools`/`response_format` got the stored reply. Only that last question is now compared approximately; everything around it must be identical. Entries stored before are no longer served.
- **Response cache served another question's answer**: the cache key covered only `temperature`, `max_tokens`, `top_p` and `stop`, so the same messages with other `tools`, `tool_choice`, `response_format`, `seed`, `n`, `reasoning_effort` or penalties got the first cached reply. Every request field now counts, except `stream`, `stream_options` and `cache`.
- **Admin MFA with an unreadable secret** (key changed without rotation): codes are refused with a clear log line instead of an HTTP 500.
- **Usage of an interrupted stream**: a streamed answer cut short by the client or by an upstream read error is now billed from what was received; before, it recorded no usage and escaped `monthly_limit_usd`.
- **Fallback on a network error**: a refused or dropped connection, a truncated body or a non-JSON answer from a provider now hands the request to the next candidate instead of returning 502 at once. A provider-prefixed model keeps its single route.

## [4.4.1] — 2026-11-08

### Fixed

- **Login page on phones**: the page now declares a mobile viewport, so iPhone and Android show it at its real size instead of a zoomed-out desktop page. The card fits the screen width with adaptive padding and stays scrollable when the keyboard opens (`100dvh`). Fields use a 16 px font, which stops iOS from zooming on focus, and fields and button are at least 44 px tall on touch screens. Long error messages wrap instead of overflowing.

## [4.4.0] — 2026-10-29

### Added

- **Local model downloads switch** (Ollama page, `GET`/`PUT /admin/ollama/settings`, `PLUGBOARD_LOCAL_MODEL_DOWNLOADS`): off, only Ollama cloud models (`:cloud`, `-cloud`, official library only, layers capped at 64 MB) can be pulled; local pulls get 403. The admin choice is saved and wins over the variable.
- **Disk guard on Ollama pulls**: the registry size plus `PLUGBOARD_OLLAMA_MIN_FREE_GB` (default 5) must fit in the free space of `PLUGBOARD_OLLAMA_DISK_PATH` (507 otherwise); a pull of unknown size is stopped and its partial model deleted before it crosses the margin. Model names are validated (422).
- **Ollama Cloud catalogue** on the Ollama page (`GET /admin/ollama/cloud-catalog`) with one-click add; no inference probe.
- **Embedding concurrency cap** (`PLUGBOARD_EMBEDDING_CONCURRENCY`, default 2): extra calls get 429 instead of loading a small host.
- **MCP embeddings**: tools `embed` and `list_embedding_models`, and client route `GET /v1/embeddings/models`.

- **`POST /v1/embeddings`** (OpenAI-compatible), client keys only, served by a registered local Ollama engine (`sovereign`, loopback `base_url`) for the exact identities listed in `PLUGBOARD_EMBEDDING_MODELS` (empty: closed). No owner guessing, no chat fallback, no caller-supplied destination; the key's `allowed_models`, budget and rpm limit apply. Bounded input (1–256 strings, ≤ 16 384 characters each, 2 MiB), `truncate: false`, vectors checked (count, dimension, finite, non-zero) and the engine's model digest returned. Usage rows `embedding:<model>` at cost 0 (`price_source = local:embeddings`); logs carry the request id, never the text. Chat routes are unchanged ([guide](docs/guide/embeddings.md)).

### Changed

- OVH deployment: image `plugboard:private-full-v4.4.0`, `PLUGBOARD_LOCAL_MODEL_DOWNLOADS=0`, `PLUGBOARD_OLLAMA_MIN_FREE_GB=5`, `PLUGBOARD_EMBEDDING_MODELS=ollama:qwen3-embedding:0.6b`.

## [4.3.3] — 2026-10-28

### Fixed

- **Antigravity (agy) account catalog**: the model refresh now reads the signed-in account's `agy models` list even when agy is not in `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS`, and publishes every listed model without an inference probe. New account models (for example a new model family) appear without spending subscription quota; before, they were dropped whenever the availability probe failed, including on an exhausted quota, and repeated failures quarantined them. The refresh report adds `trusted_account_catalog`; `verified_count` stays 0 for this discovery-only path.
- A listed model is not marked as working: the quarantine state is neither written nor cleared, and an already quarantined model stays hidden. A failed or empty `agy models` answer, or the preset/pricing fallback, keeps the saved models and default and adds nothing unless probes are enabled for agy. Removals still go through the usual two-pass rule. Claude, Codex and Gemini CLI refreshes are unchanged.
- OVH deployment (`deploy/ovh`): model and pricing jobs enabled, `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` set to empty (no quota-consuming probes), image `plugboard:private-full-v4.3.3`.

## [4.3.2] — 2026-10-28

### Fixed

- **Antigravity (agy) token usage**: the counts agy reports (`input_tokens`, `output_tokens`) are now used for the response `usage`, the final SSE usage chunk and the usage ledger (`usage.db`), instead of a tiktoken estimate of the visible text (a 4-token answer to an 11 510-token prompt was billed as about 10 tokens). A reported 0 is kept as a count. Valid `cache_read_tokens` and `thinking_tokens` appear as `prompt_tokens_details.cached_tokens` and `completion_tokens_details.reasoning_tokens` and are never added to the total.
- A missing or malformed agy usage block (bool, string, fractional, negative or null counter, non-object usage), or a `total_tokens` different from input + output, is not trusted: the tokens are estimated as before. Other CLIs and HTTP providers are unchanged.
- Existing usage rows and cached responses keep their earlier estimates; no historical count is rewritten or invented.

## [4.3.1] — 2026-10-27

### Security

- **Breaking for partial `allowed_models` entries**: a restricted key now matches the effective model identity exactly — the native model ID or `<provider>:<native>`. Substring, prefix and family matching no longer grant access (`"fake"` or `"test-provider"` no longer allow `test-provider:fake:model-v1`). An empty list stays unrestricted. Rewrite partial entries as explicit IDs before upgrading ([details](docs/model-access-correction.md)).
- The policy is checked on every provider actually dispatched, including failover and circuit-breaker fallbacks, for streaming and non-streaming calls, before any upstream call; a refused candidate returns 403. `/v1/models` `account_compatible` and `/v1/preflight` (`forbidden_model`) use the same check.
- Restricted keys bypass the response cache, whose entries do not record the serving identity.

### Fixed

- **Antigravity (agy) in the `full` image**: the gateway image now ships the official agy CLI (SHA512-pinned release archive), so the *Antigravity (via agy CLI)* provider can use a Google subscription once signed in (`docker exec -it <gateway> agy`; session kept in `/home/app`). An unsigned agy now returns an explicit "not signed in" error instead of "invalid JSON". API-key access to Gemini stays on *Google Gemini Direct* (agy does not read `GEMINI_API_KEY`).

## [4.3.0] — 2026-10-24

### Known limitations

- Laya (local decision engine) is qualified on macOS only; Linux/OVH (install, memory) is checked after deployment.

### Fixed

- Jev `score` answers: TypeSafe rounds the score and each probability to two decimals separately, so the score can differ from the expectation of the probabilities by the rounding (e.g. score 1.0, probabilities 0.99/0.01, expectation 1.01). Those answers were rejected as 502 (and still billed); the check now allows the worst-case rounding only (half a hundredth per value), and a sum of rounded probabilities off by the same margin. Inconsistent answers are still refused. Found by the first live test against `api.typesafe.ai` (jev-1.14.0): 1 score answer in 4 was refused.

### Security

- **Breaking with admin MFA enforced**: the `ADMIN_KEY` alone is refused (401) on admin routes (`/admin/*`, `/providers*`, `/metrics*`, `/docs`, dashboard); add a fresh code in `X-Plugboard-Admin-TOTP` or use a session ([admin API with MFA](docs/guide/monitoring.md#admin-api-with-mfa)). Without enforced MFA nothing changes. TOTP codes are single-use. Admin sessions are now recorded server-side (`data/admin_sessions.json`): logout revokes the session, enrolling, rotating or disabling MFA revokes all of them, and a session opened without a code stops working once MFA is enforced. Existing sessions end at upgrade: sign in again.
- MCP server: `--sse` and `--http` refuse to start without `PLUGBOARD_MCP_TOKEN` (32+ characters), require it as bearer, listen on `127.0.0.1` by default (`MCP_HOST`), keep DNS-rebinding protection (`PLUGBOARD_MCP_ALLOWED_HOSTS`, `PLUGBOARD_MCP_ALLOWED_ORIGINS`), and never load `PLUGBOARD_ADMIN_KEY` (`provider_status` refuses). stdio is unchanged.
- OAuth broker: saving or deleting a credential (connect, refresh, `DELETE /admin/oauth/{source}`) drops the cached tokens of that source at once, including the derived Copilot token; a token without expiry is re-read after 5 minutes at most. Disconnecting keeps the broker provider records (the endpoint description said otherwise).
- OVH compose: the gateway no longer bind-mounts the host account's `.codex` and `.claude`; CLI logins live in the `cli-home` volume. [Migration](deploy/ovh/README.md#cli-homes): log in again inside the container.
- Residuals: admin sessions are bound to the MFA generation, so an MFA enrolment, rotation or disable refuses older sessions even if `admin_sessions.json` is read-only (the write error is still returned); the `/admin/mfa/setup` code is consumed; MCP `--sse`/`--http` also drops the admin key that importing the package reloads from `.env`; a Copilot token derived during a GitHub save or disconnect is not cached.
- MCP `--sse`/`--http` keep only their own variables (`PLUGBOARD_URL`, `PLUGBOARD_API_KEY`, `PLUGBOARD_MCP_*`, `MCP_HOST`, `MCP_PORT`, data paths, system variables): `ADMIN_KEY`, `ENCRYPTION_KEY`, provider keys and `PLUGBOARD_JEV_API_KEY` are dropped, from `.env` too.
- With `PLUGBOARD_ADMIN_MFA_REQUIRED=1`, an unreadable `admin_mfa.json` refuses admin access (401) instead of turning MFA off.
- An OAuth refresh in flight no longer brings back a credential disconnected or replaced meanwhile.

### Removed

- OpenClaw connector: environment bootstrap (`OPENCLAW_URL`, `OPENCLAW_KEY`, `OPENCLAW_MODEL`, `OPENCLAW_NAME`), wrapper model encoding, connector aliases and watchdog launcher execution. Those variables are ignored. Recognizable legacy provider IDs/markers and requests are refused as an unsupported removed connector, before transport and without fallback. Existing usage, historical price lookup, invoices and attribution notices are preserved.

### Changed

- Configure direct providers explicitly and update callers and ACLs manually to their advertised native IDs; see [migration](docs/guide/openclaw-removal.md). Claude Code, Codex, Ollama and generic compatible HTTP providers remain available. Typed decisions remain Jev through TypeSafe API and hosted Laya English/multilingual, with Kev absent. No release or deployment qualification is implied.
- Typed decisions are recorded in the usage ledger (`decision:<id>`) and count towards the key's monthly limit. Jev is billed at TypeSafe's official price, $0.042 per million input tokens (output free), from the `usage` its answer reports; missing or invalid usage is billed at its 64,000-token default context (`billing.status: "estimated"`). Laya is free (`billed`, 0). `GET /v1/decisions/models` lists each engine's `pricing`. Decisions are never refused with 402. A Jev call TypeSafe answered but whose answer is rejected (502), or that timed out after it was sent (504), is billed: reported usage, else the ceiling; other failed calls, and a call the 45-second API deadline ends before TypeSafe answers, are not recorded. `PLUGBOARD_JEV_PRICE_PER_CALL_USD` is removed: ignored with one warning if still set.
- Built-in prices from the vendors' official pages (2026-10-24): Claude Fable 5.1, Opus 5.5, Sonnet 5.5, Haiku 5.5 and the agy `-medium` variants, `gemini-4-argon-*`, `gemini-3.8-flash-*`, `gpt-4o-mini`. `gpt-oss-120b-medium` and `meta-llama-3.1-405b-instruct` stay unpriced (no official price): 402 for budgeted keys.
- Official prices go through the pricing scheduler: a dated official table (`plugboard/official_prices.py`, new source `official`, ranked right after `external`) is written into each month's grid at every refresh, with no fetch, so it applies when every network source fails. Google's increases apply by themselves: `gemini-4-argon-*` 4/20 from the 2026-12 grid, `gemini-3.8-flash-*` 1.50/7.50 from the 2027-01 grid (`pricing.changed`, `applied`). Admin `manual` prices still win. Default `PLUGBOARD_PRICING_SOURCES` gains `official` (`external,official,known` with only a source URL). The scheduler status reports the table's age under `official` (`PLUGBOARD_OFFICIAL_PRICES_MAX_AGE_DAYS`, 35). Decisions (Jev, Laya) are priced from the grid too; `GET /v1/decisions/models` shows the price applied. [Updating the table](docs/runbooks/pricing-scheduler.md#official-price-table).
- Decision admission: one decision in flight per key (429 for a second one); other keys wait in arrival order (8 waiting at most, 15 seconds each) instead of being refused at once.

### Fixed

- `POST /v1/preflight` now applies the chat checks to each pair before its ping (allowed models, monthly budget, known price, key RPM limit; the concurrency cap covers the batch), accepts at most 10 pairs (422 beyond, `maxItems` in the contract), bills each ping like a chat call, and reports failures as closed codes (`unknown_model`, `forbidden_model`, `budget_exceeded`, `price_unavailable`, `rate_limited`, `upstream_error`) instead of the provider's error text.
- A key with a monthly budget is refused with 402 a model without a known price, streamed or not and in preflight, before any provider call: such calls were recorded at 0 and never reached the limit. Ollama cloud models (`*-cloud`, `:cloud`) priced only by the built-in local 0 count as unpriced. Keys without a budget are unchanged. See [which models have a known price](docs/guide/clients-and-keys.md#which-models-have-a-known-price).
- CLI providers run in their own process group: on a timeout, a cancellation or any error the CLI and its descendants are killed and reaped before the concurrency slot and the private Gemini home are released, including for `agy models`. Previously a timeout killed only the CLI itself and a cancellation killed nothing.
- The `spawn CLI` log line no longer contains command-line arguments, which carried the prompt for Antigravity; it records the CLI, the model and the prompt length.
- Python SDK: the synchronous client no longer follows HTTP redirects, which sent the `Authorization` key to the redirect target; a 3xx now raises `PlugboardError` with that status. The async client already did not follow them.
- Docker image: `manifests/` (the decision manifests the gateway loads) is now copied beside the package.
- The built-in 0 for local Ollama models applies to self-hosted Ollama providers only: a local model name (`llama3`) on another provider, or an Ollama cloud model, is no longer free by name.
- The same holds for a 0 written by name in a grid by a source (or an entry without source), and for estimates from it; an admin's `manual` 0 stays a price for every provider.
- Typed decisions: time waiting in the queue is deducted from the engine's allowance, so queue plus engine stay within the published 45-second `api_timeout_seconds` (504 beyond). A success whose usage cannot be recorded now returns its answers with `billing.status: "unrecorded"` and an error log without the request, instead of a 500.
- **Breaking for Ollama reached by a DNS name** (`host.docker.internal`, a compose service): the local 0 now needs an Ollama `base_url` on `localhost` or a loopback/private IP literal; elsewhere set a `manual` 0 in the `ollama` price space, or budgeted keys get 402. The id or port 11434 alone no longer qualifies.
- Ollama names are priced case- and space-insensitively (`gpt-oss:20b-CLOUD` is a cloud model, never free) and still sent upstream as written.
- Decision engine prices (`jev`, `laya`) no longer price chat models, and a price key matches the end of a model id only after `/`, `:` or `--` (`himalaya` no longer takes `laya`'s price).
- Decisions: when a usage write fails, budgeted keys are refused (503) until a write succeeds; one probe call every 60 seconds. Keys without a budget are unchanged.
- `/v1/chat/completions`: a non-string `model` or a non-object body returns 400 instead of 500.
- Claude Code CLI model discovery no longer picks up the agy effort variants (`claude-opus-5-5-medium`, any `-low`/`-medium`/`-high` row) that the official price table writes into the grid; agy keeps them.

## [4.2.0] — 2026-10-23

### Added

- **Client API contract**: [`docs/api/openapi.json`](docs/api/openapi.json) (OpenAPI 3.1) describes every route a client key calls, with request and response schemas, status codes and headers, and [`docs/api/mcp.json`](docs/api/mcp.json) the MCP tools with their input schemas. Both are generated from the code (`make contract`) and checked in CI, against the routes and against real answers. A running instance serves the OpenAPI document without a key at `GET /v1/openapi.json`. Integration guide: [`docs/api/README.md`](docs/api/README.md).

### Fixed

- Streamed answers: the usage chunk Plugboard adds (tokens and `cost_usd`) now always comes before `data: [DONE]`. When a short answer reached the gateway in one read, it came after, and OpenAI clients, which stop at `[DONE]`, never saw it.
- `billing.price_key` (the pricing-grid entry, which reveals provider routing) is no longer sent to client keys, as for the `X-Price-Key` header and `/usage/me`; admins still see it.

## [4.1.2] — 2026-10-22

### Fixed

- Playground: changing the model starts a new conversation. The previous model's answers were sent to the new one, which took that identity over and repeated them (asking "which model are you?" after a switch returned the previous answer). The earlier messages stay on screen above a divider, and each answer shows which model it was asked to.
- Playground: a message that ends in an error is no longer sent again with the next one.
- Claude Opus 4.7 / 4.8 / 5 / 5.5, Sonnet 5 / 5.5, Haiku 5.5 and the Fable / Mythos models no longer fail with HTTP 400 "`temperature` is deprecated for this model": the gateway removes `temperature`, `top_p` and `top_k` for these models, which do not accept them. Other models are unchanged.

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
