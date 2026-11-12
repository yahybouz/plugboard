# Runbook: production gate (private host upgrade)

The ordered commands that move the private host (`deploy/ovh/`) to a new
release, with a way back at every step: data check, backup and its check,
build, CLI versions, smoke, a rehearsed rollback, then 24 hours of
observation. The operator runs every command; nothing here is automated.

The gate passes when the new version serves for 24 hours with **less than
1 % of `5xx` answers** on the client routes (`/v1/...`), after a green smoke
and a rollback rehearsed both ways.

## When to use

Before every production upgrade of the private host. The UAT host must
already run the same commit (`ci deployer`), with a green smoke and a
rehearsed rollback ([rollback-release.md](rollback-release.md#rehearse-the-rollback)).

## Before you start

- A shell on the host as the administrator account, with `sudo`, in
  `/opt/plugboard`. `/etc/plugboard/runtime.env` is readable by root only:
  every `docker compose` command runs with `sudo`.
- The admin key and a fresh TOTP code for each admin call (admin MFA is
  enforced; a code is accepted once).
- A **smoke key**: a client key created in the dashboard for the gate only,
  with a small monthly limit, whose `allowed_models` holds one local chat
  model and, if the host serves embeddings, the identity of
  `PLUGBOARD_EMBEDDING_MODELS` (`ollama:qwen3-embedding:0.6b` in
  `deploy/ovh/compose.yml`). Each smoke run sends four short chat calls to
  that model.
- The `age` identity of the backups, on the machine that keeps it.
- Write down, as you go: the running version and image ID (step 1), the
  backup archive name (step 3), the new version (step 4), each smoke result.

Placeholders: `<running-version>` is the version in service before the gate
(step 1), `<new-version>` the release being deployed (its `VERSION` file),
`<model>` the chat model of the smoke key.

## Steps

### 1. Record what runs now

```bash
cd /opt/plugboard
curl -s http://127.0.0.1:8765/health      # "version": <running-version>
cid="$(sudo docker compose -f deploy/ovh/compose.yml ps -q gateway)"
sudo docker inspect --format '{{.Config.Image}} {{.Image}} {{.State.Health.Status}}' "$cid"
git describe --tags --always
git status --short && git log --oneline -3
```

`git status --short` must print nothing. Local changes or local commits that
the release does not contain would be left behind by the checkout of step 4:
reconcile them with the release first.

### 2. Fetch the release and copy its tools

The smoke and the data check come from the new release; older releases do
not ship them. Copy them out of git without changing the checkout yet:

```bash
cd /opt/plugboard
git fetch --tags origin
git rev-parse v<new-version>
install -d -m 0700 ~/plugboard-tools
git show v<new-version>:scripts/precheck_data.py > ~/plugboard-tools/precheck_data.py
git show v<new-version>:scripts/smoke_consumers.py > ~/plugboard-tools/smoke_consumers.py
```

### 3. Check the data, then back it up

The data check reads the live data volume **read-only**, with the Python of
the running image as the user `app`; it prints file names, `key_id`s and
rules, never a stored value.

```bash
cd /opt/plugboard
cid="$(sudo docker compose -f deploy/ovh/compose.yml ps -q gateway)"
sudo docker run --rm --network none --user 1000:1000 --volumes-from "$cid:ro" \
  -v "$HOME/plugboard-tools/precheck_data.py:/precheck_data.py:ro" \
  --entrypoint python "$(sudo docker inspect --format '{{.Config.Image}}' "$cid")" \
  -I /precheck_data.py --data /app/data
echo "exit $?"   # 0: READY
```

Exit `1` (`NOT READY`): fix every `ERROR` line in the dashboard before going
on. A key is fixed by revoking it and creating a new one with a correct
`client_id` and `allowed_models` (written `<provider id>:<model>`), then
handing the new key to its client. Run the check again until it prints
`READY`. Read the `WARN` lines too (`--strict` makes them fail).

Then take a cold backup (the gateway stops for a few seconds) and check it:

```bash
cd /opt/plugboard
sudo bash deploy/ovh/backup.sh            # prints the archive path: write it down
sudo sha256sum -c <archive-path>.sha256   # <archive-path>: OK
bash deploy/ovh/verify.sh                 # the gateway is back: every line PASS
```

On the machine that keeps the `age` identity, pull the archive (the same
variables as the scheduled copy; `PLUGBOARD_BACKUP_DIRECTORY` if it is not the
default `~/.local/share/plugboard-backups`) and prove it can be decrypted,
without writing the plaintext anywhere:

```bash
PLUGBOARD_BACKUP_SSH_TARGET=<user@host> PLUGBOARD_HOST_KEY_ALIAS=<host-key-alias> \
  bash deploy/ovh/sync-backups.sh         # "Encrypted backups verified: N"
age --decrypt -i <identity-file> ~/.local/share/plugboard-backups/plugboard-<stamp>.tar.gz.age \
  | tar -tzf - | grep -c -E '^(app/data|home/app|backup-config)/'
```

A count above zero means the archive decrypts and holds the data, the CLI
logins and the runtime configuration. A failure here stops the gate: there
is no way back without a backup.

### 4. Keep the running image, then build the new one

Give the running image a tag of its own, so the build cannot take its name:

```bash
cd /opt/plugboard
cid="$(sudo docker compose -f deploy/ovh/compose.yml ps -q gateway)"
sudo docker image tag "$(sudo docker inspect --format '{{.Image}}' "$cid")" \
  plugboard:rollback-<running-version>
sudo docker image ls plugboard
```

Build the release without switching yet (a failed build changes nothing):

```bash
cd /opt/plugboard
git checkout v<new-version>
cat VERSION                                # <new-version>
sudo docker compose -f deploy/ovh/compose.yml config --quiet
sudo env PLUGBOARD_IMAGE=plugboard:private-full-v<new-version> \
  docker compose -f deploy/ovh/compose.yml build gateway
```

The build fails, on purpose, when a CLI cannot be installed or does not
start (BUG-50). Then stay on the running version: nothing was switched.

### 5. Check the CLI versions of the new image

In a throwaway container of the new image, as the user `app`, without
network, before switching:

```bash
sudo docker run --rm --network none --entrypoint sh \
  plugboard:private-full-v<new-version> -ec \
  'id -u; gemini --version; claude --version; codex --version; gh --version; agy --version'
```

Expected: `1000`, then `0.65.0`, `2.1.306 (Claude Code)`, `codex-cli 0.163.0`,
a `gh version` line and `1.4.4`: the versions pinned in the `Dockerfile` and
in `deploy/ovh/compose.yml`. Another version or an error stops the gate.

### 6. Switch to the new version

```bash
cd /opt/plugboard
sudo env PLUGBOARD_IMAGE=plugboard:private-full-v<new-version> \
  docker compose -f deploy/ovh/compose.yml up -d --no-build gateway
# Allow about 2 minutes, until it prints the new image and "healthy"
sudo docker inspect --format '{{.Config.Image}} {{.State.Health.Status}}' \
  "$(sudo docker compose -f deploy/ovh/compose.yml ps -q gateway)"
# The CLI logins kept in the cli-home volume still work
sudo docker compose -f deploy/ovh/compose.yml exec gateway sh -ec \
  'claude --version; codex --version; gemini --version'
```

### 7. Smoke the new version

```bash
read -rs PLUGBOARD_SMOKE_KEY && export PLUGBOARD_SMOKE_KEY
python3 ~/plugboard-tools/smoke_consumers.py --url http://127.0.0.1:8765 \
  --model <model> --expect-version <new-version>
echo "exit $?"   # 0, in less than 60 s
bash deploy/ovh/verify.sh
```

Every line must be `PASS` (`SKIP embeddings` only when the smoke key holds
no embedding model). A `FAIL` means: go back now with step 8, first part,
and stop the gate.

### 8. Rehearse the rollback, then go forward again

Go back to the kept image, smoke it, then forward to the new one and smoke
it again. Make no admin change (keys, providers, prices) during this step.

```bash
cd /opt/plugboard
# Back
sudo env PLUGBOARD_IMAGE=plugboard:rollback-<running-version> \
  docker compose -f deploy/ovh/compose.yml up -d --no-build gateway
sudo docker inspect --format '{{.Config.Image}} {{.State.Health.Status}}' \
  "$(sudo docker compose -f deploy/ovh/compose.yml ps -q gateway)"
python3 ~/plugboard-tools/smoke_consumers.py --url http://127.0.0.1:8765 \
  --model <model> --expect-version <running-version>

# Forward
sudo env PLUGBOARD_IMAGE=plugboard:private-full-v<new-version> \
  docker compose -f deploy/ovh/compose.yml up -d --no-build gateway
sudo docker inspect --format '{{.Config.Image}} {{.State.Health.Status}}' \
  "$(sudo docker compose -f deploy/ovh/compose.yml ps -q gateway)"
python3 ~/plugboard-tools/smoke_consumers.py --url http://127.0.0.1:8765 \
  --model <model> --expect-version <new-version>
```

Wait for `healthy` before each smoke. Both smokes must exit `0`. If the
previous version fails on data the new one wrote, note it: a real rollback
would then need the restore of
[rollback-release.md, step 5](rollback-release.md#5-if-the-old-version-cannot-read-the-current-data).

Without `PLUGBOARD_IMAGE`, compose starts the default image of
`deploy/ovh/compose.yml`. Check that it is the new one, so that a later
plain `docker compose ... up -d` cannot start the previous version:

```bash
sudo docker compose -f deploy/ovh/compose.yml config --images
```

It must print `plugboard:private-full-v<new-version>`. If it prints another
name, the release did not update `deploy/ovh/compose.yml`: until it does,
always pass `PLUGBOARD_IMAGE=plugboard:private-full-v<new-version>` as above.

### 9. Observe for 24 hours

Right after the last switch, and again 24 hours later, read the gateway's
counters (admin key and a fresh TOTP code each time; they restart at zero
with the container):

```bash
read -rs ADMIN_KEY
curl -s -H "Authorization: Bearer $ADMIN_KEY" -H "X-Plugboard-Admin-TOTP: <code>" \
  http://127.0.0.1:8765/metrics | python3 -m json.tool
```

`/metrics` counts provider calls and provider errors (`requests`,
`errors`), not HTTP answers. The gate criterion is counted on the HTTP
answers of the access log, 24 hours after the last switch:

```bash
cd /opt/plugboard
sudo docker compose -f deploy/ovh/compose.yml logs --no-log-prefix --since 24h gateway \
  | awk -F'"' '
    $2 ~ /^[A-Z]+ \/[^ ]* HTTP\/[0-9.]+$/ && $3 ~ /^ [0-9][0-9][0-9]( |$)/ {
      split($2, req, " "); code = substr($3, 2, 3); all++; if (code ~ /^5/) all5++
      if (req[2] ~ /^\/v1\//) { api++; if (code ~ /^5/) api5++ }
    }
    END {
      printf "all routes: %d requests, %d 5xx (%.2f %%)\n", all, all5, all ? 100 * all5 / all : 0
      printf "/v1 routes: %d requests, %d 5xx (%.2f %%)\n", api, api5, api ? 100 * api5 / api : 0
    }'
```

The gate passes when the `/v1 routes` line shows less than `1.00 %`.
`0 requests` on both lines means the access log was not found (another log
format, or the container was recreated within the 24 hours): count again
from the last recreation, and note it.

### 10. Close the gate

- Write down the results: versions, image IDs, archive name, the three smoke
  outputs, the `/metrics` snapshots and the 5xx rate.
- Keep `plugboard:rollback-<running-version>` and the pre-gate backup until
  the next release passes its own gate.
- Revoke the smoke key in the dashboard if it will not be used again, and
  remove the copied tools: `rm -f ~/plugboard-tools/*.py`.

## If a step fails

| Step | Failure | Do |
|---|---|---|
| 3 | the data check is not `READY` | fix the keys, run it again; do not go on |
| 3 | the backup, its checksum or its decryption fails | stop: no upgrade without a proven backup |
| 4 | the build fails | stay on the running version; report the failing CLI or step |
| 5 | a CLI version differs or does not start | stop; the image is not the one the CI checked |
| 6 | the container does not turn `healthy` | go back: step 8, first command block, then the smoke |
| 7 | the smoke fails | go back (step 8, first block); keep the smoke output |
| 8 | the previous version fails its smoke | go forward again; the way back needs the restore of rollback-release.md step 5: escalate before relying on it |
| 9 | 1 % or more `5xx` on `/v1` | go back (step 8, first block), then investigate with [rollback-release.md](rollback-release.md) |

## Escalation

Hand over the step number, the command, its output (keys redacted) and the
notes of step 10 to the deployment owner. A failure at step 8 or 9 is also
reported as a bug of the release.

See also: [rollback-release.md](rollback-release.md),
[deploy/ovh/README.md](../../deploy/ovh/README.md).
