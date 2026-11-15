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

## Client address and rate limit (BUG-119)

The chain is `tailscale serve` → nginx on the host → the gateway on the
existing Docker network. As set up before 4.14.3, the gateway sees every
request from one address, nginx's on that network (for example `172.18.0.1`):
60 calls without a key in a minute blocked every client, and wrong logins
locked out everyone.

Since 4.14.3 a request with a valid key (a client key, the admin key or an
admin session) is counted under its key, not its address, and wrong TOTP codes
lock the admin key, not an address. What still counts per address: requests
without a valid key, and wrong admin keys at login. For those to count each
tailnet client, the gateway must see the client's tailnet address. Apply one
of the two options below.

**Option A: nginx listens on the tailnet address.** nginx listens on
`<tailnet-ip>:<port>` itself (TLS with a certificate for the tailnet name,
e.g. from `tailscale cert`), and `tailscale serve` no longer serves that port.
nginx then sees each client's `100.x` address as `$remote_addr`.

**Option B: `tailscale serve` passes the address in PROXY protocol.** Run
`tailscale serve` with `--proxy-protocol 2` (the option is in
`tailscale serve --help` of Tailscale 1.104.7; check the version in
production: `<tailscale-version>`), and in nginx:

```nginx
listen 127.0.0.1:<port> proxy_protocol;
set_real_ip_from 127.0.0.1;
real_ip_header proxy_protocol;
```

In both cases:

- nginx **overwrites** the header with the address it saw:
  `proxy_set_header X-Forwarded-For $remote_addr;`. Never
  `$proxy_add_x_forwarded_for`, which keeps what the client sent.
- The gateway trusts nginx only: `FORWARDED_ALLOW_IPS=<nginx address as the
  gateway sees it>`, the gateway of the Docker network (e.g. `172.18.0.1`).
  Never `*` nor a `/0` network: any client could then choose its address (the
  boot log warns). uvicorn reads `X-Forwarded-For` only from a listed peer,
  and takes its right-most address that is not a listed proxy.
- Check: the gateway's request and audit logs show distinct `100.x` addresses
  for distinct tailnet clients, and no longer the network gateway's address.

## Backup

- **Every night, encrypted** (since 14 November 2026): `plugboard-backup.timer`
  runs [`deploy/ovh/backup.sh`](../../deploy/ovh/backup.sh) at 03:30 UTC, plus
  up to 10 minutes. The archive is encrypted with `age` for the recipient in
  `/etc/plugboard/backup.recipient`; the private key is not on the server.
  **The script stops the gateway while it copies**: about two minutes without
  service each night (2 min 16 s at the first run).
- **By hand, before a switch**, under `/var/backups/plugboard/`: the data and
  the CLI home, **not encrypted**, readable by root only. It holds the keys,
  the CLI logins and agy conversations in clear: never copy it elsewhere as it
  is. It is the restore point of that switch.
- **No restore has been rehearsed** (PLU-63): the nightly archive has not been
  decrypted, and no archive has been restored in full. Until one is, a backup
  is not a proven restore point. The restore procedure is to be completed by
  the operator.

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

## agy cage

From 4.14.2, agy runs inside a bubblewrap cage (BUG-86). The container must
allow user and mount namespaces, as for codex. Without them, every agy call
answers `503 provider_sandbox_unavailable` and agy is not run. Check the cage
after each switch, without a model request:

```bash
docker compose -p plugboard-private \
  -f /etc/plugboard/compose.v<version>.json \
  exec -T gateway python - < deploy/ovh/verify-agy-sandbox.py; echo "exit $?"
```

Expected: one JSON line with `"result": "pass"` and, in this container,
`"proc": "not mounted (the container forbids a new procfs)"` (Docker forbids
the cage a new `/proc`; it then has none, which is safe), then `exit 0`. Exit
codes and what to do: [agy cage](../../deploy/ovh/README.md#agy-cage).

## What to watch

- The size of the CLI logins volume.
- The nightly backup: a short outage around 03:30 UTC is expected; a longer
  one, or a gateway that stays down, is not.
- The catalogue scheduler stays off until the test above has passed.

See also: [production gate](prod-gate.md),
[rollback-release.md](rollback-release.md),
[deployment guide](../guide/deployment.md).
