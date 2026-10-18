# Runbook: provider down

An upstream provider (an HTTP API, a local Ollama daemon, a CLI-backed
provider, or an OAuth-connected source) stops answering or returns errors.

## Symptom

- The Providers page of the dashboard shows the provider in red.
- Clients get `502` on `POST /v1/chat/completions` (or `503` with a
  `Retry-After` header when the upstream timed out).
- The alert webhook (`ALERT_WEBHOOK_URL`, if set) receives a payload with
  `"type": "provider_down"` after `ALERT_FAILURE_THRESHOLD` (default 3)
  consecutive failures. Alerts for the same provider are sent at most once
  every 5 minutes.
- Logs contain lines such as `Watchdog: provider '<id>' unhealthy`,
  `Circuit OPENED for <model>` or `Circuit breaker: <model> → fallback <model>`.

## Priority

| Situation | Priority |
|---|---|
| The provider is the only one serving the affected models, or clients pin it (`<provider_id>:<model>`) | P1 |
| Another active provider serves the same models and clients use unprefixed model names (automatic fallback) | P2 |
| Rate limiting (`429`) that clears on its own | P3 |

## Quick diagnosis

Run from the installation directory. Admin calls need the admin key.

```bash
cd /path/to/plugboard
export ADMIN_KEY=$(grep '^ADMIN_KEY=' .env | cut -d= -f2-)
```

1. List providers and spot the inactive or suspicious one:

   ```bash
   curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/providers \
     | python3 -c "import json,sys; [print(f\"{p['id']:35} active={p.get('is_active')} {p.get('auth_source') or ''} {p['base_url']}\") for p in json.load(sys.stdin)]"
   ```

2. Probe the provider directly:

   ```bash
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/providers/<provider_id>/health | python3 -m json.tool
   ```

   Healthy: `"ok": true` with a `2xx`/`3xx` `status`. Unhealthy: `"ok": false`
   plus an `error` or a `status` code. CLI-backed providers report
   `"url": "subprocess://<cli>"` instead of an HTTP URL.

3. Check the watchdog view and its recent events:

   ```bash
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/watchdog | python3 -m json.tool
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     "http://localhost:8765/admin/watchdog/events?provider_id=<provider_id>&limit=20" \
     | python3 -m json.tool
   ```

   `consecutive_failures`, `last_healthy_ago_s` and the `details` of failed
   events usually name the cause.

4. Check the model circuit breakers:

   ```bash
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/circuits | python3 -m json.tool
   ```

   A model in state `open` has failed 3 times in a row. Requests for it are
   rejected for 60 seconds and rerouted to the model listed in `fallback`
   when one is available. After the cooldown the circuit goes `half_open`
   and lets one request through.

5. Get the full picture (all providers, health, uptime over 24 h and 7 days):

   ```bash
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/monitoring | python3 -m json.tool
   ```

6. Read the logs. Logs are JSON lines on stdout; they are also written to a
   file when `PLUGBOARD_LOG_FILE` is set.

   ```bash
   docker compose logs --since 30m | grep -iE "watchdog|circuit|<provider_id>"
   # or, with a log file:
   grep -iE "watchdog|circuit|<provider_id>" "$PLUGBOARD_LOG_FILE" | tail -50
   ```

## Common causes

### Upstream outage

`/providers/<provider_id>/health` returns `5xx` or a connection error, and the
provider's public status page confirms an incident. Nothing to fix locally:
the router retries transient errors (`429`, `500`, `502`, `503`, `529`) on the
same provider, then, for model names sent without a provider prefix, moves to
the next active provider. Requests that pin a provider (`<provider_id>:<model>`)
do not cascade to other providers and fail until it recovers. Monitor until
the upstream recovers, or disable the provider (see Mitigation) if clients
keep hitting it.

### Rate limit (`429`)

The probe or the logs show `429`. The model circuit opens after 3 failures and
recovers by itself. Wait for the upstream window to reset. If it happens often,
lower client traffic with per-key `rpm_limit` values or add a second provider
for the same models.

### Expired or revoked credentials (`401` / `403`)

