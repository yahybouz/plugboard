# Runbook: move an install to another machine

Move a running Plugboard 4.x install, with its providers, client keys, usage
and pricing history, to a new machine. Works for a source install (`run.py`,
systemd) and for the default Docker Compose deployment (`docker-compose.yml`,
service `api`, volume on `/app/data`). Both machines run the **same**
Plugboard version: move first, upgrade afterwards
([deployment](../guide/deployment.md#backups-and-upgrades)). The private
deployment under `deploy/ovh/` has its own cold backup, `deploy/ovh/backup.sh`,
which also saves the CLI logins.

What moves, and where it lives:

| What | Where | Moves with |
|---|---|---|
| Providers, encrypted provider tokens, client keys (hashed), usage, pricing grids, response cache, sessions, MFA secret, OAuth sign-ins from the dashboard | the data directory: `data/` in the install directory, or `PLUGBOARD_DATA`; `/app/data` in Docker | the data archive |
| `ENCRYPTION_KEY` | `.env` (or the platform's secret store); when it is empty, `data/.encryption_key` | `.env`, or the data archive |
| `ADMIN_KEY`, `API_KEY`, every other setting | `.env` | `.env` |
| CLI subscription logins (Claude, Codex, Gemini...) | the home directory of the account that runs Plugboard (`/home/app` in Docker) | nothing: sign in again |
| Local Ollama models | the Ollama install | nothing: pull them again |

Without the encryption key the stored provider tokens, OAuth sign-ins and
MFA secret cannot be read. The archive and `.env` hold secrets: keep them
owner-only and delete the copies once the move is done.

## Symptom

- The machine must be replaced (end of lease, new hardware, new host or
  region), or Plugboard moves to a machine of its own.
- The old machine is failing but still starts: move before it stops for good
  (for a machine that no longer starts, restore your last backup the same way,
  from step 4).

## Priority

P3 for a planned move: pick a maintenance window, clients get errors while
the service is stopped (steps 3 to 8). P1 when the old machine is failing.

## Quick diagnosis

On the old machine, from the installation directory, record what the new
one must show. When admin MFA is enforced, see the
[runbook conventions](README.md) for admin calls.

```bash
cd /path/to/plugboard
export ADMIN_KEY=$(grep '^ADMIN_KEY=' .env | cut -d= -f2-)

# Version
curl -s http://localhost:8765/health | python3 -m json.tool

# Auth posture: checks.auth is "ok" (API_KEY set), "dev" (empty, PLUGBOARD_DEV=1)
# or "fail" (empty outside dev mode, the route then answers 503)
curl -s http://localhost:8765/health/ready | python3 -m json.tool

# Active providers (admin route)
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/providers \
  | python3 -c 'import json,sys; print(sorted(p["id"] for p in json.load(sys.stdin) if p.get("is_active") is not False))'

# Client keys, usage entries of the month (probes the providers, runs no model)
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/health/detailed \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["version"], d["keys_count"], d["usage_month"], d["usage_entries"])'

# Where the data and the key live (empty values: data/ and data/.encryption_key)
grep -E '^(PLUGBOARD_DATA|ENCRYPTION_KEY|OLLAMA_URL|PLUGBOARD_REGISTRY_PATH|PLUGBOARD_DECISION_ASSETS)=' .env | cut -d= -f1,2 | sed 's/^\(ENCRYPTION_KEY=\).\+/\1(set)/'
```

Keep the output: the same version, auth posture (`checks.auth`), active
providers, key count and usage entries are expected on the new machine. A `PLUGBOARD_REGISTRY_PATH` or
`PLUGBOARD_DECISION_ASSETS` outside the data directory must be copied too
(same path on the new machine, or update the variable).

## Common causes

Of a move that fails:

- **Another encryption key on the new machine**: `.env` was not copied, or
  `ENCRYPTION_KEY` was empty and `data/.encryption_key` stayed behind (the
  server then generates a new one). Providers answer `401`/`403` upstream and
  the log says `Failed to decrypt stored token`. Step 6 catches it before the
  start.
- **Another version on the new machine**: install the version recorded above,
  then upgrade with the usual procedure.
- **Data directory not readable by the service account**: files restored
  under another owner (a Docker volume needs owner `app`, UID 1000). Step 5
  sets it.
- **Copied while running**: the SQLite stores were copied mid-write. Stop
  the old instance before the archive (step 3).
- **Ollama models missing**: `data/` holds the `ollama` provider, not the
  models. Pull them on the new machine, or point the provider to an Ollama
  that has them (step 7).
- **CLI providers signed out**: their logins are in the old account's home
  directory. Sign in again on the new machine (step 7).
- **Two instances on the same keys**: the old machine started again after
  the move; usage and billing then diverge. Keep it stopped (step 10).

## Mitigation and rollback

### 1. Prepare the new machine

Install the same version as the old one, without starting it and without
`make init` (the old `.env` is reused).

Source install (see [deployment](../guide/deployment.md#without-docker-systemd)):

```bash
git clone <repository-url> /opt/plugboard
cd /opt/plugboard
git checkout v<version>
make setup
```

Docker: clone and check out the same version, then build the image:

```bash
git clone <repository-url> /opt/plugboard
cd /opt/plugboard
git checkout v<version>
docker compose build
```

### 2. Save this month's usage (optional)

A CSV of the month, kept outside Plugboard, in case the move has to be
undone after clients used the new machine:

```bash
curl -s -H "Authorization: Bearer $ADMIN_KEY" \
  "http://localhost:8765/admin/usage/$(date -u +%Y_%m)/export?format=csv" \
  > usage-$(date -u +%Y_%m)-before-move.csv
```

### 3. Stop the old instance and archive the data

Stop it so the SQLite stores are consistent and nothing is written after
the copy.

Source install (the data directory is `data/`; with `PLUGBOARD_DATA` set,
archive that directory instead):

```bash
sudo systemctl stop plugboard        # or: kill $(lsof -ti:8765)
umask 077
tar czf plugboard-data.tar.gz data
```

Docker (reads the volume through a throwaway container of the same image,
with no network):

```bash
docker compose stop
umask 077
docker run --rm --user 0:0 --network none \
  --volumes-from "$(docker compose ps -aq api):ro" \
  --entrypoint tar plugboard:api -C /app -czf - data > plugboard-data.tar.gz
```

`plugboard:api` is the image named in `docker-compose.yml`; use yours if you
changed it (the `full` target, a pinned tag).

### 4. Copy the archive and `.env`

```bash
scp plugboard-data.tar.gz .env <user>@<new-host>:/opt/plugboard/
```

With `ENCRYPTION_KEY` kept in a secret manager rather than `.env`, give the
new machine the same value there.

### 5. Restore on the new machine

Source install, from `/opt/plugboard`:

```bash
tar xzpf plugboard-data.tar.gz
chmod 700 data
chmod 600 .env
```

Docker, from `/opt/plugboard`: create the container and its volume without
starting it, write the data into the volume, and give it to `app`:

```bash
docker compose up --no-start
docker run --rm -i --user 0:0 --network none \
  --volumes-from "$(docker compose ps -aq api)" \
  --entrypoint tar plugboard:api -C /app -xzpf - < plugboard-data.tar.gz
docker run --rm --user 0:0 --network none \
  --volumes-from "$(docker compose ps -aq api)" \
  --entrypoint chown plugboard:api -R app:app /app/data
docker run --rm --user 0:0 --network none \
  --volumes-from "$(docker compose ps -aq api)" \
  --entrypoint chmod plugboard:api 700 /app/data
chmod 600 .env
```

### 6. Check the encryption key before the start

A dry run of the key rotation reads every stored secret (provider tokens,
OAuth sign-ins, MFA secret) with the key the server will use. It writes
nothing but the throwaway key file it is given.

Source install:

```bash
make rotate-encryption-key ARGS="--new-key-file /tmp/plugboard-check.key --generate --dry-run"
rm -f /tmp/plugboard-check.key
```

Docker (the key file stays in the throwaway container):

```bash
docker compose run --rm api python scripts/rotate_encryption_key.py \
  --new-key-file /tmp/plugboard-check.key --generate --dry-run
```

Expected: `Dry run: nothing was written.` If it prints `Cannot be read with
the current key nor the new one`, the key is not the old one: fix
`ENCRYPTION_KEY` in `.env`, or, when it is empty, put the old
`data/.encryption_key` back (the check generated a new one if it was
missing), and run the check again. Do not start the server before it passes.

### 7. Adapt what belongs to the machine

- **`.env`**: `HOST`, `PORT`, `CORS_ORIGINS`, `PLUGBOARD_DATA` (if the path
  changed), `PLUGBOARD_MCP_ALLOWED_HOSTS` and `PLUGBOARD_MCP_ALLOWED_ORIGINS`
  when the host name changes. Keep `ADMIN_KEY`, `API_KEY` and
  `ENCRYPTION_KEY` as they are.
- **Reverse proxy and TLS**: set them up on the new machine
  ([deployment](../guide/deployment.md#behind-a-reverse-proxy)).
- **Ollama**: install it and pull the local models you use
  (`ollama pull <model>`). If it answers at another address, set `OLLAMA_URL`,
  start Plugboard (step 8), then update the provider's address, which
  `data/` kept:

  ```bash
  curl -s -X PUT -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
    -d '{"base_url": "<ollama-url>"}' http://localhost:8765/providers/ollama | python3 -m json.tool
  ```

- **CLI subscriptions**: sign in again as the account that runs Plugboard
  (in Docker, inside the container: see
  [deployment](../guide/deployment.md#docker-compose)). Do not copy their
  token files from the old machine.

### 8. Start and verify

```bash
# Source install
sudo systemctl enable --now plugboard    # or: python run.py

# Docker
docker compose up -d
```

Then, with `ADMIN_KEY` exported as in the quick diagnosis:

```bash
# Same version as recorded; "ok": true
curl -s http://localhost:8765/health | python3 -m json.tool

# Same checks.auth as recorded; "ready": true (HTTP 200)
curl -s http://localhost:8765/health/ready | python3 -m json.tool

# Same active providers as recorded
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/providers \
  | python3 -c 'import json,sys; print(sorted(p["id"] for p in json.load(sys.stdin) if p.get("is_active") is not False))'

# Same version, key count and usage entries; each provider "ok"
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/health/detailed \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["version"], d["keys_count"], d["usage_month"], d["usage_entries"])'
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/health/detailed | python3 -m json.tool

# An existing client key still works (no model is run)
export CLIENT_KEY=pb_<your-client-key>
curl -s -H "Authorization: Bearer $CLIENT_KEY" http://localhost:8765/v1/me | python3 -m json.tool
curl -s -H "Authorization: Bearer $CLIENT_KEY" http://localhost:8765/v1/models | python3 -m json.tool

# Background jobs
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/pricing/scheduler/status | python3 -m json.tool
curl -s -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8765/admin/models/scheduler/status | python3 -m json.tool
```

Sign in to the dashboard with `ADMIN_KEY`; with MFA enrolled, the same
authenticator code works. A real completion (it is billed) is in the
[rollback runbook](rollback-release.md#6-verify).

### 9. Switch the traffic

Point the DNS name or the reverse proxy to the new machine. If the address
clients use changes, give them the new base URL, and the new `PLUGBOARD_URL`
to MCP servers.

### 10. Retire the old machine

Keep it stopped, not deleted, until the new one has served for a while:

```bash
sudo systemctl disable plugboard    # source install: no start at boot
```

With Docker, `docker compose stop` (step 3) is enough: a container stopped
by hand does not restart with the machine. Then delete
`plugboard-data.tar.gz` and the copied `.env` from wherever they were
copied, and wipe the old machine's data directory with the machine.

### Roll back

If the new machine does not pass step 8, stop it (`docker compose stop`,
or `sudo systemctl stop plugboard`), start the old one again
(`docker compose start`, or `sudo systemctl start plugboard`), and point the
traffic back. Usage recorded on the new machine meanwhile is not on the old
one: reconcile it from `GET /admin/usage/<month>/export?format=csv` of the
new machine.

## Escalation

- Step 6 still fails with the old key restored: the archive or the key is
  not the right one. Keep the old machine running and escalate to whoever
  holds the key backups ([deployment](../guide/deployment.md#backups-and-upgrades)).
- A provider fails only on the new machine (network, firewall, allowlist,
  DNS): see [provider-down.md](provider-down.md).

See also: [deployment guide](../guide/deployment.md),
[rollback runbook](rollback-release.md).
