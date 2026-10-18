# Runbook: roll back a release

Go back to the previous known-good version of Plugboard after a bad upgrade.
Works for source installs (git tags) and Docker deployments (rebuild from a
tag, or pin the previous image tag).

## Symptom

- After an upgrade, `GET /health` fails or the service does not start.
- The error rate on `POST /v1/chat/completions` jumps right after the
  deployment, or clients report new `4xx`/`5xx` responses that started at the
  deployment time.
- A security issue is found in the version that was just deployed.

## Priority

P1. Decide within 15 minutes, execute within 30. When in doubt, roll back
first and investigate afterwards.

## Quick diagnosis

Confirm that the new release is the cause, not an upstream provider (see
[provider-down.md](provider-down.md)).

```bash
cd /path/to/plugboard
export ADMIN_KEY=$(grep '^ADMIN_KEY=' .env | cut -d= -f2-)

# Running version
curl -s http://localhost:8765/health | python3 -m json.tool

# Installed version (source install)
cat VERSION
git describe --tags

# Errors since the deployment (JSON logs on stdout, or PLUGBOARD_LOG_FILE)
docker compose logs --since 30m | grep -E '"level": "(ERROR|CRITICAL)"'

# Provider health: if every provider is healthy, the gateway itself is the suspect
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/monitoring | python3 -m json.tool
```

Read the `CHANGELOG.md` entry of the new version for breaking changes or
configuration changes that were missed during the upgrade. A missing
environment variable is often fixed faster by setting it than by rolling back.

## Common causes

- A breaking change listed in `CHANGELOG.md` was not applied (new required
  variable, changed default, removed option).
- A dependency changed and `requirements.txt` was not reinstalled.
- A regression in the new release.
- Data written by the new version that the old version cannot read (only
  matters once you roll back, see step 5).

## Mitigation and rollback

### 1. Pick the target version

```bash
git fetch --tags
git tag --sort=-v:refname | head -5
```

Use the release that ran before the upgrade. With Docker images, use the
previous image tag.

### 2. Save what the bad version wrote (if it is still up)

Usage records written since the upgrade are needed for billing. Export them
before stopping anything:

```bash
curl -s -H "Authorization: Bearer $ADMIN_KEY" \
  "http://localhost:8765/admin/usage/$(date -u +%Y_%m)/export?format=csv" \
  > usage-$(date -u +%Y_%m)-before-rollback.csv
```

### 3. Stop the service and back up the data directory

The data directory (`data/` by default, or `PLUGBOARD_DATA`) holds providers,
client keys, usage, pricing grids and SQLite databases. Copy it while the
service is stopped so the SQLite files are consistent.

```bash
# Source install: stop the process (Ctrl+C in its terminal, your process
# manager, or the command below)
kill $(lsof -ti:8765)

# Docker
docker compose stop

tar czf plugboard-data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz data/
```

With Docker, archive the host directory or volume mounted on the container's
data directory (`/app/data` in the default image).

Keep `.env` unchanged, in particular `ENCRYPTION_KEY`. Provider tokens are
encrypted with it (or with `data/.encryption_key` when the variable is not
set); with a different key they cannot be read. The backup contains secrets:
store it accordingly.

### 4. Deploy the previous version

Source install:

```bash
git checkout v<previous-version>
pip install -r requirements.txt
python run.py
```

Docker, building from source:

```bash
git checkout v<previous-version>
docker compose up -d --build
```

Docker, using a published image: set the `image:` tag in your compose file to
the previous version, then:

```bash
docker compose pull
docker compose up -d
```

### 5. If the old version cannot read the current data

If the previous version fails at startup or errors on data written by the new
one, restore the backup taken before the upgrade (not the one from step 3):

```bash
# service stopped
mv data data.after-upgrade-$(date -u +%Y%m%dT%H%M%SZ)
tar xzf plugboard-data-<pre-upgrade-timestamp>.tar.gz
```

Anything created after that backup (client keys, providers, usage) is lost
from the live data; use the CSV from step 2 to reconcile billing, and
re-create keys or providers that clients still need. Always take a data
backup before an upgrade so this step stays possible.

### 6. Verify

```bash
# Expected version and "ok": true
curl -s http://localhost:8765/health | python3 -m json.tool

# One real completion with a client key (pb_..., older lle_... keys also work)
export CLIENT_KEY=pb_<your-client-key>
curl -s http://localhost:8765/v1/chat/completions \
  -H "Authorization: Bearer $CLIENT_KEY" -H "Content-Type: application/json" \
  -d '{"model": "<model-id>", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}' \
  | python3 -m json.tool

# Providers and background jobs
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/watchdog | python3 -m json.tool
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/pricing/scheduler/status | python3 -m json.tool
```

Optionally probe a set of models without changing the quarantine store
(`API_KEY` must hold a valid client key):

```bash
make smoke-models ARGS="--dry-run --only <substring>"
```

### 7. Communicate

Tell clients which version is running, when the rollback happened, what was
affected, and when the next update is expected. Clients that already adopted
something only available in the newer version (new models, new endpoints,
new options) will see errors until the fix ships.

### 8. Roll forward later

Once a fixed release exists, back up the data directory, then deploy it with
the same procedure (step 4) using the new tag or image.

## Escalation

- The rollback fails or makes things worse: restore the pre-upgrade backup
  together with the pre-upgrade version (a known-good pair) and escalate to
  the deployment owner.
- Regression confirmed in a release: open an issue in the project repository
  with the version numbers, the failing request (keys redacted) and the error
  log lines.

After the incident, write a short post-mortem (timeline, root cause, impact,
follow-up actions) and add a test that would have caught the regression
before release.

See also: [deployment guide](../guide/deployment.md).
