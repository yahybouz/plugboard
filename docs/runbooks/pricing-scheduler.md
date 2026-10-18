# Runbook: pricing grid stale or pricing scheduler stuck

Plugboard bills each request from a monthly pricing grid stored in
`data/pricing/YYYY_MM.json` (under `PLUGBOARD_DATA` when set). An in-process
scheduler refreshes that grid: once at startup ("bootstrap"), then daily at
every `PLUGBOARD_PRICING_REFRESH_INTERVAL_H` hours (default 6, plus up to 5
minutes of jitter), or daily at `PRICING_DAILY_HOUR_UTC` when it is `0`. Each refresh
reads the sources listed in `PLUGBOARD_PRICING_SOURCES` (your own document at
`PLUGBOARD_PRICING_SOURCE_URL`, OpenRouter, models.dev, LiteLLM, built-in
prices) and merges them model by model; see `docs/guide/pricing.md#sources`.

## Symptom

- `GET /admin/coverage/pricing` lists models under `missing`, or
  `coverage_pct` drops. Billing falls back to the previous 12 monthly grids
  and to built-in prices; a model priced nowhere is billed at 0 USD, so usage
  costs are under-reported.
- `GET /admin/pricing/scheduler/status` shows `"cb_state": "OPEN"` or
  `"DEGRADED"`, `"running": false`, or `"disabled": true`.
- Logs contain `PricingScheduler circuit breaker → OPEN until ...`,
  `PricingScheduler skipping run`, `PricingScheduler ... update timed out`,
  `PricingScheduler ... update failed` or `PricingScheduler task crashed`.
- The current month's grid file is missing or has not changed in more than a
  day.

## Priority

| Situation | Priority |
|---|---|
| Grid stale or a few models missing, requests still served | P2 |
| `cb_state` `OPEN` for longer than one cooldown (`cb_cooldown_s`, set by `PRICING_CB_COOLDOWN_S`, default 3600 s) | P2 |
| No grid file at all in `data/pricing/` after startup | P1 (requests billed at built-in prices or 0 USD) |

## Quick diagnosis

```bash
cd /path/to/plugboard
export ADMIN_KEY=$(grep '^ADMIN_KEY=' .env | cut -d= -f2-)
```

1. Scheduler state:

   ```bash
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/pricing/scheduler/status | python3 -m json.tool
   ```

   Healthy output looks like this:

   ```json
   {
     "disabled": false,
     "running": true,
     "daily_hour_utc": 3,
     "refresh_interval_h": 0,
     "mode": "daily",
     "update_timeout_s": 120,
     "cb_state": "CLOSED",
     "cb_consecutive_failures": 0,
     "cb_open_until": null,
     "cb_cooldown_s": 3600
   }
   ```

   - `disabled: true`: `PRICING_SCHEDULER_DISABLED` is set to a true value.
   - `running: false` with `disabled: false`: the background task stopped.
   - `cb_state: "DEGRADED"`: at least one recent refresh failed.
   - `cb_state: "OPEN"`: 3 consecutive refreshes failed; scheduled runs are
     skipped until `cb_open_until`, then one probe run is allowed.

2. Pricing coverage:

   ```bash
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/coverage/pricing | python3 -m json.tool
   ```

3. Current grid on disk and via the API:

   ```bash
   ls -la data/pricing/
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/pricing/$(date -u +%Y_%m) \
     | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('models', {})), 'models -', d.get('note', ''))"
   ```

   The `note` field records the date and source of the last update. A `404`
   means there is no grid for the current month yet.

4. Scheduler logs:

   ```bash
   docker compose logs --since 48h | grep -E "PricingScheduler|LiteLLM pricing|pricing source|SENTINEL"
   # or, with a log file:
   grep -E "PricingScheduler|LiteLLM pricing|pricing source|SENTINEL" "$PLUGBOARD_LOG_FILE" | tail -50
   ```

   A healthy run logs `PricingScheduler bootstrap OK` or
   `PricingScheduler daily OK` with `added`, `updated`, `inherited` and `total`
   counts.

## Common causes

### Scheduler disabled by configuration

`disabled: true`. Remove `PRICING_SCHEDULER_DISABLED` from the environment (or
set it to `0`) and restart. If you disabled it on purpose, refresh pricing
another way (see Mitigation).

### Refresh timeouts

Logs show `PricingScheduler daily update timed out after 120s` (or `bootstrap`).
The price source is slow. Raise the timeout and restart:

```bash
# .env
PRICING_UPDATE_TIMEOUT_S=300
```

