# Monitoring

Plugboard monitors itself and its providers without external services: everything below works out of the box, and the metrics endpoints let you plug in Prometheus or any HTTP checker.

All `/admin/*` and `/metrics*` routes require `Authorization: Bearer $ADMIN_KEY` (or a dashboard session).

### Changes made with a dashboard session

A dashboard session changes nothing from another page. A `POST`, `PUT`, `PATCH` or `DELETE` authenticated by the session cookie, on an admin route or on the Playground's chat routes, is refused with `403 Cross-site request refused` when the browser says another page sent it: a `Sec-Fetch-Site` header other than `same-origin`, or, from a browser that sends no `Sec-Fetch-Site` (plain HTTP to a non-local address), an `Origin` whose host or port differs from the `Host` header. The cookie is `SameSite=Strict`, but a page served on another port of the same host counts as the same site, so the browser would send the cookie with its requests.

The dashboard and `/suivi` call the API from their own origin and are not affected, nor are reads, the admin key in `Authorization`, client keys, requests without browser headers (`curl -b admin.cookies` below) and `POST /auth/admin/login`. Behind a proxy, keep the `Host` header (see [Deployment](deployment.md#behind-a-reverse-proxy)).

### Admin API with MFA

When MFA is enforced (`PLUGBOARD_ADMIN_MFA_REQUIRED=1` and a secret enrolled), the admin key alone is refused with 401 on every admin route (`/admin/*`, `/providers*`, `/metrics*`, `/docs`, the dashboard). A script either:

- sends a fresh code with each call: `-H "Authorization: Bearer $ADMIN_KEY" -H "X-Plugboard-Admin-TOTP: <code>"`. A code is accepted once, and no older code after it, so this suits one call per 30 seconds; wrong codes count towards the login lockout;
- or opens a session once and reuses its cookie:

```bash
curl -s -c admin.cookies -H "Content-Type: application/json" \
  -d "{\"key\": \"$ADMIN_KEY\", \"totp_code\": \"<code>\"}" http://localhost:8765/auth/admin/login
curl -s -b admin.cookies http://localhost:8765/admin/watchdog
curl -s -b admin.cookies -X POST http://localhost:8765/auth/admin/logout
```

Sessions are stored on the server (`data/admin_sessions.json`) and last `ADMIN_SESSION_MAX_AGE` (8 hours). **Log out** revokes that session; `POST /auth/admin/logout-all` (dashboard: **Log out everywhere**) revokes every session, this one included, and forgets every browser remembered for local auto-login (sign in again with the link `make dev` opens, or with `ADMIN_KEY`); enrolling, rotating or disabling MFA revokes every session; a session opened without a code stops working once MFA is enforced. Keep the cookie file private and delete it after use. Without enforced MFA the bearer alone works as before.

Once a secret is enrolled, changing the second factor needs a current code from it, even from an open session: `DELETE /admin/mfa` takes it in the `X-Plugboard-Admin-TOTP` header, and rotating through `POST /admin/mfa/setup` takes it as `current_code` next to the new `secret` and `code`. Without it the call is refused with 403; wrong codes count towards the login lockout. With a lost authenticator, run `scripts/admin_mfa_setup.py --rotate` on the host.

## Dashboard pages

| Page | What you see |
|---|---|
| Overview | Totals and a table of requests, errors and average latency per provider since the last restart |
| Monitoring | Health of each active provider (status, latency, error rate, uptime over 24 hours and 7 days) and the check history of the last 24 hours |
| Catalog history | Models each provider gained or lost, newest first, filtered by provider, period and model name ([details](#catalog-history)) |
| Usage & billing | Requests, tokens and cost per client for a month |
| Jobs | The price refresh, the model catalog refresh and the provider watchdog: state, last and next run, **Run now** |
| Playground | Per-model connectivity and internet-access indicators |
| Tickets (`/suivi`) | Kanban of Plugboard tickets, see below |

## Ticket board (`/suivi`)

`/suivi` (sidebar link **Tickets**) shows Plugboard tickets as a Kanban: *Triage*, *Ready*, *In progress*, *In review*, *Done*, *Blocked*, *Abandoned*. It needs the admin session, like the dashboard; opened signed out, it goes through `/login` and back.

- Move a ticket with its **Status** list (works on a phone and with the keyboard). *Blocked* and *Abandoned* ask for a reason; *Done* needs at least one proof (command and result, commit, CI run, path or link), added under **Evidence**. Error messages from the server are still in French.
- **Import** reads, without changing them, `docs/superpowers/plans/*.md` (one ticket per `### Task N:` section, or per top-level checkbox), `docs/superpowers/specs/*.md` that no plan mentions yet (one "Planifier : …" ticket each) and `TODOS.md` (one ticket per `###` entry, its `**Priority:**`, type *Security* under `## Security`). Importing again creates nothing twice; a task whose boxes are all checked moves to *In review*, never to *Done*. `.atelier/` traces are never read, and importing starts no agent or job.
- Tickets, proofs and the history of moves are in `data/suivi.db`. Every change is in the [audit log](#admin-audit-log) (`suivi.*` actions).
- API (admin): `GET`/`POST /admin/suivi/tickets`, `PATCH /admin/suivi/tickets/{id}`, `POST /admin/suivi/tickets/{id}/preuves`, `POST /admin/suivi/import`. A refused move answers `409` with a `code` (`motif_requis`, `preuve_requise`).

## Health endpoints

`GET /health` is public and cheap, suitable for load balancers and container health checks:

```json
{"ok": true, "version": "4.11.5", "providers": 3, "active": ["anthropic-direct", "ollama", "claude-via-claude-cli"]}
```

When `API_KEY` is empty it also returns `"auth_disabled": true` and `"dev_mode"`, so a deployment gate can refuse an instance whose client authentication is off.

`GET /health/detailed` (admin) calls `<base_url>/health` on every active HTTP provider and adds usage counters for the current month.

## Metrics

| Endpoint | Format |
|---|---|
| `GET /metrics` | JSON: per provider, request count, error count, average latency |
| `GET /metrics/prometheus` | Prometheus text format: request totals, error totals and average latency per provider, and an `up` gauge |
| `GET /admin/monitoring` | Full health check of all providers with 24-hour and 7-day uptime |
| `GET /admin/monitoring/history?provider_id=&limit=` | Stored health snapshots (`data/monitoring.db`) |
| `GET /admin/circuits` | Per-model circuit breaker state |
| `GET /admin/cache/stats` | Response cache statistics |

Request metrics live in memory and restart from zero with the server. Prometheus must send the admin key as a bearer token.

## Provider watchdog

The watchdog starts with the server and checks every active provider every `WATCHDOG_INTERVAL` seconds (default 30):

- HTTP providers: `GET <base_url>/health`, and every `WATCHDOG_DEEP_INTERVAL` checks (default 3) a real one-token completion with the provider's default model. **On paid API providers these deep checks appear on your provider bill.**
- Ollama and other *sovereign* providers: `GET <base_url>`; no deep check.
- CLI providers: a lightweight probe of the CLI.

The watchdog does not search for or execute a host launcher script and does not
load a `.env` to restart a connector. An unmanaged direct-provider restart is
reported as unsupported. Operate Docker-managed services through their deployment
supervisor; CLI requests still start their native command on demand. Recognizable
removed legacy providers are rejected before health, discovery or deep-check
transport, rather than reported healthy. See [migration](openclaw-removal.md).

| Endpoint | Effect |
|---|---|
| `GET /admin/watchdog` | Running state and per-provider counters |
| `POST /admin/watchdog/start`, `POST /admin/watchdog/stop` | Start or stop the loop |
| `POST /admin/watchdog/{provider_id}/restart` | Report restart unsupported for unmanaged providers; reject removed legacy connectors |
| `GET /admin/watchdog/events?provider_id=&event_type=&limit=` | Check and restart history (`data/watchdog.db`) |
| `GET /admin/watchdog/restarts?hours=24` | Restart statistics |

## Model catalog and connectivity

- **At startup**, Plugboard probes the connectivity of each provider's models and writes `data/model_status.json`; `/v1/models` reports the result in each model's `connected` field. Ollama models found offline are probed again every `PLUGBOARD_OLLAMA_RECHECK_INTERVAL_S` seconds (300).

Before any observation, `/v1/models` returns `connected: false` and `last_probed_at: null`. This means connectivity is unknown and does not prevent a first request to a newly configured provider. MCP displays this state with a neutral white icon. A real positive or negative observation retains its result and timestamp. A policy quarantine does not create a probe timestamp and cannot replace an observation belonging to another provider, even when their IDs share a prefix.
- **Every 6 hours** (`PLUGBOARD_MODEL_AVAILABILITY_INTERVAL_H`; `0` = once a day at `MODEL_AVAILABILITY_DAILY_HOUR_UTC`), a refresh reads each provider's model list: Anthropic, Gemini and Ollama through their own APIs, any provider with `auto_discover: true` (providers created from a catalog preset) through `GET /models`, and the signed-in CLI accounts by probing them, at most every `PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H` hours (24), since each probe spawns the CLI. It also quarantines models that keep failing.
- **How a list changes.** A new model is added at once. A model is removed only after `PLUGBOARD_MODEL_REMOVAL_PASSES` (2) refreshes in a row that answered correctly without it; a refresh that fails, answers in another format or returns an empty list changes nothing; a refresh that would drop more than half of the list is treated as a provider problem, not applied, and reported in `errors`. The `default_model` and the models listed in `pinned_models` are never removed. A removed model disappears from `GET /v1/models`; a request that names `provider:model` explicitly is still forwarded. `POST /providers/{id}/discover` is an explicit admin action and removes at once (`?dry_run=true` shows what would change).
- `GET /admin/models/scheduler/status` shows the scheduler and `last_changes` (added, removed and pending removals per provider, errors). `stale: true` means no successful refresh for two intervals (48 hours in daily mode).
- `POST /admin/models/refresh` runs the connectivity probe on demand (at most once every `PLUGBOARD_REFRESH_COOLDOWN_S` seconds); `GET /admin/models/status` returns the stored status.
- **Internet-access check**: at startup Plugboard asks every model a question that only a model with live web access can answer, to show which models can browse. This sends one real request to each model, except local Ollama models (they have no web access, and the check would make the host load and run each of them; Ollama Cloud models are checked); set `PLUGBOARD_INTERNET_CHECK_ON_BOOT=0` to skip it. The Playground buttons run the same check on demand (`POST /admin/models/check-internet` and `/admin/models/check-internet/all`, which checks local models too unless the body holds `"skip_local": true`). A model whose providers all stay at their concurrency cap is answered `"status": "busy"`: nothing was checked, and nothing is recorded, even with `"persist": true`. Each check calls the model's own provider only. The bulk check runs the models of a capped provider (a CLI provider, one request at a time by default, or a local one) within that cap (one after the other for a cap of 1), so its own probes never fill the provider's queue; while it checks a CLI model, chats for that CLI wait for it like for any other request, then may get `503 providers_busy`. Set `PLUGBOARD_INTERNET_CHECK_ON_BOOT=0` when the startup check must not compete with chats.

## Automatic updates at a glance

```bash
curl -s http://localhost:8765/admin/auto-update/status -H "Authorization: Bearer $ADMIN_KEY"
curl -s -X POST http://localhost:8765/admin/auto-update/run -H "Authorization: Bearer $ADMIN_KEY"
```

The status groups the price scheduler, the model-list scheduler, the models.dev catalog (`ok`, `stale`, `unavailable` or `off`), the number of price suggestions waiting for review and the latest changes. `run` starts a price update and a model-list refresh in the background (`202`); it answers `409` with `running_since` while one is already running.

Every change found by an automatic update (model added or removed, price change above `PLUGBOARD_PRICE_DRIFT_PCT`) is appended to `data/catalog/changes.jsonl`, which keeps changes for `PLUGBOARD_CHANGE_JOURNAL_DAYS` days (365) and at most 10 000 lines: older changes are no longer shown in the [catalog history](#catalog-history) and are dropped from the file when a new change is written (the Monitoring card shows the last changes whatever their age).

## Catalog history

Which models a provider gained or lost, and when, from the same journal. The dashboard shows it on the **Catalog history** page (also linked as **Full history** under Monitoring › Recent automatic changes); the API answers the same:

```bash
curl -s "http://localhost:8765/admin/catalog/history?provider=<provider-id>&since=2026-11-01&limit=50" \
  -H "Authorization: Bearer $ADMIN_KEY"
```

| Parameter | Meaning |
|---|---|
| `provider` | One provider ID (exact). A deleted provider keeps its history. |
| `since`, `until` | UTC days as `YYYY-MM-DD`, both included. `since` after `until` answers `422`. |
| `model` | Part of a model name, any case. A matching change keeps all its names, the matching ones first. |
| `limit`, `offset` | Page size (1 to 200, default 50) and position. |

The answer lists `entries` newest first, each with `at` (UTC), `provider`, `added` and `removed`, then `total` (matches for these filters), the `limit` and `offset` used, `providers` (every provider in the history, for a filter list) and `retention` (`days`, `max_lines`, and `oldest`, the `at` of the oldest model-list change kept). Dates are UTC, written `YYYY-MM-DDTHH:MM:SSZ`. Only model-list changes are listed: prices stay in Pricing › Review and in the Monitoring card. The first model list of a new provider is not a change and is not listed. A model removed by an automatic refresh is dated when the removal is confirmed, after `PLUGBOARD_MODEL_REMOVAL_PASSES` refreshes without it (2); a removal from the **Discover** button is dated at once.

## Admin audit log

Admin actions are recorded in `data/audit.db`: logins (success, failure, lockout, MFA), auto-logins and suspicious auto-login refusals, key creation, revocation, rotation and deletion, provider creation, changes and deletion (including base URL changes and refused domains), OAuth connections, pricing refreshes, cache clears, circuit resets, watchdog actions, model refreshes, Ollama pulls and deletions, job runs, and MFA enrolment. Each entry has an `actor`: `admin` by default, `system` for an egress refusal found when a protected provider connects (`provider.egress_refused`), and `mcp` for the provider calls of the [MCP admin tools](mcp.md#admin-tools-stdio-only). These record `provider.create`, `provider.discover`, `provider.egress.rejected` and `provider.domain.rejected` as for the dashboard, plus their other refusals (`provider.create.refused`, `provider.discover.refused`) and previews (`provider.discover.preview`).

```bash
curl -s "http://localhost:8765/admin/audit?limit=20" -H "Authorization: Bearer $ADMIN_KEY"
curl -s "http://localhost:8765/admin/audit?action=auth.login_failed" -H "Authorization: Bearer $ADMIN_KEY"
```

## Alerts and webhooks

### Alert webhook

Set `ALERT_WEBHOOK_URL` to receive JSON alerts (for example on a Slack or Discord incoming webhook, or your own endpoint):

- `provider_down`: a provider failed `ALERT_FAILURE_THRESHOLD` consecutive forwarded requests (default 3) on its own side, by the circuit breaker's rule (`429`, `5xx`, timeouts, network errors, `401`/`403`, CLI quota or failed run; never a `400`, `413`, `422` or a prompt too long); a streamed request to an HTTP provider is not counted. At most one alert per provider every five minutes.

  ```json
  {"type": "provider_down", "provider_id": "anthropic-direct", "consecutive_failures": 3, "message": "..."}
  ```

- `spending_alert`: a client is at or above 80 % of its monthly limit, sent on each billed request in that range.

  ```json
  {"type": "spending_alert", "status": "warning", "client_id": "billing-service", "current_cost_usd": 8.12, "limit_usd": 10, "percentage": 81.2, "message": "..."}
  ```

- `provider_egress_refused`: the host of a protected provider resolves to a non-public address, so its requests are refused before anything is sent ([public addresses only](providers-and-subscriptions.md#public-addresses-only)). At most one alert per provider every five minutes; the provider stays active.

  ```json
  {"type": "provider_egress_refused", "provider_id": "catalog-provider", "host": "api.example.com", "message": "..."}
  ```

### Event webhooks

`WEBHOOK_URLS` (comma-separated) receives lifecycle events: `key.created`, `key.revoked`, `key.rotated`, `provider.created`, `provider.deleted`, and the automatic-update events below. Restrict them with `WEBHOOK_EVENTS` (comma-separated, default all). Each delivery is a POST of:

```json
{"event": "key.created", "ts": "2026-10-17T09:12:03.120Z", "data": {"key_id": "...", "client_id": "billing-service"}}
```

with the event name in an `X-Plugboard-Event` header. When `WEBHOOK_SECRET` is set, the raw body is signed with HMAC-SHA256 and sent as `X-Plugboard-Signature: sha256=<hex>`; verify it before trusting the payload. Delivery is fire-and-forget with a 10-second timeout and no retry.

Automatic updates send:

| Event | `data` |
|---|---|
| `models.changed` | `{"provider": "<id>", "added": [...], "removed": [...]}` |
| `price.estimated` | `{"model": "<id>", "space": "<provider space or empty>", "from": "<model whose price was borrowed>", "input_price": ..., "output_price": ...}` |
| `pricing.changed` | `{"month", "model", "source", "old": {input_price, output_price}, "new": {...}, "pct", "status"}`, plus `provider` for a provider price. `status` is `pending` when the change waits for review and `applied` when it is written. |

They are only sent for real changes: never for the first model list of a provider or the first price of a model, never when only the source of a price changes, and a pending price change is announced once. One update sends at most `PLUGBOARD_WEBHOOK_MAX_PER_RUN` events (50); the rest is summarised in one event per type with `{"truncated": N}`, and every change is still in the journal.

## Logs

Logs are JSON lines on standard output, one object per line with `ts`, `level`, `logger`, `request_id` and `msg`. The `request_id` matches the `X-Request-ID` response header. Set `PLUGBOARD_LOG_FILE` to also write a rotating file (`PLUGBOARD_LOG_MAX_BYTES`, default 20 MB, `PLUGBOARD_LOG_BACKUPS`, default 5).

Request and response bodies are not written to the logs, but provider and CLI error messages can be. Useful patterns: `auth.auto_login refused reason=...`, `Circuit OPENED for`, `Provider ... failed, trying next`, `PricingScheduler`, `Watchdog:`.
