# Runbook: roll back a release

Go back to the previous known-good version of Plugboard after a bad upgrade.
The procedure depends on how the version was deployed:

| Deployment | Goes back with | Section |
|---|---|---|
| UAT host, deployed by `ci deployer` | `ci deployer plugboard <previous sha>`, which reuses the previous image | [4a](#4a-uat-host-ci-deployer) |
| Production instance, Compose project `plugboard-private` | the Compose file of the previous version under `/etc/plugboard`, and the previous image checked by its ID | [4b](#4b-production-instance-plugboard-private) |
| Source install, other Docker deployments | the previous git tag, or the previous image tag | [4c](#4c-source-install-and-other-docker-deployments) |

The production instance is described in
[production-instance.md](production-instance.md); its deployment files are
not in the repository. What could not be checked against its host is marked
**to be confirmed by the operator**. The former private host `deploy/ovh/`
(end of life) has its own section at the end:
[former private host](#former-private-host-deployovh).

`ci` is the local CI tool of the UAT host (`python3 /srv/dev/.ci/outils/ci.py`,
subcommands `etat`, `lancer`, `deployer`). There is no remote CI pipeline.

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

On the production instance no port is published: `/health` is read inside
the gateway container ([4b](#4b-production-instance-plugboard-private)),
admin calls go to the address used for them (`<gateway-url>`, with a fresh
TOTP code: [admin API with MFA](../guide/monitoring.md#admin-api-with-mfa)),
the configuration is under `/etc/plugboard`, and the logs come from
`sudo docker compose -p plugboard-private -f /etc/plugboard/compose.v<new-version>.json logs --since 30m gateway`
(`<new-version>`: the version in service, the one to leave).

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

Older releases do not ship the smoke nor the data check. Before any
`git checkout`, copy both out of the checkout of the release that has them,
and run them from that copy in every step below:

```bash
install -d -m 0700 ~/plugboard-tools
cp scripts/smoke_consumers.py scripts/precheck_data.py ~/plugboard-tools/
```

### 1. Pick the target version

Use the release that ran before the upgrade: the commit SHA on the UAT host,
the Compose file and the image ID written down before the upgrade on the
production instance ([production gate, step 1](prod-gate.md#1-record-what-runs-now-and-pause-the-nightly-backup)),
the git tag or image tag elsewhere.

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
databases. On the production instance, take a cold backup with the script
the nightly timer runs, from the repository checkout on the host
(`<checkout>`, see [production gate](prod-gate.md#before-you-start)). It
stops the gateway, allowing it up to 40 seconds to stop, archives data, CLI
logins and `/etc/plugboard/runtime.env`, encrypts the archive with `age`
under `/var/backups/plugboard/`, and restarts the gateway (about two
minutes):

```bash
cd <checkout>
sudo bash deploy/ovh/backup.sh            # prints the archive path
```

Elsewhere, stop the service and copy the data directory, so the SQLite files
are consistent. Source install, from the installation directory:

```bash
# Stop the process (Ctrl+C in its terminal, your process manager, or:)
kill $(lsof -ti:8765)
tar czf plugboard-data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz data/
```

Default Docker Compose deployment (`docker-compose.yml`, service `api`, data
in a named volume): archive the volume through a throwaway container.

```bash
docker compose stop
docker run --rm --user 0:0 --network none \
  --volumes-from "$(docker compose ps -aq api):ro" \
  --entrypoint tar plugboard:api -C /app -czf - data \
  > plugboard-data-$(date -u +%Y%m%dT%H%M%SZ).tar.gz
```

`plugboard:api` is the image named in `docker-compose.yml`; use yours if you
changed it.

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
| `1`, `2` | the deployer did not run (sudo refused, unknown application) | escalate |
| `3` | refused: SHA unknown, or no green CI report for it (only the last 30 reports are kept) | `ci lancer plugboard <previous-sha>`, wait for green, run `ci deployer` again |
| `4` | the source export or the image build failed, or the image variable is missing: nothing was switched | the output names the deployer's build log (`/srv/plugboard/build-<sha12>.log`, readable by the host owner); the bad version is still in service |
| `5` | the previous SHA did not turn healthy; the deployer put back the version that was running | escalate |
| `6` | the automatic rollback failed too | escalate at once: manual intervention on the host |
| `75` | another deployment is running | wait for it, then retry |

The previous SHA is the `sha_en_service` that `--etat` printed before the
upgrade: write it down before every deployment. The deployer keeps no list
of past deployments that the agent account can read.

The UAT volumes belong to the host owner: if the previous version cannot read
the data (step 5), escalate instead of restoring them yourself.

### 4b. Production instance (`plugboard-private`)

The instance in service ([production-instance.md](production-instance.md)):
Compose project `plugboard-private`, one Compose file per version under
`/etc/plugboard` (`compose.v<version>.json`). Rolling back starts the Compose
file of the previous version, without building, on the previous image
checked by its ID. The commands run Docker with `sudo`, as in the
[production gate](prod-gate.md#before-you-start); which account runs Docker
on that host is to be confirmed by the operator.

Before every upgrade (the version then in service is `<previous-version>`),
write down the ID of its image, `<previous-image-id>`, and keep its Compose
file unchanged ([production gate, step 1](prod-gate.md#1-record-what-runs-now-and-pause-the-nightly-backup)).
The gate also tags that image `plugboard:rollback-<previous-version>`, so
that no later tag can take it away.

To roll back:

```bash
prev() { sudo docker compose -p plugboard-private -f /etc/plugboard/compose.v<previous-version>.json "$@"; }
old_id=<previous-image-id>
sudo docker image inspect --format '{{.Id}}' "$old_id"   # still on the host
img="$(prev config --images)"             # the image that file starts
# That name must still be the previous image: put it back on the ID if a tag moved
[ "$(sudo docker image inspect --format '{{.Id}}' "$img")" = "$old_id" ] \
  || sudo docker image tag "$old_id" "$img"
prev up -d --no-build gateway
# Must print the image of that file and healthy (allow about 2 minutes)
sudo docker inspect --format '{{.Config.Image}} {{.State.Health.Status}}' "$(prev ps -q gateway)"
[ "$(sudo docker inspect --format '{{.Image}}' "$(prev ps -q gateway)")" = "$old_id" ] \
  && echo "running the previous image" || echo "STOP: not the previous image"
```

This is the command used on 15 November 2026 to go back from the broken
first 4.14.2 image to 4.14.1, in about 20 seconds (INC-4). When the previous
version also needs an override file, add it with a second `-f`, after the
first. This changes the image, not the data: what the newer version wrote
stays in place; if the previous version cannot read it, see
[step 5](#5-if-the-old-version-cannot-read-the-current-data).

If `<previous-image-id>` is no longer on the host, the image must be built
again from the previous release (the `full` stage, as in
[production gate, step 4](prod-gate.md#4-keep-the-running-image-then-get-the-new-one));
its ID then differs from the one written down, and only the checks of the
gate's step 5 vouch for it. How the operator gets that image is to be
confirmed by the operator.

**No port is published**: read the version from inside the gateway
container, then run the smoke there too ([step 6](#6-verify)):

```bash
sudo docker exec "$(prev ps -q gateway)" python -c \
  "import os, urllib.request; print(urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8765'), timeout=5).read().decode())"
# "version": <previous-version>
```

Going forward again: start the Compose file of the newer version, then
check that the container runs the image ID written down at the gate's step 4
(`<new-image-id>`):

```bash
sudo docker compose -p plugboard-private -f /etc/plugboard/compose.v<new-version>.json \
  up -d --no-build gateway
sudo docker inspect --format '{{.Image}}' \
  "$(sudo docker compose -p plugboard-private -f /etc/plugboard/compose.v<new-version>.json ps -q gateway)"
# Expected: <new-image-id>
```

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

Production instance: **no restore has been rehearsed** (PLU-63), and the
restore procedure is to be confirmed by the operator. Until one is rehearsed,
a backup is not a proven restore point: escalate before relying on it. What
is known:

- The archives of `deploy/ovh/backup.sh` (`plugboard-<stamp>.tar.gz.age`,
  under `/var/backups/plugboard/`: the nightly ones and those taken by hand
  before the switches to 4.14.1 and 4.14.2) are encrypted with `age` for the
  recipient in `/etc/plugboard/backup.recipient`; the private key is not on
  the server. They hold `app/data`, `home/app` and
  `backup-config/runtime.env`, as on the former host, whose restore commands
  ([below](#restore-on-the-former-host)) are the model: stop the gateway with
  the Compose file of the version in service, extract into its volumes
  through `--volumes-from`, then start the previous version as in
  [4b](#4b-production-instance-plugboard-private).
- The archive taken by hand before 4.14.0
  (`pre-4.14.0-<stamp>.tar.gz`, same directory) is **not encrypted** and
  holds the data and the CLI home in clear, readable by root only: never
  copy it elsewhere as it is. Its layout is to be confirmed by the operator.

Default Docker Compose deployment, with the archive of step 3 taken before
the upgrade:

```bash
docker compose stop
docker run --rm -i --user 0:0 --network none \
  --volumes-from "$(docker compose ps -aq api)" \
  --entrypoint sh plugboard:api -ec 'find /app/data -mindepth 1 -delete; tar -C /app -xzpf -' \
  < plugboard-data-<pre-upgrade-timestamp>.tar.gz
docker compose up -d
```

Source install:

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
whose `allowed_models` holds one local chat model and, where embeddings are
served, the identity listed in `PLUGBOARD_EMBEDDING_MODELS`; never an admin
key. It must pass within 60 seconds and report the version you rolled back
to. Run it from the copy made at the start (older releases do not ship it):

```bash
read -rs PLUGBOARD_SMOKE_KEY && export PLUGBOARD_SMOKE_KEY
python3 ~/plugboard-tools/smoke_consumers.py --url http://127.0.0.1:8765 \
  --model <model-of-the-smoke-key> --expect-version <previous-version>
echo "exit $?"   # 0: every check passed
```

When the smoke key holds the embedding identity, add `--require-embeddings`
so that a missing embedding route or model fails instead of being skipped.

On the UAT host the gateway listens on `http://127.0.0.1:18765`. On the
production instance **no port is published**: run the smoke inside the
gateway container with the `smoke` helper of the
[production gate](prod-gate.md#before-you-start) (`smoke <previous-version>`;
any Compose file of the project finds the gateway), and send the admin calls
below to `<gateway-url>` with a fresh TOTP code.

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

Once a fixed release exists, back up the data, run the data check
(`precheck_data.py`) on a copy of it, then deploy it with the same procedure
(step 4) and run the smoke again.

## Rehearse the rollback

A rollback that was never run is not a rollback. Before a production
upgrade, run it once on the UAT host, target version, then previous, then
target again, with a green smoke at each step:

```bash
smoke() { python3 ~/plugboard-tools/smoke_consumers.py --url http://127.0.0.1:18765 --model <model> --expect-version "$1"; }
ci deployer plugboard <target-sha>   && smoke <target-version>
ci deployer plugboard <previous-sha> && smoke <previous-version>
ci deployer plugboard <target-sha>   && smoke <target-version>
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

## Former private host (deploy/ovh)

This host is end of life: its gateway was stopped on 14 November 2026 (see
[deploy/ovh/README.md](../../deploy/ovh/README.md)). The steps below apply
only if it serves again. Its `.env` is `/etc/plugboard/runtime.env`, its
logs come from `docker compose -f deploy/ovh/compose.yml logs --since 30m
gateway`, and after a rollback also run `bash deploy/ovh/verify.sh`
(authentication answers, no model call, on its published port).

### Roll back on the former host

`/etc/plugboard/runtime.env` is readable by root only, so every
`docker compose` command runs with `sudo`, and the image is passed on each
command line with `sudo env PLUGBOARD_IMAGE=...` (an `export` does not cross
`sudo`). Without `PLUGBOARD_IMAGE`, compose falls back to the default of
`deploy/ovh/compose.yml`.

Before every upgrade, give the running image a tag of its own, then build the
new release under a new tag, so the previous image stays on the host:

```bash
cd /opt/plugboard
cid="$(sudo docker compose -f deploy/ovh/compose.yml ps -q gateway)"
curl -s http://127.0.0.1:8765/health   # note the running "version"
sudo docker image tag "$(sudo docker inspect --format '{{.Image}}' "$cid")" \
  plugboard:rollback-<running-version>

# The upgrade itself, after git checkout of the new release
sudo env PLUGBOARD_IMAGE=plugboard:private-full-v<new-version> \
  docker compose -f deploy/ovh/compose.yml up -d --build gateway
```

To roll back, start the kept image without building:

```bash
cd /opt/plugboard
sudo docker image ls plugboard   # plugboard:rollback-<previous-version> must be listed
sudo env PLUGBOARD_IMAGE=plugboard:rollback-<previous-version> \
  docker compose -f deploy/ovh/compose.yml up -d --no-build gateway

# Must print plugboard:rollback-<previous-version> healthy (allow about 2 minutes)
sudo docker inspect --format '{{.Config.Image}} {{.State.Health.Status}}' \
  "$(sudo docker compose -f deploy/ovh/compose.yml ps -q gateway)"
```

This runs the previous image with the current `compose.yml`. If the upgrade
also changed `deploy/ovh/compose.yml` in a way the previous version cannot
run with, use the previous release's compose file. A release older than the
`PLUGBOARD_IMAGE` variable names its image in the file itself and ignores the
variable: give the kept image that name first.

```bash
cd /opt/plugboard
git checkout v<previous-version>
grep -n 'image:' deploy/ovh/compose.yml
# Only when that line is a fixed name such as plugboard:private-full-v4.4.0:
sudo docker image tag plugboard:rollback-<previous-version> <that-fixed-name>
sudo env PLUGBOARD_IMAGE=plugboard:rollback-<previous-version> \
  docker compose -f deploy/ovh/compose.yml up -d --no-build gateway
```

No kept image on the host (it was removed, or not tagged before the
upgrade): rebuild it from the previous release. This takes several minutes
and downloads the CLIs again; the compose file of that release names the
resulting image.

```bash
cd /opt/plugboard
git checkout v<previous-version>
sudo env PLUGBOARD_IMAGE=plugboard:rollback-<previous-version> \
  docker compose -f deploy/ovh/compose.yml up -d --build gateway
```

In every case, [step 6](#6-verify) must report `<previous-version>`: the
image name alone does not prove which version runs.

Going forward again: if you checked out the previous release, run
`git checkout v<new-version>` first (an older compose file ignores
`PLUGBOARD_IMAGE`), then the same `up -d --no-build` command with
`PLUGBOARD_IMAGE=plugboard:private-full-v<new-version>`, and the smoke with
`--expect-version <new-version>`.

### Restore on the former host

The archives of `deploy/ovh/backup.sh` are encrypted with
`age` to the recipient in `/etc/plugboard/backup.recipient`; only the holder
of the matching identity can decrypt them, on the machine that keeps it,
where `deploy/ovh/sync-backups.sh` copies the archives. The decrypted archive
holds every secret (runtime configuration, provider tokens, CLI logins):
keep it owner-only and delete every plaintext copy at the end.

On the machine that keeps the identity, from the backup directory:

```bash
umask 077
archive=plugboard-<pre-upgrade-stamp>.tar.gz.age
# The .sha256 file names the archive by its path on the host: check by name.
echo "$(cut -d' ' -f1 "$archive.sha256")  $archive" | shasum -a 256 -c -
age --decrypt -i <identity-file> "$archive" > plugboard-restore.tar.gz
scp plugboard-restore.tar.gz <host>:plugboard-restore.tar.gz
rm -f plugboard-restore.tar.gz
```

On the host, from `/opt/plugboard`: stop the gateway, empty the data and
CLI-login volumes, extract `app/data` and `home/app` from the archive (its
`backup-config/runtime.env` is not extracted), start the previous image, then
delete the plaintext copy.

```bash
chmod 600 ~/plugboard-restore.tar.gz
cd /opt/plugboard
sudo docker compose -f deploy/ovh/compose.yml stop gateway
cid="$(sudo docker compose -f deploy/ovh/compose.yml ps -aq gateway)"
sudo docker run --rm -i --user 0:0 --network none --volumes-from "$cid" \
  --entrypoint sh "$(sudo docker inspect --format '{{.Config.Image}}' "$cid")" -ec \
  'find /app/data /home/app -mindepth 1 -delete; tar -C / -xzpf - app/data home/app' \
  < ~/plugboard-restore.tar.gz
sudo env PLUGBOARD_IMAGE=plugboard:rollback-<previous-version> \
  docker compose -f deploy/ovh/compose.yml up -d --no-build gateway
rm -f ~/plugboard-restore.tar.gz
```

See also: [deployment guide](../guide/deployment.md),
[production gate](prod-gate.md),
[production instance](production-instance.md).
