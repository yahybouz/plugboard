# Runbook: roll back a release

Go back to the previous known-good version of Plugboard after a bad upgrade.
The procedure depends on how the version was deployed:

| Deployment | Goes back with | Section |
|---|---|---|
| UAT host, deployed by `ci deployer` | `ci deployer plugboard <previous sha>`, which reuses the previous image | [4a](#4a-uat-host-ci-deployer) |
| Private host `deploy/ovh/` | the previous image, kept under its own tag before the upgrade | [4b](#4b-private-host-deployovh) |
| Source install, other Docker deployments | the previous git tag, or the previous image tag | [4c](#4c-source-install-and-other-docker-deployments) |

Every way ends with the same check: `scripts/smoke_consumers.py` green
against the version you rolled back to ([step 6](#6-verify)).

## Symptom

- After an upgrade, `GET /health` fails or the service does not start.
- The error rate on `POST /v1/chat/completions` jumps right after the
  deployment, or clients report new `4xx`/`5xx` responses that started at the
  deployment time.
- The post-deployment smoke (`scripts/smoke_consumers.py`) fails.
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

On the private host, `.env` is `/etc/plugboard/runtime.env` and the logs come
from `docker compose -f deploy/ovh/compose.yml logs --since 30m gateway`.

Read the `CHANGELOG.md` entry of the new version for breaking changes or
configuration changes that were missed during the upgrade. A missing
environment variable is often fixed faster by setting it than by rolling back.

## Common causes

- A breaking change listed in `CHANGELOG.md` was not applied (new required
  variable, changed default, removed option).
- A client key policy whose meaning changed between the versions (run
  `scripts/precheck_data.py` on a copy of the data before the next attempt).
- A dependency changed and `requirements.txt` was not reinstalled.
- A regression in the new release.
- Data written by the new version that the old version cannot read (only
  matters once you roll back, see step 5).

## Mitigation and rollback

### 1. Pick the target version

Use the release that ran before the upgrade: the commit SHA on the UAT host,
the image kept before the upgrade on the private host, the git tag or image
tag elsewhere.

```bash
git fetch --tags
git tag --sort=-v:refname | head -5
```

### 2. Save what the bad version wrote (if it is still up)

Usage records written since the upgrade are needed for billing. Export them
before stopping anything:

```bash
curl -s -H "Authorization: Bearer $ADMIN_KEY" \
  "http://localhost:8765/admin/usage/$(date -u +%Y_%m)/export?format=csv" \
  > usage-$(date -u +%Y_%m)-before-rollback.csv
```

### 3. Back up the data as it is now

The data directory (`data/` by default, or `PLUGBOARD_DATA`; `/app/data` in
the image) holds providers, client keys, usage, pricing grids and SQLite
databases. On the private host, take a cold backup (it stops the gateway for
a few seconds, archives data, CLI logins and runtime configuration, and
restarts it):

```bash
cd /opt/plugboard
sudo bash deploy/ovh/backup.sh
```

Elsewhere, stop the service and copy the data directory, so the SQLite files
are consistent:

```bash
# Source install: stop the process (Ctrl+C in its terminal, your process
# manager, or the command below)
kill $(lsof -ti:8765)

# Docker
docker compose stop

tar czf plugboard-data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz data/
```

Keep the environment unchanged, in particular `ENCRYPTION_KEY`. Provider
tokens are encrypted with it (or with `data/.encryption_key` when the
variable is not set); with a different key they cannot be read. The backup
contains secrets: store it accordingly.

### 4a. UAT host (`ci deployer`)

`ci deployer` deploys a commit whose local CI is green, builds the image
`plugboard:uat-<sha7>` only when it is missing, and never touches the volumes
or the secrets. Rolling back is deploying the previous SHA again: its image
is still there, so nothing is rebuilt.

```bash
# SHA in service, image, CI status of the SHA asked for
ci deployer plugboard <previous-sha> --etat

# Roll back
ci deployer plugboard <previous-sha>
echo "exit $?"
```

| Exit | Meaning | Next |
|---|---|---|
| `0` | the previous SHA is in service and healthy | [step 6](#6-verify) |
| `3` | refused: SHA unknown, or no green CI report for it (only the last 30 reports are kept) | `ci lancer plugboard <previous-sha>`, wait for green, run `ci deployer` again |
| `4` | the image build failed: nothing was switched | `ci journal plugboard <previous-sha>`; the bad version is still in service |
| `5` | the previous SHA did not turn healthy; the deployer put back the version that was running | escalate |
| `6` | the automatic rollback failed too | escalate at once: manual intervention on the host |
| `75` | another deployment is running | wait for it, then retry |

The previous SHA is the `sha_en_service` that `--etat` printed before the
upgrade: write it down before every deployment. The deployer keeps no list
of past deployments that the agent account can read.

The UAT volumes belong to the host owner: if the previous version cannot read
the data (step 5), escalate instead of restoring them yourself.

### 4b. Private host (`deploy/ovh/`)

The gateway runs the image named by `PLUGBOARD_IMAGE` (default in
`deploy/ovh/compose.yml`). Before every upgrade, give the running image a tag
of its own, so the upgrade builds a new tag and the previous image stays on
the host:

```bash
cd /opt/plugboard
cid="$(docker compose -f deploy/ovh/compose.yml ps -q gateway)"
curl -s http://127.0.0.1:8765/health   # note the running "version"
docker image tag "$(docker inspect --format '{{.Image}}' "$cid")" \
  plugboard:rollback-<running-version>
```

To roll back:

```bash
cd /opt/plugboard
# The image kept before the upgrade
docker image ls plugboard

# Start the previous image, without building
export PLUGBOARD_IMAGE=plugboard:rollback-<previous-version>
docker compose -f deploy/ovh/compose.yml up -d --no-build gateway

# The container must run that image and turn healthy (up to about 2 minutes)
docker inspect --format '{{.Config.Image}} {{.State.Health.Status}}' \
  "$(docker compose -f deploy/ovh/compose.yml ps -q gateway)"
```

This uses the current `compose.yml` with the previous image. If the upgrade
also changed `deploy/ovh/compose.yml` in a way the previous version cannot
run with, check out the previous release first (`git checkout v<previous-version>`)
and run the same `up` command.

No previous image on the host (it was removed, or not tagged before the
upgrade): rebuild it from the previous release. This takes several minutes
and downloads the CLIs again.

```bash
cd /opt/plugboard
git checkout v<previous-version>
export PLUGBOARD_IMAGE=plugboard:rollback-<previous-version>
docker compose -f deploy/ovh/compose.yml up -d --build gateway
```

Going forward again is the same command with the new image:
`export PLUGBOARD_IMAGE=plugboard:private-full-v<new-version>`, then
`docker compose -f deploy/ovh/compose.yml up -d --no-build gateway`.

### 4c. Source install and other Docker deployments

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
one, restore the backup taken before the upgrade (not the one from step 3).

Private host: the archives of `deploy/ovh/backup.sh` are encrypted with
`age` to the recipient in `/etc/plugboard/backup.recipient`; only the holder
of the matching identity can decrypt them. Decrypt the pre-upgrade archive
where the identity is kept, copy the result to `/opt/plugboard` on the host
(owner-only, it holds secrets), then, with the gateway stopped, empty the two
volumes and extract the data and the CLI logins into them. The runtime
configuration in the archive (`backup-config/runtime.env`) is not extracted.

```bash
# Where the age identity is kept
sha256sum -c plugboard-<pre-upgrade-stamp>.tar.gz.age.sha256
age --decrypt -i <identity-file> plugboard-<pre-upgrade-stamp>.tar.gz.age \
  > plugboard-<pre-upgrade-stamp>.tar.gz

# On the host, from /opt/plugboard, gateway stopped
docker compose -f deploy/ovh/compose.yml stop gateway
cid="$(docker compose -f deploy/ovh/compose.yml ps -aq gateway)"
docker run --rm -i --user 0:0 --network none --volumes-from "$cid" \
  --entrypoint sh "$(docker inspect --format '{{.Config.Image}}' "$cid")" -ec \
  'find /app/data /home/app -mindepth 1 -delete; tar -C / -xzpf - app/data home/app' \
  < plugboard-<pre-upgrade-stamp>.tar.gz
docker compose -f deploy/ovh/compose.yml up -d --no-build gateway
rm -f plugboard-<pre-upgrade-stamp>.tar.gz
```

Elsewhere:

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

The smoke reads its key from `PLUGBOARD_SMOKE_KEY`: a dedicated client key
limited to one local model (`allowed_models`), never an admin key. It must
pass within 60 seconds and report the version you rolled back to.

```bash
read -rs PLUGBOARD_SMOKE_KEY && export PLUGBOARD_SMOKE_KEY
python3 scripts/smoke_consumers.py --url http://127.0.0.1:8765 \
  --model <model-of-the-smoke-key> --expect-version <previous-version>
echo "exit $?"   # 0: every check passed
```

On the UAT host the gateway listens on `http://127.0.0.1:18765`. On the
private host also run `bash deploy/ovh/verify.sh` (authentication answers,
no model call).

```bash
# Providers and background jobs
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/watchdog | python3 -m json.tool
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/pricing/scheduler/status | python3 -m json.tool
```

### 7. Communicate

Tell clients which version is running, when the rollback happened, what was
affected, and when the next update is expected. Clients that already adopted
something only available in the newer version (new models, new endpoints,
new options) will see errors until the fix ships.

### 8. Roll forward later

Once a fixed release exists, back up the data, run
`scripts/precheck_data.py` on a copy of it, then deploy it with the same
procedure (step 4) and run the smoke again.

## Rehearse the rollback

A rollback that was never run is not a rollback. Before a production
upgrade, run it once on the UAT host, target version, then previous, then
target again, with a green smoke at each step:

```bash
ci deployer plugboard <target-sha>   && python3 scripts/smoke_consumers.py --url http://127.0.0.1:18765 --model <model> --expect-version <target-version>
ci deployer plugboard <previous-sha> && python3 scripts/smoke_consumers.py --url http://127.0.0.1:18765 --model <model> --expect-version <previous-version>
ci deployer plugboard <target-sha>   && python3 scripts/smoke_consumers.py --url http://127.0.0.1:18765 --model <model> --expect-version <target-version>
```

## Escalation

- The rollback fails or makes things worse: restore the pre-upgrade backup
  together with the pre-upgrade version (a known-good pair) and escalate to
  the deployment owner.
- `ci deployer` exits `5` or `6`: hand over the exit code and the output of
  `ci deployer plugboard <sha> --etat` to the UAT host owner.
- Regression confirmed in a release: open an issue in the project repository
  with the version numbers, the failing request (keys redacted) and the error
  log lines.

After the incident, write a short post-mortem (timeline, root cause, impact,
follow-up actions) and add a test that would have caught the regression
before release.

See also: [deployment guide](../guide/deployment.md).