Check that the new value shows up as `update_timeout_s` in the status output.

### Price sources unreachable

A refresh that cannot reach any source does not fail: it falls back to the
built-in prices and logs `Failed to fetch LiteLLM pricing`,
`LiteLLM pricing endpoint returned <code>` or
`Failed to fetch external pricing source`. A manual refresh then reports
`"source": "known_prices"`, and recently released models stay in `missing`.

Check outbound access from the Plugboard host:

```bash
curl -sI https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json | head -1
curl -sI https://openrouter.ai/api/v1/models | head -1   # only with an OpenRouter provider
curl -sI "$PLUGBOARD_PRICING_SOURCE_URL" | head -1   # only if you set it
```

Fix egress (proxy, firewall, DNS) or correct `PLUGBOARD_PRICING_SOURCE_URL`. That
document must return JSON shaped like `{"models": {"<model>": {"input_price": ..., "output_price": ...}}}`
with prices in USD per 1M tokens. A source that fails during a refresh leaves
its prices unchanged; the others still apply. If prices stop moving although the
sources answer, check `GET /admin/pricing/suggestions`: changes above
`PLUGBOARD_PRICE_DRIFT_PCT` wait there for an admin.

### Update refused by the shrink guard

Logs show `Pricing <month> SENTINEL REFUSED` and a manual refresh returns
`"persisted": false, "sentinel_refused": true`. The refresh produced a grid
less than 20% the size of the previous month's (when that month has more than
50 models), so it was not written. Usually a partial read from the price
source; retry the refresh once the source is healthy.

### Data directory not writable or grid file corrupt

Logs show `PricingScheduler ... update failed` with a file system error, or
`Failed to read pricing/<month>.json`. Check permissions and free space on the
data directory, and that the grid file is valid JSON:

```bash
python3 -m json.tool data/pricing/$(date -u +%Y_%m).json > /dev/null && echo OK
```

Move a corrupt file aside and run a manual refresh; the new grid inherits
missing entries from the previous month.

### Circuit breaker still open after the cause is fixed

The scheduler's breaker state is saved in `data/cb_state.json` inside the
installation directory (this file does not follow `PLUGBOARD_DATA`), so it
survives restarts. A manual refresh (`POST /admin/pricing/scrape`) updates the
grid but does not close the breaker. To close it:

1. Restart Plugboard. The startup bootstrap refresh always runs; if it
   succeeds the breaker returns to `CLOSED`.
2. If you need a clean slate, stop Plugboard, delete `data/cb_state.json`,
   then start it again.

## Mitigation and rollback

1. Trigger a refresh now (collects the current model list, then updates the
   current month's grid):

   ```bash
   curl -s -X POST -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/pricing/scrape | python3 -m json.tool
   ```

   The response contains `month`, `added`, `updated`, `inherited`, `total`
   and `source`. Check coverage again afterwards.

2. Or, in the dashboard, **Jobs → Run now** on *Price refresh*: it starts the
   same scheduler update in the background.

3. Last resort, price a missing model by hand. `PUT` replaces the whole grid
   for that month, so start from the current grid:

   ```bash
   MONTH=$(date -u +%Y_%m)
   curl -s -H "Authorization: Bearer $ADMIN_KEY" \
     http://localhost:8765/admin/pricing/$MONTH > grid.json
   # edit grid.json: add "<model>": {"input_price": 3.0, "output_price": 15.0} under "models"
   curl -s -X PUT -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
     --data @grid.json http://localhost:8765/admin/pricing/$MONTH | python3 -m json.tool
   ```

   Past months are never rewritten by the scheduler. Editing a grid only
   changes the cost of future requests: usage already recorded keeps the cost
   computed at the time.

4. If the scheduler itself misbehaves and a fix is not available yet, set
   `PRICING_SCHEDULER_DISABLED=1`, restart, and run `make pricing` daily until
   the fix ships. Re-enable the scheduler afterwards.

## Escalation

- Breaker `OPEN` for more than 24 hours despite the steps above: escalate to
  the deployment owner with the status output and the `PricingScheduler` log
  lines.
- Confirmed billing drift (clients charged at 0 USD or at stale prices):
  inform whoever owns client invoicing; export the affected month with
  `GET /admin/usage/<YYYY_MM>/export?format=csv` to recompute charges.
- Crash or unexplained exception in the scheduler: open an issue in the
  project repository with the stack trace (keys redacted).

See also: [pricing guide](../guide/pricing.md),
[troubleshooting guide](../guide/troubleshooting.md).
