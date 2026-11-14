# Runbook: the production instance

How the production gateway actually runs, and the gestures that differ from
the repository files. `deploy/ovh/compose.yml` is the reference model; the
instance in service departs from it on the points listed below, on purpose.
This page names no host: replace every `<placeholder>`.

## What runs

| | |
|---|---|
| Compose project | `plugboard-private` |
| Configuration | under `/etc/plugboard`, one Compose file per version: `compose.v<version>.json` |
| Image | `plugboard:private-full-v<version>` |
| CLI logins | a volume of the container (`cli-home`, as in `deploy/ovh/compose.yml`) |
| Admin access | MFA required (`PLUGBOARD_ADMIN_MFA_REQUIRED=1`) |

## Deliberate differences with `deploy/ovh/compose.yml`

| Point | `deploy/ovh/compose.yml` | Production |
|---|---|---|
| Model catalogue scheduler | on | **off**: `MODEL_AVAILABILITY_SCHEDULER_DISABLED=1` |
| Port | published on host loopback | none: the gateway is reached through the Docker network |
| Network | created by the Compose file, fixed subnet | an existing network, reused |

The price scheduler is on (`PRICING_SCHEDULER_DISABLED=0`), as in the
repository.

**Why the catalogue scheduler is off.** At the switch to 4.14.0, the first
catalogue pass removed three models from the claude subscription provider
while that provider still served them. A codex refusal had quarantined those
model names, and the quarantine was kept by model name alone, without the
provider. The list was restored from the backup and the scheduler turned off.
4.14.1 fixes the cause: the quarantine is kept per provider (BUG-102, see the
`CHANGELOG`).

Keep the scheduler off, and do not start the catalogue job by hand (**Jobs →
Run now**), until a real test of 4.14.1 on the production data has passed.
Only then set `MODEL_AVAILABILITY_SCHEDULER_DISABLED=0`. The test procedure
is to be completed by the operator.

## Admin access

The admin key alone is refused (`401`): every admin call also needs a fresh
TOTP code, or an admin session. See
[admin API with MFA](../guide/monitoring.md#admin-api-with-mfa).

## Backup

- Taken **by hand, before a switch**, under `/var/backups/plugboard/`. It holds
  the data and the CLI home.
- **Not encrypted**, readable by root only. It holds the keys, the CLI logins
  and agy conversations in clear: never copy it elsewhere as it is.
- **No scheduled backup** on this instance to date (PLU-63).
- No full restore has been tried. The backup command and the restore
  procedure are to be completed by the operator.

## Roll back

Start the gateway again with the Compose file of the previous version,
without building:

```bash
docker compose -p plugboard-private \
  -f /etc/plugboard/compose.v<previous-version>.json \
  up -d --no-build gateway
```

When the previous version also needs an override file, add it with a second
`-f`, after the first.

This changes the image, not the data: what the newer version wrote stays in
place. If the previous version cannot read it, the backup taken before the
switch is the only restore point.

Then check the version in `/health` and run `scripts/smoke_consumers.py`: every
line must be `PASS`, except `SKIP structured` and `SKIP embeddings`, which are
expected. No port is published: the address to reach the gateway from is to be
completed by the operator.

## agy conversations

- agy keeps its conversations in `~/.gemini/antigravity-cli`, inside the CLI
  logins volume of the container.
- **Never delete that directory, nor `antigravity-oauth-token`**: they hold
  agy's sign-in.
- From 4.14.1 the gateway cleans it itself. After each agy call it deletes
  that call's files in `conversations/`, `brain/`, `annotations/` and
  `presence/`. When no agy call is running, it also deletes the shared
  summaries, `conversation_summaries.db` and `jetbox_summaries_proto.pb`.
- It does not touch `implicit/`, `log/`, `cache/` and `crashes/`. These may
  keep other traces; what to do with them is still open (BUG-97).
- A backup taken before a purge keeps the conversations of before, in clear.

## What to watch

- The size of the CLI logins volume.
- The catalogue scheduler stays off until the test above has passed.

See also: [production gate](prod-gate.md),
[rollback-release.md](rollback-release.md),
[deployment guide](../guide/deployment.md).