- API key provider: store a new key, then probe again.

  ```bash
  curl -s -X PUT -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
    -d '{"bearer_token": "<new-key>"}' \
    http://localhost:8765/providers/<provider_id>
  ```

- CLI-delegated auth (`auth_source` starting with `cli:`): the error reads
  `Auth not available for cli:<name>. Run the corresponding CLI login first.`
  Log in again with that CLI as the same OS user that runs Plugboard.
- OAuth-connected source (`auth_source` starting with `broker:`): list the
  connections, remove the broken one, then reconnect it from the dashboard
  (Providers page).

  ```bash
  curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/oauth/connections
  curl -s -X DELETE -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/oauth/<source>
  ```

### CLI-backed provider broken (`auth_source` starting with `subprocess:`)

The probe returns `'<cli>' not found on PATH. Install it and retry.` or a
failed `--version` call. Install the CLI on the `PATH` of the Plugboard
process, make sure it is logged in for that user, then restart Plugboard so
the CLI is warmed up again.

### Upstream endpoint moved (`404` on the probe)

The provider changed its base URL. Update it, then rediscover models:

```bash
curl -s -X PUT -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"base_url": "https://new-host.example.com"}' \
  http://localhost:8765/providers/<provider_id>
curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  http://localhost:8765/providers/<provider_id>/discover
```

If the update is rejected with `400 ... not in PLUGBOARD_PROVIDER_DOMAINS_ALLOWED`,
add the new host to that variable and restart.

### Ollama daemon stopped

Ollama is a local (sovereign) provider: the watchdog never restarts it.

```bash
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/ollama/status
curl -s http://localhost:11434/api/tags   # or the host set in OLLAMA_URL
ollama serve                               # start the daemon if it is down
```

### Watchdog not running

`GET /admin/watchdog` returns `"running": false`, so no automatic checks or
restarts happen. Start it again:

```bash
curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/watchdog/start
```

The watchdog checks every `WATCHDOG_INTERVAL` seconds (default 30). After
`WATCHDOG_FAILURES` consecutive failures (default 3) it tries a restart,
limited by `WATCHDOG_RESTART_COOLDOWN` (default 120 s) and
`WATCHDOG_MAX_RESTARTS` per hour (default 5). Restarts call an
operator-provided launcher script (see `restart_provider` in
`plugboard/provider_watchdog.py`); without one, a restart fails with
`Launcher script not found` and you must restart the upstream service yourself.

## Mitigation and rollback

1. Restart a self-hosted upstream through the watchdog (only works when a
   launcher script is installed), then check the restart history:

   ```bash
   curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/watchdog/<provider_id>/restart
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     "http://localhost:8765/admin/watchdog/restarts?hours=24"
   ```

2. Take the provider out of rotation so requests go to the remaining
   providers instead of failing on it first:

   ```bash
   curl -s -X PUT -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{"is_active": false}' \
     http://localhost:8765/providers/<provider_id>
   ```

   Requests that pin this provider (`<provider_id>:<model>`) or ask for a
   model only it serves keep failing until it is back. Tell the affected
   clients to switch models in the meantime.

3. When the provider recovers, re-enable it and clear the open circuits
   instead of waiting for the cooldown:

   ```bash
   curl -s -X PUT -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
     -d '{"is_active": true}' \
     http://localhost:8765/providers/<provider_id>
   curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/circuits/<model>/reset
   ```

4. Confirm recovery: the health probe returns `"ok": true`, the watchdog shows
   `"is_healthy": true`, and `/admin/circuits` shows the models `closed`.

## Escalation

- P1 lasting more than 30 minutes: page the on-call owner of the deployment
  and post a status update to affected clients every 30 minutes (what is
  down, which models, workaround, next update time).
- Upstream outage: follow the provider's status page and open a support
  ticket with the provider if it is not acknowledged.
- Suspected Plugboard bug (provider healthy upstream but failing through the
  gateway): collect the health probe output, `/admin/watchdog/events` and the
  relevant log lines with keys redacted, then open an issue in the project
  repository.

After the incident, write a short post-mortem (timeline, root cause, impact,
follow-up actions) and add a test that would have caught the problem.

See also: [monitoring guide](../guide/monitoring.md),
[troubleshooting guide](../guide/troubleshooting.md).
