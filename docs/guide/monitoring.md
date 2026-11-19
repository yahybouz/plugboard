# Monitoring

Plugboard monitors itself and its providers without external services: everything below works out of the box, and the metrics endpoints let you plug in Prometheus or any HTTP checker.

All `/admin/*` and `/metrics*` routes require `Authorization: Bearer $ADMIN_KEY`, a [personal dashboard key](#personal-dashboard-keys) (`Bearer pba_…`, always with its own TOTP code) or a dashboard session.

### Changes made with a dashboard session

A dashboard session changes nothing from another page. A `POST`, `PUT`, `PATCH` or `DELETE` authenticated by the session cookie, on an admin route or on the Playground's chat routes, is refused with `403 Cross-site request refused` when the browser says another page sent it: a `Sec-Fetch-Site` header other than `same-origin`, or, from a browser that sends no `Sec-Fetch-Site` (plain HTTP to a non-local address), an `Origin` whose host or port differs from the `Host` header. The cookie is `SameSite=Strict`, but a page served on another port of the same host counts as the same site, so the browser would send the cookie with its requests.

The dashboard and `/suivi` call the API from their own origin and are not affected, nor are reads, the admin key in `Authorization`, client keys, requests without browser headers (`curl -b admin.cookies` below) and `POST /auth/admin/login`. Behind a proxy, keep the `Host` header (see [Deployment](deployment.md#behind-a-reverse-proxy)).

### Admin API with MFA

When MFA is enforced (`PLUGBOARD_ADMIN_MFA_REQUIRED=1` and a secret enrolled), the admin key alone is refused with 401 on every admin route (`/admin/*`, `/providers*`, `/metrics*`, `/docs`, the dashboard). A script either:

- sends a fresh code with each call: `-H "Authorization: Bearer $ADMIN_KEY" -H "X-Plugboard-Admin-TOTP: <code>"`. A code is accepted once, and no older code after it, so this suits one call per 30 seconds; wrong codes count towards the admin key's lockout (5 in a minute lock TOTP checks for 15 minutes, from every address);
- or opens a session once and reuses its cookie:

```bash
curl -s -c admin.cookies -H "Content-Type: application/json" \
  -d "{\"key\": \"$ADMIN_KEY\", \"totp_code\": \"<code>\"}" http://localhost:8765/auth/admin/login
curl -s -b admin.cookies http://localhost:8765/admin/watchdog
curl -s -b admin.cookies -X POST http://localhost:8765/auth/admin/logout
```

Sessions are stored on the server (`data/admin_sessions.json`) and last `ADMIN_SESSION_MAX_AGE` (8 hours). **Log out** revokes that session; `POST /auth/admin/logout-all` (dashboard: **Log out everywhere**) revokes every session, this one included, and forgets every browser remembered for local auto-login (sign in again with the link `make dev` opens, or with `ADMIN_KEY`); enrolling, rotating or disabling MFA revokes every session; a session opened without a code stops working once MFA is enforced. Keep the cookie file private and delete it after use. Without enforced MFA the bearer alone works as before.

Once a secret is enrolled, changing the second factor needs a current code from it, even from an open session: `DELETE /admin/mfa` takes it in the `X-Plugboard-Admin-TOTP` header, and rotating through `POST /admin/mfa/setup` takes it as `current_code` next to the new `secret` and `code`. Without it the call is refused with 403; wrong codes count towards the admin key's lockout. With a lost authenticator, run `scripts/admin_mfa_setup.py --rotate` on the host.

### Personal dashboard keys

Instead of sharing `ADMIN_KEY`, each person can hold their own dashboard key and their own second factor. A key is named, revocable on its own, and every admin action it makes is recorded under that name in the [audit log](#admin-audit-log). Every person is a full admin: there are no roles and no read-only keys. `ADMIN_KEY` keeps working as before and stays the break-glass key. Personal keys need `ADMIN_KEY` to be set.

**Create a key.** An admin (with `ADMIN_KEY` or a session) uses the **People** page of the dashboard (**+ New person**), or the API:

```bash
curl -s -X POST http://localhost:8765/admin/people \
  -H "Authorization: Bearer $ADMIN_KEY" -H "X-Plugboard-Admin-TOTP: <code>" \
  -H "Content-Type: application/json" -d '{"name": "person-one"}'
```

The `X-Plugboard-Admin-TOTP` header is needed only where `ADMIN_KEY` itself needs a code (see [Admin API with MFA](#admin-api-with-mfa)); a session cookie replaces both headers. The answer (`201`, `Cache-Control: no-store`) holds the person (`id` like `ap_0123456789ab`, `status`), the raw key (`pba_…`), the TOTP secret and its `otpauth://` URI (issuer `Plugboard`, account the name). **The key, the secret and the URI are shown this once**: only a hash of the key and the encrypted secret are stored, and nothing can show them again. The dashboard shows them with a Copy button each and clears them when you confirm you stored them. The name is 1 to 64 printable characters, unique among the keys that are not revoked; a name with an invisible character, a space at the start, at the end or doubled is refused (`422`), and a name that looks like another once Unicode compatibility forms and letter case are folded (for example a full-width form of it) is refused as taken (`409`). If the audit log cannot record the creation, the key is revoked at once and the call answers `503`: a key is never returned without its audit entry.

**Hand it over.** Send the key and the secret to the person through two different channels, or hand them over in person. The person adds the secret (or the URI) to an authenticator application. The key stays `pending` until its first accepted code, at login or in the header: that code confirms it (the key turns `active`, audited as `admin.person.confirmed`). A key that is never used stays `pending`: revoke it if it was not handed over.

**Sign in.** The login screen takes the personal key in the same field as `ADMIN_KEY`, then asks for the code of the person's own authenticator. A script sends both headers, **always**, even where `ADMIN_KEY` needs no code:

```bash
curl -s http://localhost:8765/admin/watchdog \
  -H "Authorization: Bearer pba_…" -H "X-Plugboard-Admin-TOTP: <code>"
```

A code is accepted once, and no older code after it, as for `ADMIN_KEY`. A session opened with a personal key lasts `ADMIN_SESSION_MAX_AGE` like any other and belongs to that person.

**List and revoke.** `GET /admin/people` lists the keys: `id`, `name`, `key_prefix` (the first 10 characters, to tell keys apart), `status` (`pending`, `active`, `revoked`), the dates and who created, confirmed or revoked each one; never a hash or a secret. `DELETE /admin/people/<id>` revokes a key: it stays in the list as `revoked`, it is refused from then on, and the sessions it opened stop working at once. The call is idempotent (a second one changes nothing and writes no second audit entry); an unknown id answers `404`. Any admin can revoke any key, a person their own included.

**Lost authenticator.** A person's second factor cannot be reset: revoke the key and create a new one (the name is free again once the old key is revoked).

**Lockout.** Wrong codes count against that person's key only: 5 wrong codes in a minute lock that key's code checks for 15 minutes (audited as `auth.person.mfa_failed`, then `auth.login_blocked`), without touching `ADMIN_KEY` nor any other person. A wrong or unknown `pba_` key counts against the caller's address, like a wrong `ADMIN_KEY`; a valid key from a locked address is not refused for that.

**What a personal key does not do.**

- It does not call the client API as a bearer: `/v1/*` takes client keys (`pb_…`), and `API_KEY` is not a personal key. A session a person opened reaches the Playground like any admin session: it is the `admin` client on `/v1/*` (no budget, model restriction or RPM limit), and that usage is recorded under `admin`, not under the person.
- It does not manage the second factor of `ADMIN_KEY`: `POST /admin/mfa/begin`, `POST /admin/mfa/setup`, `POST /admin/mfa/verify` and `DELETE /admin/mfa` answer `403` to a person (and to a session a person opened), before any code is read, and the refusal is audited as `admin.mfa.person_refused`. Only `GET /admin/mfa/status` answers. Changing the second factor of `ADMIN_KEY` stays with `ADMIN_KEY` and its authenticator.

**What cuts sessions.** Enrolling, rotating or disabling the MFA of `ADMIN_KEY`, and **Log out everywhere**, revoke every session, those of personal keys included; the keys themselves stay valid and their holders sign in again. Revoking a key cuts only that person's sessions.

**Instances without `ADMIN_KEY`.** With authentication fully off (no `API_KEY` and no `ADMIN_KEY`, a local install), the people routes are open like every admin route; `POST /admin/people` answers `503` (`Personal keys need ADMIN_KEY`), since a key would mean nothing, and `GET` and `DELETE` are not protected (a revocation is then recorded as `admin-key`). With `API_KEY` set but no `ADMIN_KEY`, every admin route answers `503`, these included.

**Unreadable store.** If `data/admin_people.json` cannot be read (damaged file, bad permissions), every personal key is refused (`401`) and the people routes answer `503`, with the error type only in the log; `ADMIN_KEY` is not affected. A write failure answers `503` too, never a `500`. Repair the file or restore it from a backup.

**Where things are.** The store is `data/admin_people.json` (mode `0600`): it lives in `data/`, so the [backup](deployment.md#backups-and-upgrades) holds it, and `scripts/rotate_encryption_key.py` re-encrypts the personal secrets with the others (a rotation refuses to run when the file is damaged). The logs mask `pba_…` keys (`pba_***REDACTED***`). These routes are admin routes: they stay out of the client API contract (`CONTRACT_VERSION` is unchanged, 3.0.1) and are documented by the admin-only `/openapi.json` of the instance.

**Rolling back to 4.14.6.** The older version ignores the new store and the two new audit columns, so it starts and serves as before, but personal keys then stop working (only `ADMIN_KEY` is accepted). A session a person opened before the rollback is still honoured by the older version, with full admin rights and no name in the audit log, until it expires: use **Log out everywhere** (or revoke the keys first) before rolling back. Rolling forward again, the keys work again unless revoked meanwhile. If `ENCRYPTION_KEY` was rotated while 4.14.6 ran, the older script left the personal secrets on the former key: back on this version every personal key is refused without counting a wrong code (the log says `Personal TOTP secret of <id> cannot be decrypted`); revoke those keys and create new ones.

## Dashboard pages

| Page | What you see |
|---|---|
| Overview | Totals and a table of requests, errors and average latency per provider since the last restart |
| Monitoring | Health of each active provider (status, latency, error rate, uptime over 24 hours and 7 days) and the check history of the last 24 hours. Uptime comes from availability snapshots taken every 5 minutes, not from opening the page, which stores nothing; it shows `—` when the period (24 hours or 7 days) holds no snapshot, as before the first one |
| Catalog history | Models each provider gained or lost, newest first, filtered by provider, period and model name ([details](#catalog-history)) |
| Usage & billing | Requests, tokens and cost per client for a month |
| People | Personal dashboard keys: who holds one, its status, **+ New person**, **Revoke** ([details](#personal-dashboard-keys)) |
| Jobs | The price refresh, the model catalog refresh, the provider watchdog, the availability snapshots (every `PLUGBOARD_MONITORING_INTERVAL_S` seconds) and the daily data retention pass ([data retention](configuration.md#data-retention)): state, last and next run, **Run now** |
| Playground | Per-model connectivity and internet-access indicators |
| Tickets (`/suivi`) | Kanban of Plugboard tickets, see below |

## Ticket board (`/suivi`)

`/suivi` (sidebar link **Tickets**) shows Plugboard tickets as a Kanban: *Triage*, *Ready*, *In progress*, *In review*, *Done*, *Blocked*, *Abandoned*. It needs the admin session, like the dashboard; opened signed out, it goes through `/login` and back.

- Move a ticket with its **Status** list (works on a phone and with the keyboard). *Blocked* and *Abandoned* ask for a reason; *Done* needs at least one proof (command and result, commit, CI run, path or link), added under **Evidence**. Error messages from the server are still in French.
- **Import** reads, without changing them, `docs/superpowers/plans/*.md` (one ticket per `### Task N:` section, or per top-level checkbox), `docs/superpowers/specs/*.md` that no plan mentions yet (one "Planifier : …" ticket each) and `TODOS.md` (one ticket per `###` entry, its `**Priority:**`, type *Security* under `## Security`). Importing again creates nothing twice; a task whose boxes are all checked moves to *In review*, never to *Done*. `.atelier/` traces are never read, and importing starts no agent or job.
- Tickets, proofs and the history of moves are in `data/suivi.db`. Every change is in the [audit log](#admin-audit-log) (`suivi.*` actions).
- API (admin): `GET`/`POST /admin/suivi/tickets`, `PATCH /admin/suivi/tickets/{id}`, `POST /admin/suivi/tickets/{id}/preuves`, `POST /admin/suivi/import`. A refused move answers `409` with a `code` (`motif_requis`, `preuve_requise`).

## Health endpoints

`GET /health` is public and cheap, suitable for container and deployment health checks. It always answers `200` while the process runs, reads no file, and shows only the status and the version:

```json
{"ok": true, "version": "4.15.1"}
```

The list of providers is on the admin `GET /providers`. The client authentication posture is the `auth` check of `GET /health/ready`: a deployment gate that must refuse an instance whose client authentication is off reads that route, which answers `503` when `API_KEY` is empty outside dev mode.

`GET /health/ready` is public too and says whether the instance can serve requests: `200` when it is ready, `503` when it is not. The status code is the answer: a load balancer or a deployment gate needs no key. It never calls a provider or starts a CLI, and its result is kept for 5 seconds. Without a key, or with a wrong, forged or revoked one (not refused here), the body is the status and the version only:

```json
{"ready": true, "version": "4.15.1"}
```

With a valid key (an active client key, the admin key, no TOTP code needed, or an admin session) it adds `checks`, which says which check failed (PLU-75, since 4.14.3; API contract 3.0.0). With both `API_KEY` and `ADMIN_KEY` empty (a keyless install, loopback only) every route is open, and so are the checks:

```bash
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/health/ready
```

```json
{"ready": true, "version": "4.15.1", "checks": {"usage_db": "ok", "cli": "ok", "auth": "ok", "ollama": "not_configured", "background": "ok"}}
```

| Check | States | Blocks readiness |
|---|---|---|
| `usage_db` | `ok`: `data/usage.db` takes a write lock; `fail`: it cannot within 5 seconds (another process holds it, a damaged file), when billing writes can wait or fail too; the retention pass never compacts it ([data retention](configuration.md#data-retention)) | yes, unless `ok` |
| `cli` | `ok`: every active CLI provider finds its binary on PATH; `fail`: one does not; `none`: no active CLI provider | on `fail` |
| `auth` | `ok`: `API_KEY` is set; `dev`: empty with `PLUGBOARD_DEV=1`; `fail`: empty outside dev mode | on `fail` |
| `ollama` | `ok` or `unreachable` from the watchdog's last check of the active Ollama providers; `unknown` when the watchdog is stopped or its last check is older than three intervals; `not_configured` without an active Ollama provider | never |
| `background` | `ok`: the watchdog, the availability snapshots and the retention pass run (or are turned off); `degraded` otherwise | never |

The body names no provider and carries no error text: a check that raises gives its failure state (`fail`, `unknown` or `degraded`) and a warning in the server log. The dashboard and `GET /health/detailed` (admin) say which provider is affected. Container and deployment health checks stay on `/health`: use `/health/ready` to keep traffic away from an instance that cannot serve, not to restart it.

`GET /health/detailed` (admin) probes every active provider with the dashboard's health check (a CLI provider by its binary and `--version`, a local server at the URL it serves, an HTTP provider at `<base_url>/health` or its model-list URL) and answers `ok` (every probe passed), `version`, `providers` (one result per provider), `usage_month`, `usage_entries` and `keys_count`.

## Jobs

The background jobs run inside the server; nothing is scheduled outside it (no crontab, no script). The **Jobs** page and the admin API show the same five jobs: `pricing_refresh`, `model_refresh`, `provider_watchdog`, `availability_snapshots` and `data_retention`.

| Endpoint | Effect |
|---|---|
| `GET /admin/jobs` | One entry per job: `id`, `name`, `description`, `schedule`, `next_run`, `last_run` and `last_status`: `success`, `running`, `never_run`, `disabled` (turned off by the configuration), `stale`, `error` (its last run failed, or its circuit breaker is open or degraded), `stopped` (its loop is no longer alive) or `not_started` (the application never started it); for the watchdog `running`, `stopped`, or `error` when its status cannot be read |
| `GET /admin/jobs/{id}/log?lines=50` | `id`, `name`, `status`, `output` (the last `lines` lines of the job's status, as JSON) and `lines_total` |
| `POST /admin/jobs/{id}/run` | **Run now**: starts the job in the background and answers at once with `status` `started`, `running` (a run is already under way) or `error` (the watchdog runs continuously and cannot be started by hand). It also runs a job whose scheduled runs are turned off. Recorded in the [audit log](#admin-audit-log) as `job.run` |

An unknown job ID answers `404`.

## Metrics

| Endpoint | Format |
|---|---|
| `GET /metrics` | JSON: per provider, request count, error count, average latency, and under `models` the per-model measures below |
| `GET /metrics/prometheus` | Prometheus text format: request totals, error totals and average latency per provider, an `up` gauge, the per-model histograms and counter below, the total wait of each chat request, and the [Claude limits met](#shared-claude-quota) |
| `GET /admin/monitoring` | Full health check of all providers with 24-hour and 7-day uptime computed from the stored snapshots; the check itself is not stored. `uptime_pct` is `null` when the period holds no snapshot |
| `GET /admin/monitoring/history?provider_id=&limit=` | Availability snapshots (`data/monitoring.db`): one row per active provider every 5 minutes; a probe that does not answer within 30 seconds is stored as down |
| `GET /admin/retention` | Retention settings in force (`settings`), the report of the last retention pass (`last_run`, `null` before the first pass): when it ran, rows deleted per store, errors; and `held` (`reason`, `since`) while the daily pass is held by its clock check, else `null` ([data retention](configuration.md#data-retention)) |
| `GET /admin/circuits` | Per-route circuit breaker state (at most 1024 routes are kept: the least recently used closed route without failures is forgotten first, then a closed one with failures, then any) |
| `GET /admin/cache/stats` | Response cache statistics |

Request metrics live in memory and restart from zero with the server. Prometheus must send the admin key as a bearer token.

### Per-model measures

Each provider entry of `GET /metrics` gains a `models` object once one of its models has a measure (a provider seen only there gets zeroed request counters). For each model, the time to first token (`ttft`), tokens per second (`tokens_per_second`) and admission wait (`admission_wait`) give `_count`, `_sum`, `_avg`, `_min`, `_max` and `_p95` (estimated from the histogram buckets); all but `_count` and `_sum` are `null` until the first value. `busy_refusals` counts the `503 providers_busy` answers.

| Measure | Prometheus | What it counts |
|---|---|---|
| Time to first token | `plugboard_ttft_seconds` (histogram) | Seconds from the slot granted on the provider to the first generated chunk of the streamed answer: text, reasoning or a tool call. The admission wait is not included: the first word a user sees comes after both |
| Tokens per second | `plugboard_tokens_per_second` (histogram) | Completion tokens after the first one, divided by the time from the first generated chunk to the last. Uses the provider's `usage`, else a tiktoken estimate. Only when the words came over 50 ms at least: a burst says nothing of the rate. A provider that counts hidden reasoning in `usage` reads higher than what the reader sees |
| Admission wait | `plugboard_admission_wait_seconds` (histogram) | Seconds a request waited for each slot it took on a provider (`PLUGBOARD_PROVIDER_WAIT_S`); `0` when the slot was free. A request that falls back to another provider records one value per provider; internal calls through the router (preflight, internet check) count too |
| Busy refusals | `plugboard_providers_busy_total` (counter) | Chats answered `503 providers_busy`, one per answer, under the first full candidate route |

**Total wait per request** (`plugboard_request_wait_seconds`, histogram, Prometheus only, no labels): the seconds each chat request (`/v1/chat/completions`, `/v2/chat/completions`, `/v1/preflight`) spent waiting in all, for a slot of the global cap, for its session's turn and for providers' slots, recorded when the request ends (after the last byte of a stream); `0` for a request that never waited. Every request that passed the key check counts once, served, refused (`503 server_busy`, `session_busy` or `providers_busy`) or left by its client (`499`), so the buckets just above the wait budget (the longer of `PLUGBOARD_INFLIGHT_WAIT_S` and `PLUGBOARD_PROVIDER_WAIT_S`, 10 s by default) show the requests that waited it all. `GET /metrics` keeps one entry per provider and does not carry it.

The time to first token and tokens per second are measured on streamed answers from HTTP providers: a non-streamed answer has no first token to time, and a CLI provider's stream is not measured here (`claude` and `agy` send theirs live, `codex` replays its complete answer; [`scripts/bench_latence.py`](../../scripts/bench_latence.py) times their first word). Labels are `provider` and `model` only, never a client or a key; a model the provider does not list (nor has as its default model) is labelled `(unlisted)`, so a name a client typed never becomes a label. At most 256 (provider, model) series are kept; past that, each provider's new models are counted under one `(other)` series of that provider.

### Shared Claude quota

When one Claude account serves several gateways (production, a test instance, development sessions), any of them can spend the quota the others need. Since 4.14.5 the gateway notes each time a real call meets a Claude limit (PLU-67): a claude CLI that answers a usage limit (`Claude AI usage limit reached`, `You've hit your limit`, `5-hour limit reached`, `You've reached your weekly limit`, or an API 429 it reports), or an Anthropic API provider (its preset, or a base URL on `anthropic.com`) that answers `429`. It sends nothing to find out: no probe, no extra call, so a gateway that serves no Claude call sees nothing.

- `GET /admin/claude-quota` answers `last_limit_at`, `provider`, `model`, `source` (`cli` or `http`), `message` (the start of the error, at most 200 characters, keys masked), `resumes_at` (when the quota reopens, if the error said so: a `Retry-After`, a "try again at HH:MM", or the time after `usage limit reached|`; `null` otherwise, for instance "resets 3pm", which gives no time zone), and per provider the limits met (`count`) and the last one. All times are UTC. The **Monitoring** page shows the same under **Claude quota**.
- The first limit of an episode (limits less than an hour apart, per provider) writes one `provider.quota_limit` entry in the [audit log](#admin-audit-log), actor `system`; a burst of calls writes no more.
- `GET /metrics/prometheus` adds `plugboard_claude_limit_total` (counter, label `provider`) and `plugboard_claude_limit_last_timestamp_seconds` (gauge, Unix time of the last limit, label `provider`).
- The state is kept in `data/claude_quota.json` and read again on each view, so it survives a restart and every worker shows the same; the counter does not restart from zero.

An alert rule that fires when a Claude limit was met in the last 15 minutes:

```yaml
groups:
  - name: plugboard-claude-quota
    rules:
      - alert: ClaudeQuotaLimitReached
        expr: increase(plugboard_claude_limit_total[15m]) > 0
        labels:
          severity: warning
        annotations:
          summary: "Claude limit reached on {{ $labels.provider }}"
          description: "See GET /admin/claude-quota for the time it reopens."
```

`time() - plugboard_claude_limit_last_timestamp_seconds < 3600` gives the same answer for the last hour, without a range.

### Load bench

`scripts/bench_concurrency.py` measures what a gateway holds, outside the CI. It sends streamed chats from 1, 2, 4 then 8 clients at once (`--clients`), `--rounds` chats each, after `--warmup` unmeasured chats, and prints per level the status codes, the time to the status of the served chats and of the refusals, the time to the first word (the first generated chunk, reasoning included, as in the per-model measures) and to the end, and the tokens per second after the first word. A chat is served when it gets a `200` event stream with at least one word that ends with `[DONE]` or a `finish_reason`; any other `200` counts as `200 stream error`. Each chat carries a distinct prompt and `"cache": false`. The client key is read from the variable named by `--key-env` (default `PLUGBOARD_BENCH_KEY`) and is never printed or written; it needs only the model under test in its `allowed_models`, and it is never sent over plain `http` to another machine unless `--allow-http` is given.

```bash
PLUGBOARD_BENCH_KEY=... .venv/bin/python scripts/bench_concurrency.py \
  --url https://gateway.example --model <provider-id>:<model> \
  --clients 1,2,4,8 --rounds 3 --warmup 1 --check \
  --first-word-p95-max 5 --tps-min 5 --json bench.json
```

| Option | Checks (exit `1` when one fails) |
|---|---|
| `--check` | Every answer is a `200` or a `503 providers_busy`; every `503` comes within `--max-wait-s` (default 10.5 s: the gateway's 10 s admission wait plus a margin; follow `PLUGBOARD_PROVIDER_WAIT_S` if it changes); every `200` within `--max-wait-s` plus `--max-first-chunk-s` (default 5 s), since the gateway sends a `200` with the provider's first chunk, after the wait |
| `--first-word-p95-max`, `--tps-min` | The D-26 targets, at the levels of `--targets-at` (default 4 clients): every chat served, the 95th percentile of the first word (nearest rank: the slowest chat up to 20 chats), and for each client the median of its rounds' tokens per second |

The gateway admits as many chats at once as the provider's `max_concurrency` (one for a local provider by default): to measure what the provider holds at a level, set it at least to that level, or the bench measures the cap. Each chat is a real inference on the provider: run it on purpose, not on a production gateway in use. Exit codes: `0` done, `1` a check failed, `2` bad usage, `3` the bench itself failed.

## Provider watchdog

The watchdog starts with the server and checks every active provider every `WATCHDOG_INTERVAL` seconds (default 30):

- HTTP providers: `GET <base_url>/health`, and every `WATCHDOG_DEEP_INTERVAL` checks (default 3) a real one-token completion with the provider's default model; with the defaults the first one comes with the third check, about 60 seconds after startup. **On paid API providers these deep checks appear on your provider bill.**
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
| `POST /admin/watchdog/{provider_id}/restart` | Restarts nothing: records the attempt and answers `500` (restart not supported, or a removed legacy connector rejected) |
| `GET /admin/watchdog/events?provider_id=&event_type=&limit=` | Check and restart history (`data/watchdog.db`) |
| `GET /admin/watchdog/restarts?hours=24` | Restart statistics |

## Model catalog and connectivity

- **At startup**, with the defaults, the catalog refresh and its probes call no model: they sync the model lists that need no inference (Ollama's `/api/tags`, the agy account list, the Anthropic and Gemini list APIs). The CLI and Ollama models whose kind is in `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` (empty by default) are probed with a short completion, and the result goes to `data/model_status.json`; `/v1/models` reports it in each model's `connected` field. With `ollama` in that list, Ollama models found offline are probed again every `PLUGBOARD_OLLAMA_RECHECK_INTERVAL_S` seconds (300).

Before any observation, `/v1/models` returns `connected: false` and `last_probed_at: null`. This means connectivity is unknown and does not prevent a first request to a newly configured provider. MCP displays this state with a neutral white icon. A real positive or negative observation retains its result and timestamp. A policy quarantine does not create a probe timestamp and cannot replace an observation belonging to another provider, even when their IDs share a prefix.
- **Every 6 hours** (`PLUGBOARD_MODEL_AVAILABILITY_INTERVAL_H`; `0` = once a day at `MODEL_AVAILABILITY_DAILY_HOUR_UTC`), a refresh reads each provider's model list: Anthropic, Gemini and Ollama through their own APIs, any provider with `auto_discover: true` (providers created from a catalog preset) through `GET /models`, the agy account list (`agy models`), and the CLI accounts whose kind is in `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` by probing their models, at most every `PLUGBOARD_MODEL_AVAILABILITY_CLI_INTERVAL_H` hours (24), since each probe spawns the CLI. With the default (empty), the claude, codex and gemini lists keep their saved models. It also quarantines models that keep failing. The quarantine (`data/quarantined_models.json`) is kept per provider: a model that a provider refuses (three failed probes, or a refusal that names the account or the model, met by a probe, a chat or the internet check) is labelled `"reason": "quarantined"` at that provider and leaves that provider's saved list at the second refresh that finds it quarantined; it is never labelled nor removed at another provider that serves the same name. A successful probe lifts the quarantine of its provider+model pair, and since 4.14.4 so does a real chat call that succeeds on that pair, streamed or not (BUG-105): with the probes off, a model that answers again is no longer kept hidden nor removed at the next refresh. A model already removed from a CLI provider's saved list is not routed to that provider any more, so only a probe or **Discover** brings it back. Entries recorded before 4.14.1 name no provider: they stay in the file, under `models`, and are applied to no provider.
- **How a list changes.** A new model is added at once. A model is removed only after `PLUGBOARD_MODEL_REMOVAL_PASSES` (2) refreshes in a row that answered correctly without it; an absence counts towards the previous one only if that one was noted less than 72 hours earlier, otherwise the count starts again at 1; a refresh that fails, answers in another format or returns an empty list changes nothing; a refresh that would drop more than half of the list is treated as a provider problem, not applied, and reported in `errors`. The `default_model` and the models listed in `pinned_models` are never removed. A removed model disappears from `GET /v1/models`; a request that names `provider:model` explicitly is still forwarded. `POST /providers/{id}/discover` is an explicit admin action and removes at once (`?dry_run=true` shows what would change).
- `GET /admin/models/scheduler/status` shows the scheduler and `last_changes` (added, removed and pending removals per provider, errors). `stale: true` means no successful refresh for two intervals (48 hours in daily mode).
- `POST /admin/models/refresh` runs the connectivity probe on demand, for the kinds in `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` (none by default), at most once every `PLUGBOARD_REFRESH_COOLDOWN_S` seconds; `GET /admin/models/status` returns the stored status.
- **Internet-access check**: at startup Plugboard asks every model a question that only a model with live web access can answer, to show which models can browse. This sends one real request to each model, except local Ollama models (they have no web access, and the check would make the host load and run each of them; Ollama Cloud models are checked). It is off by default: set `PLUGBOARD_INTERNET_CHECK_ON_BOOT=1` to run it at startup. The Playground buttons run the same check on demand (`POST /admin/models/check-internet` and `/admin/models/check-internet/all`, which checks local models too unless the body holds `"skip_local": true`). A model whose providers all stay at their concurrency cap is answered `"status": "busy"`: nothing was checked, and nothing is recorded, even with `"persist": true`. Likewise `"status": "sandbox_unavailable"` when agy's sandbox (bubblewrap) could not start, so agy was not run: the model is neither quarantined nor recorded. Since 4.14.4, a check that timed out answers `"status": "timeout"` and records nothing either (BUG-105): before, a mere timeout quarantined the model for good. Since 4.14.5 the check quarantines a model only when the error says the model is refused or does not exist (`model_not_found`, "does not exist", a refusal that names the account or the model, a 404 saying the model is not found), with the router's classification; any other failure (a 401 or 403 of the account, a key error, a CLI that exited in error, a 5xx) answers `"status": "error"` and records nothing (BUG-127): before, a word such as `key`, `exited`, `401` or `403` in the error quarantined the model for good. The bulk check counts these as failed. Each check calls the model's own provider only. The bulk check runs the models of a capped provider (a CLI provider, one request at a time by default, or a local one) within that cap (one after the other for a cap of 1), so its own probes never fill the provider's queue; while it checks a CLI model, chats for that CLI wait for it like for any other request, then may get `503 providers_busy`. Leave `PLUGBOARD_INTERNET_CHECK_ON_BOOT` off when the startup check must not compete with chats.

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

Admin actions are recorded in `data/audit.db`: logins (success, failure, lockout, MFA), auto-logins and suspicious auto-login refusals, key creation, revocation, rotation and deletion, provider creation, changes and deletion (including base URL changes and refused domains), OAuth connections, pricing refreshes, cache clears, circuit resets, watchdog actions, model refreshes, Ollama pulls and deletions, job runs, and MFA enrolment. Each entry has an `actor` (the channel): `admin` by default, `system` for an egress refusal found when a protected provider connects (`provider.egress_refused`), for a provider whose stored key cannot be decrypted (`provider.secret_unreadable`) and for the first Claude limit of an episode (`provider.quota_limit`, see [Shared Claude quota](#shared-claude-quota)), and `mcp` for the provider calls of the [MCP admin tools](mcp.md#admin-tools-stdio-only). These record `provider.create`, `provider.discover`, `provider.egress.rejected` and `provider.domain.rejected` as for the dashboard, plus their other refusals (`provider.create.refused`, `provider.discover.refused`) and previews (`provider.discover.preview`).

Each entry also names **who** acted, in two columns (added with PB-AUTH-01): `principal` is `admin-key` for `ADMIN_KEY` (and for a session it opened) or `person:<id>` for a [personal key](#personal-dashboard-keys) (and for a session that person opened), and `principal_name` is that person's name at the time. An empty `principal` means no admin credential was known: an entry written before these columns existed (an older database gains them, empty, when it is opened), by the system (`actor` `system`, and background tasks such as the watchdog or a job run), or for a request that did not authenticate. A login with an unknown key names nobody; a wrong code on a personal key names that person. The personal keys write four actions: `admin.person.created`, `admin.person.confirmed` (the first accepted code), `admin.person.revoked` and `auth.person.mfa_failed` (a wrong code); a person refused on a second-factor route of `ADMIN_KEY` writes `admin.mfa.person_refused`. Filter on `action` as below; `GET /admin/audit` returns the two columns on every entry.

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

- `provider_secret_unreadable`: a provider's stored key cannot be decrypted with the current `ENCRYPTION_KEY`, so the provider is turned off and its key is never sent ([backups and upgrades](deployment.md#backups-and-upgrades)). Once per provider and stored key while the server runs.

  ```json
  {"type": "provider_secret_unreadable", "provider_id": "openai", "message": "..."}
  ```

- `retention_held`: the daily data retention pass is held by its clock check: the clock is more than 7 days after the last pass, or `data/retention.json` cannot be read ([data retention](configuration.md#data-retention)). Once per hold; `GET /admin/retention` shows it in `held` until a pass runs.

  ```json
  {"type": "retention_held", "message": "..."}
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

Logs are JSON lines on standard output, one object per line with `ts`, `level`, `logger`, `request_id` and `msg`. The `request_id` matches the `X-Request-ID` response header. When the request carried a valid `traceparent` ([trace context](#trace-context)), the lines of that request also have `trace_id` and `span_id`; otherwise both are left out. Set `PLUGBOARD_LOG_FILE` to also write a rotating file (`PLUGBOARD_LOG_MAX_BYTES`, default 20 MB, `PLUGBOARD_LOG_BACKUPS`, default 5).

Request and response bodies are not written to the logs, but provider and CLI error messages can be. Useful patterns: `auth.auto_login refused reason=...`, `Circuit OPENED for`, `Provider ... failed, trying next`, `PricingScheduler`, `Watchdog:`.

### Trace context

A client that traces its own work (an agent run, for example) can send the W3C [`traceparent`](https://www.w3.org/TR/trace-context/) header, and `tracestate`, with any request. Plugboard reads them; it needs no OpenTelemetry library and no setting.

- **Accepted**: `00-<trace-id>-<parent-id>-<flags>`, lower-case hex, 32 characters for the trace id and 16 for the parent id, neither all zeros. A later version (not `ff`) is read by these four fields, as W3C asks. A header longer than 512 characters, or sent twice, is ignored. `tracestate` is kept only with a valid `traceparent`, as printable ASCII up to 512 characters, and is not stored.
- **Invalid or missing**: the request is served as usual, never with a `4xx`. There is no trace id: Plugboard does not make one up. The header value is never written to the logs nor sent back.
- **Logs**: `trace_id` and `span_id` on every line of the request. `span_id` is the caller's span (the W3C parent-id), not a span of Plugboard.
- **Usage**: each usage entry of a chat call has two more columns in `data/usage.db`, `request_id` (the `X-Request-ID` of the call) and `trace_id` (empty without a valid `traceparent`). The cost of one call, or of a whole trace, can then be read from Plugboard's own records: `sqlite3 data/usage.db "SELECT SUM(cost_usd) FROM usage_entries WHERE trace_id = '<trace-id>'"`. An older `usage.db` gains both columns, empty, when it is opened. They appear in the admin usage routes and exports (`/admin/usage/<month>`, `/export`); `/usage/me` leaves them out, so the client API is unchanged. Only chat calls fill them: the other entries (embeddings, decisions, the gateway's own pricing calls, lapsed budget holds) leave both empty.

Not done yet: Plugboard does not pass `traceparent` on to providers, does not return one in its answers, and records no spans of its own (time waiting for a slot, retries, route fallbacks).
