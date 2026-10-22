# Monitoring

Plugboard monitors itself and its providers without external services: everything below works out of the box, and the metrics endpoints let you plug in Prometheus or any HTTP checker.

All `/admin/*` and `/metrics*` routes require `Authorization: Bearer $ADMIN_KEY` (or a dashboard session).

## Dashboard pages

| Page | What you see |
|---|---|
| Overview | Totals and a table of requests, errors and average latency per provider since the last restart |
| Monitoring | Health of each active provider (status, latency, error rate, uptime over 24 hours and 7 days) and the check history of the last 24 hours |
| Usage & Billing | Requests, tokens and cost per client for a month |
| Jobs | The price refresh, the model catalog refresh and the provider watchdog: state, last and next run, **Run now** |
| Playground | Per-model connectivity and internet-access indicators |

## Health endpoints

`GET /health` is public and cheap, suitable for load balancers and container health checks:

```json
{"ok": true, "version": "4.1.2", "providers": 3, "active": ["anthropic-direct", "ollama", "claude-via-claude-cli"]}
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

After `WATCHDOG_FAILURES` consecutive failures (default 3) the watchdog tries to restart the provider by running a launcher script, `scripts/openclaw_launcher.sh`, if you provide one (it is not shipped). Restarts are limited by `WATCHDOG_RESTART_COOLDOWN` (120 s) and `WATCHDOG_MAX_RESTARTS` per hour (5). Without the script, the attempt is recorded as a failed restart and the provider stays as it is. Sovereign providers are never restarted.

| Endpoint | Effect |
|---|---|
| `GET /admin/watchdog` | Running state and per-provider counters |
| `POST /admin/watchdog/start`, `POST /admin/watchdog/stop` | Start or stop the loop |
| `POST /admin/watchdog/{provider_id}/restart` | Run the launcher script now |
| `GET /admin/watchdog/events?provider_id=&event_type=&limit=` | Check and restart history (`data/watchdog.db`) |
| `GET /admin/watchdog/restarts?hours=24` | Restart statistics |

## Model catalog and connectivity

- **At startup**, Plugboard probes the connectivity of each provider's models and writes `data/model_status.json`; `/v1/models` reports the result in each model's `connected` field. Ollama models found offline are probed again every `PLUGBOARD_OLLAMA_RECHECK_INTERVAL_S` seconds (300).
- **Every 6 hours** (`PLUGBOARD_MODEL_AVAILABILITY_INTERVAL_H`; `0` = once a day at `MODEL_AVAILABILITY_DAILY_HOUR_UTC`), a refresh reads each provider's model list: Anthropic, Gemini and Ollama through their own APIs, any provider with `auto_discover: true` (providers created from a catalog preset) through `GET /models`, and the signed-in CLI accounts by probing them, at most every `PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H` hours (24), since each probe spawns the CLI. It also quarantines models that keep failing.
- **How a list changes.** A new model is added at once. A model is removed only after `PLUGBOARD_MODEL_REMOVAL_PASSES` (2) refreshes in a row that answered correctly without it; a refresh that fails, answers in another format or returns an empty list changes nothing; a refresh that would drop more than half of the list is treated as a provider problem, not applied, and reported in `errors`. The `default_model` and the models listed in `pinned_models` are never removed. A removed model disappears from `GET /v1/models`; a request that names `provider:model` explicitly is still forwarded. `POST /providers/{id}/discover` is an explicit admin action and removes at once (`?dry_run=true` shows what would change).
- `GET /admin/models/scheduler/status` shows the scheduler and `last_changes` (added, removed and pending removals per provider, errors). `stale: true` means no successful refresh for two intervals (48 hours in daily mode).
- `POST /admin/models/refresh` runs the connectivity probe on demand (at most once every `PLUGBOARD_REFRESH_COOLDOWN_S` seconds); `GET /admin/models/status` returns the stored status.
- **Internet-access check**: at startup Plugboard asks every model a question that only a model with live web access can answer, to show which models can browse. This sends one real request to each model; set `PLUGBOARD_INTERNET_CHECK_ON_BOOT=0` to skip it. The Playground buttons run the same check on demand (`POST /admin/models/check-internet` and `/admin/models/check-internet/all`).

## Automatic updates at a glance

```bash
curl -s http://localhost:8765/admin/auto-update/status -H "Authorization: Bearer $ADMIN_KEY"
curl -s -X POST http://localhost:8765/admin/auto-update/run -H "Authorization: Bearer $ADMIN_KEY"
```

The status groups the price scheduler, the model-list scheduler, the models.dev catalog (`ok`, `stale`, `unavailable` or `off`), the number of price suggestions waiting for review and the latest changes. `run` starts a price update and a model-list refresh in the background (`202`); it answers `409` with `running_since` while one is already running.

Every change found by an automatic update (model added or removed, price change above `PLUGBOARD_PRICE_DRIFT_PCT`) is appended to `data/catalog/changes.jsonl`, which keeps the last 1 000 lines.

## Admin audit log

Admin actions are recorded in `data/audit.db`: logins (success, failure, lockout, MFA), auto-logins and suspicious auto-login refusals, key creation, revocation, rotation and deletion, provider creation, changes and deletion (including base URL changes and refused domains), OAuth connections, pricing refreshes, cache clears, circuit resets, watchdog actions, model refreshes, Ollama pulls and deletions, job runs, and MFA enrolment.

```bash
curl -s "http://localhost:8765/admin/audit?limit=20" -H "Authorization: Bearer $ADMIN_KEY"
curl -s "http://localhost:8765/admin/audit?action=auth.login_failed" -H "Authorization: Bearer $ADMIN_KEY"
```

## Alerts and webhooks

### Alert webhook

Set `ALERT_WEBHOOK_URL` to receive JSON alerts (for example on a Slack or Discord incoming webhook, or your own endpoint):

- `provider_down`: a provider failed `ALERT_FAILURE_THRESHOLD` consecutive forwarded requests (default 3); at most one alert per provider every five minutes.

  ```json
  {"type": "provider_down", "provider_id": "anthropic-direct", "consecutive_failures": 3, "message": "..."}
  ```

- `spending_alert`: a client is at or above 80 % of its monthly limit, sent on each billed request in that range.

  ```json
  {"type": "spending_alert", "status": "warning", "client_id": "billing-service", "current_cost_usd": 8.12, "limit_usd": 10, "percentage": 81.2, "message": "..."}
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

Request and response bodies are not written to the logs, but provider and CLI error messages can be. Useful patterns: `auth.auto_login refused reason=...`, `Circuit breaker:`, `Provider ... failed, trying next`, `PricingScheduler`, `Watchdog:`.
