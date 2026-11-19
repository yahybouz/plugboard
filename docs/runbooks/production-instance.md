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

Placeholders: `<port>` is the tailnet port clients use today (the one
`tailscale serve status` shows), `<nginx-port>` the port nginx listens on,
`<tailnet-ip>` and `<tailnet-name>` the host's tailnet address and name
(`tailscale ip -4`, `tailscale status --json | jq -r .Self.DNSName`).
Before either option, keep the current serve configuration to roll back:
`tailscale serve status --json > tailscale-serve.before.json`.

**Option A: nginx listens on the tailnet address.** `tailscale serve` stops
serving that port, and nginx serves it itself with a certificate for the
tailnet name:

```bash
tailscale serve --https=<port> off
tailscale cert --cert-file <cert.pem> --key-file <key.pem> <tailnet-name>
```

```nginx
server {
    listen <tailnet-ip>:<port> ssl;
    ssl_certificate     <cert.pem>;
    ssl_certificate_key <key.pem>;
    # ... the existing locations, X-Forwarded-For overwritten (below)
}
```

nginx then sees each client's `100.x` address as `$remote_addr`: no
`set_real_ip_from` is needed. nginx must start once tailscaled has its
address (it cannot bind `<tailnet-ip>` before), and the certificate from
`tailscale cert` must be fetched again before it expires.

**Option B: `tailscale serve` passes the address in PROXY protocol.** First
check the production host's Tailscale: `tailscale version`, and
`tailscale serve --help | grep -- --proxy-protocol` must print the option
(Tailscale 1.104.7 has it). Tailscale sends the PROXY header on a TCP
forwarder only (it refuses `--proxy-protocol` with `--https` or `--http`),
and refuses a TCP forwarder on a port it serves as web: remove the web
handler first, then add the forwarder. Choose the line by where TLS ends:

```bash
tailscale serve --https=<port> off
# Tailscale keeps terminating TLS, as with --https today:
tailscale serve --bg --tls-terminated-tcp=<port> --proxy-protocol=2 tcp://127.0.0.1:<nginx-port>
# or nginx terminates TLS (its certificate must be for <tailnet-name>):
tailscale serve --bg --tcp=<port> --proxy-protocol=2 tcp://127.0.0.1:<nginx-port>
tailscale serve status   # the port now forwards to tcp://127.0.0.1:<nginx-port>
```

Clients are cut from the `off` line until the forwarder is added (run
these as root or as Tailscale's operator user). In nginx, the listener gets the
PROXY header; it keeps `ssl` when nginx terminates TLS (`--tcp`), and has
none behind `--tls-terminated-tcp`:

```nginx
server {
    listen 127.0.0.1:<nginx-port> proxy_protocol;        # behind --tls-terminated-tcp
    # listen 127.0.0.1:<nginx-port> ssl proxy_protocol;  # behind --tcp
    set_real_ip_from 127.0.0.1;
    real_ip_header proxy_protocol;
    # ... the existing locations, X-Forwarded-For overwritten (below)
}
```

`real_ip_header proxy_protocol` makes `$remote_addr` the client's address
from the PROXY header, sent only by a peer listed in `set_real_ip_from`
(nginx's `realip` module: `nginx -V` lists `--with-http_realip_module`). A
listener in PROXY protocol refuses any client that sends no header, a local
probe or `curl` on `127.0.0.1:<nginx-port>` included: give such checks
another listener without `proxy_protocol`, or call the gateway directly.
To roll back, `tailscale serve --tls-terminated-tcp=<port> off` (or
`--tcp=<port> off`), then serve the port as before,
`tailscale serve --bg --https=<port> <former target>` (the target is in
`tailscale-serve.before.json`), and put the former `listen` back.

In both cases:

- nginx **overwrites** the header with the address it saw:
  `proxy_set_header X-Forwarded-For $remote_addr;`, in every `location`
  that passes to the gateway (`proxy_pass`). nginx inherits the
  `proxy_set_header` lines of the `server` block only into a `location`
  that has none of its own: a location with any `proxy_set_header` (`Host`,
  `Upgrade`...) needs this line too. Check with
  `nginx -T | grep -n 'proxy_pass\|X-Forwarded-For'`, then `nginx -t`
  before the reload. Never
  `$proxy_add_x_forwarded_for`, which keeps what the client sent.
- The gateway trusts nginx only: `FORWARDED_ALLOW_IPS=<nginx address as the
  gateway sees it>`, the gateway of the Docker network (e.g. `172.18.0.1`).
  Never `*` nor a `/0` network: any client could then choose its address (the
  boot log warns). uvicorn reads `X-Forwarded-For` only from a listed peer,
  and takes its right-most address that is not a listed proxy.
- Check: the gateway's request and audit logs show distinct `100.x` addresses
  for distinct tailnet clients, and no longer the network gateway's address.

## Switching to 4.14.3

- Image `plugboard:private-full-v4.14.3`. In the image, the
  [production gate](prod-gate.md) expects `0.65.0`, `2.1.325 (Claude Code)`
  (was `2.1.306`), `codex-cli 0.163.0` and agy `1.4.4`.
- The image has no `pip` (PLU-74): `python -m pip` fails in the container.
- `/health/ready` without a key answers `ready` and `version` only (PLU-75):
  a check that reads `checks` must send a key.
- Claude Fable 5.2 (`claude-fable-5-2`, PLU-72): the claude provider keeps
  its saved model list, which does not have it, so a call to it answers
  `404 model_not_found` (BUG-120) until the operator adds it, as below.
- The network change of the section above is not part of the image: apply
  it, then check the logs as it says.

### Add Claude Fable 5.2 to the claude provider

Neither **Discover** nor the catalogue job adds the model here: with
`PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` empty, both keep the saved list of a
claude provider, and the catalogue job must not be run (see above). Adding
`claude` to that variable would probe every model of the preset by
inference, on the subscription's quota: not here.

After the switch, add it by hand: read the provider's saved list, then send
it back with `claude-fable-5-2` at the end (`PUT /providers/{id}` writes only
the fields it is given, here `models`). Each call needs the admin key and its
own TOTP code: a code is accepted once, so wait for the next one (about 30
seconds) between the two calls; a reused or wrong code is refused with `401`
and counts towards the admin key's lockout. `<claude-provider-id>` is the
`id` of the claude provider in `GET /providers` (for example
`claude-via-claude-cli-1`); `<gateway-url>` is the address used for the
other admin calls.

```bash
read -rs ADMIN_KEY
# 1. The saved list, as is (first code).
printf 'Authorization: Bearer %s\nX-Plugboard-Admin-TOTP: %s\n' "$ADMIN_KEY" "<code-1>" \
  | curl -sf -H @- <gateway-url>/providers \
  | jq -e '.[] | select(.id == "<claude-provider-id>") | .models' > claude-models.json
jq -r '.[].id' claude-models.json   # the saved models: write them down
# 2. The same list with claude-fable-5-2 added once; stops on an empty list.
jq -e 'if (type == "array" and length > 0) then {models: (if any(.[]; .id == "claude-fable-5-2") then . else . + [{"id": "claude-fable-5-2", "name": "claude-fable-5-2"}] end)} else error("no saved model list: stop") end' \
  claude-models.json > claude-models.put.json
# 3. Send it back (the next code).
printf 'Authorization: Bearer %s\nX-Plugboard-Admin-TOTP: %s\nContent-Type: application/json\n' "$ADMIN_KEY" "<code-2>" \
  | curl -sf -X PUT -H @- --data @claude-models.put.json <gateway-url>/providers/<claude-provider-id> \
  | jq -r '.models[].id'   # the saved models, then claude-fable-5-2
unset ADMIN_KEY; rm claude-models.json claude-models.put.json
```

If step 1 prints nothing or step 2 stops, send nothing: a provider without
a saved list already takes any model name, and a wrong `id` selects no
provider. What the call does and does not do:

- Only that provider's `models` change: its default model, its other
  models and its other fields stay; no CLI starts, no model is probed, and
  nothing is quarantined. The audit log records `provider.update` with
  `{"fields": ["models"]}`; the catalog history does not show a hand edit.
- Check: `GET /v1/models` with a client key lists an ID that ends with
  `:claude-fable-5-2` (`<claude-provider-id>:claude-fable-5-2`). A call to
  it then starts
  claude with `--model claude-fable-5-2` and the same options as any other
  claude model, and is billed at the official price, 8/40 USD per million
  tokens.
- A later Discover keeps the model, as it keeps the rest of the saved list.
- A key limited by `allowed_models` still gets `403 forbidden_model` for it
  until its list names it (`claude-fable-5-2` or
  `<claude-provider-id>:claude-fable-5-2`). That list cannot be edited:
  create a new key with the model added, then revoke the old one
  ([allowed models](../guide/clients-and-keys.md#allowed-models)).
- Not checked by these steps: a real answer from Claude Fable 5.2 through
  Claude Code 2.1.325.

## Switching to 4.14.4

- Image `plugboard:private-full-v4.14.4`. The CLI versions do not change: the
  [production gate](prod-gate.md) still expects `0.65.0`,
  `2.1.325 (Claude Code)`, `codex-cli 0.163.0` and agy `1.4.4`. Before the
  start, the gate checks on the built image's ID that no `bwrap` is on the
  PATH and that `/usr/libexec/plugboard/bwrap` answers, then that the tag and
  the running container both name that ID (BUG-117).
- The image starts `tini` as PID 1 (BUG-113, BUG-123). After the switch,
  check as in [PID 1 of the container](#pid-1-of-the-container-tini): PID 1
  is `tini` (`/proc/1/cmdline` reads `/usr/bin/tini -- python run.py`), or
  `docker-init` with tini as its direct child when the Compose file sets
  `init: true` (`/sbin/docker-init -- /usr/bin/tini -- python run.py`), and
  the count of zombies is `0` in both cases (read it again after some agy
  and codex calls).
- **A Compose file that sets `entrypoint:` for the gateway replaces tini**:
  the gateway is PID 1 again and keeps every orphan as a zombie. None in the
  repository does; check the production file before the switch
  (`grep -n entrypoint /etc/plugboard/compose.v4.14.4.json` must print
  nothing). `init: true` is harmless.
- agy's cage, without a model request (as in [agy cage](#agy-cage)):
  `verify-agy-sandbox.py` must print `"result": "pass"` with
  `"probe_is_pid1": true`, now checked without `/proc` too (BUG-114), then
  `exit 0`. It also checks that agy's settings are read-only in the cage
  (BUG-108) and reports what the cage can reach under `"network"`, for
  information (BUG-109). `~/.gemini/projects.json` is no longer required
  (BUG-115).
- codex's sandbox, without a prompt: `verify-codex-sandbox.py` (PLU-71), as
  in step 6 of the [production gate](prod-gate.md), must print
  `"result": "pass"`, then `exit 0`; on exit `1`, go back.
- The image has no pip wheel left for `ensurepip` (PLU-77): `ensurepip` has
  nothing to install in the container; nothing at run time calls it.
- Not checked by these steps: a first real agy call with its settings
  read-only, to be made on the UAT before production at each agy release.

## Switching to 4.14.5

- Image `plugboard:private-full-v4.14.5`. The CLI versions do not change: the
  [production gate](prod-gate.md) still expects `0.65.0`,
  `2.1.325 (Claude Code)`, `codex-cli 0.163.0` and agy `1.4.4`, and still
  checks the image ID and that no `bwrap` is on the PATH (BUG-117). agy's
  cage and codex's sandbox do not change: `verify-agy-sandbox.py` and
  `verify-codex-sandbox.py` must still print `"result": "pass"`.
- PID 1 is checked by a script from now on (BUG-132): from a checkout of the
  4.14.5 commit, `docker exec -i "$c" python - < deploy/check-pid1.py`, as in
  [PID 1 of the container](#pid-1-of-the-container-tini). It reads the
  binary of PID 1, not its name. Expected: `PID 1: /usr/bin/tini`, or
  `PID 1: /sbin/docker-init` then `tini under it: pid <n>`, and `exit 0`;
  the count of zombies stays `0`. A line `STOP: ...` and `exit 1`: go back,
  except a binary it cannot read (the container's user may not read
  `/proc/1/exe`): run it again with `docker exec -u 0 -i` before deciding.
  This is the script's first run in the production container.
- The shared Claude quota is watched from real calls (PLU-67), without any
  probe: after the switch, read `GET /admin/claude-quota` once (a fresh TOTP
  code, as for `/metrics` in step 9 of the [production gate](prod-gate.md)).
  Before any Claude limit it answers `"last_limit_at": null` and
  `"providers": {}`; `GET /metrics/prometheus` carries the `# TYPE` lines of
  `plugboard_claude_limit_total` and
  `plugboard_claude_limit_last_timestamp_seconds`, with no series yet. The
  state is written to `data/claude_quota.json` at the first limit, inside
  the data volume and its backup. The **Monitoring** page shows it under
  **Claude quota** ([shared Claude quota](../guide/monitoring.md#shared-claude-quota)).
- The internet check no longer quarantines a model for a `401`, a `403`, a
  key error or a CLI that exited in error (BUG-127): it answers
  `"status": "error"` and records nothing. A quarantine recorded before the
  switch stays in `data/quarantined_models.json` until a successful call or
  probe on that pair lifts it.
- The SQLite stores and the audit database need nothing (BUG-128, BUG-131):
  no migration, the existing files are kept as they are.
- Not checked by these steps: a real Claude limit met through the gateway
  (the forms of the message come from the code and its tests), and the
  **Claude quota** block in a browser.

## Switching to 4.14.6

- Image `plugboard:private-full-v4.14.6`. The CLI versions do not change: the
  [production gate](prod-gate.md) still expects `0.65.0`,
  `2.1.325 (Claude Code)`, `codex-cli 0.163.0` and agy `1.4.4`, and still
  checks the image ID and that no `bwrap` is on the PATH (BUG-117). agy's
  cage and codex's sandbox do not change: `verify-agy-sandbox.py` and
  `verify-codex-sandbox.py` must still print `"result": "pass"`.
- gh keeps its version, 2.104.0, but is now built from the sources of its
  release tag with `golang.org/x/net` v0.62.0 (SEC-VEILLE, CVE-2026-78661),
  installed at `/usr/local/bin/gh` instead of the apt package. Building the
  image needs to reach `github.com` and `proxy.golang.org`; a build that
  cannot reach them fails in the `gh` stage, before any switch. In the CLI
  check of the [production gate](prod-gate.md), `gh --version` must give
  `gh version 2.104.0 (2026-11-12)` as its first line; another version, or
  an error, stops the gate.
- After the switch, `GET /health` answers `{"ok": true, "version": "4.14.6"}`.
- The Claude quota state needs nothing (BUG-143): `data/claude_quota.json`
  is kept as it is, and an excerpt saved by 4.14.5 is masked again when
  `GET /admin/claude-quota` reads it. The audit entries already written are
  not rewritten.
- Not checked by these steps: `gh auth token` in a real container of the new
  image (the GitHub Copilot and GitHub Models providers read their token
  through it).

## Switching to 4.15.0

- Image `plugboard:private-full-v4.15.0`. The image recipe does not change
  since 4.14.6: the [production gate](prod-gate.md) expects the same CLI
  versions (`0.65.0`, `2.1.325 (Claude Code)`, `codex-cli 0.163.0`, agy
  `1.4.4`, `gh version 2.104.0 (2026-11-12)`) and still checks the image ID
  and that no `bwrap` is on the PATH. From an older version, read the
  sections of the versions in between first (4.14.4, 4.14.5, 4.14.6).
- Nothing to set for production to work as before. The four new variables
  keep their safe default: leave `PLUGBOARD_CLI_TOOLS_EMULATION` empty (tools
  emulated on CLI routes are for UAT and internal agents, ADR-039);
  `PLUGBOARD_MAX_INFLIGHT_WEB` (`2`), `PLUGBOARD_WEB_TIMEOUT_S` (`900`) and
  `PLUGBOARD_WEB_HELPER_MODEL` (`claude-sonnet-5-5`, never Haiku) only act on
  a key that holds `web_access`, and no key holds it after the switch.
- Turning the web on for one key is a separate gesture, after the switch:
  `PUT /admin/keys/{key_id}/web` with `{"web_access": true}` (and
  `web_max_uses` from 1 to 10, default 3), then `GET /admin/web` to watch it
  ([web access](../guide/web-access.md)). `PUT ... {"web_access": false}`
  turns it off again, without changing the key.
- The usage database gets four columns on start (`request_id`, `trace_id`,
  `web`, `web_uses`), the key store two fields (`allow_cli_tools`,
  `web_access`/`web_max_uses` when set) and the personal dashboard keys
  their own store (PB-AUTH-01): no migration to run.
- After the switch, `GET /health` answers `{"ok": true, "version": "4.15.0"}`
  and a chat request without the web answers as before (no `plugboard` field,
  no `X-Plugboard-Web` header).
- Rolling back to 4.14.6 works: it ignores the new columns and fields. Keys
  with `web_access` then lose the web (4.14.6 knows neither
  `X-Plugboard-Web` nor the `plugboard` field; its answer to a request that
  still sends them was not checked), and personal dashboard keys stop
  working (use **Log out everywhere** first).
- Not checked by these steps: a real web run in production (search, fetch,
  egress guard on the production network); it is checked on the first key
  given `web_access`.

## Switching to 4.15.1

- Image `plugboard:private-full-v4.15.1`. The image recipe does not change
  since 4.15.0: the [production gate](prod-gate.md) expects the same CLI
  versions (`0.65.0`, `2.1.325 (Claude Code)`, `codex-cli 0.163.0`, agy
  `1.4.4`, `gh version 2.104.0 (2026-11-12)`) and still checks the image ID
  and that no `bwrap` is on the PATH. From an older version, read the
  sections of the versions in between first.
- Nothing to set for production to work as before: no variable is added,
  removed or changed, no default moves, and no store or column changes shape.
- agy's cage now has no network of its own (BUG-156). `verify-agy-sandbox.py`
  must print `"result": "pass"` with `"shared_with_container": false` and
  `"gateway_port_from_cage": "refused"` under `"network"`, then `exit 0`; a
  cage that shares the container's network, or one from which the gateway's
  port answers, now fails it. The gateway's own Python must be in the image
  under `/usr/local` (it runs the cage's relay): the standard `full` image
  has it. Without the egress socket or the relay, agy is not run
  (`503 provider_sandbox_unavailable`).
- After the first real agy calls, read the gateway log for
  `agy egress refused: host=...`: a host agy needs that is not on the list
  shows there once per host and minute. It is added in the code, never by a
  setting, so such a line means a new release, not a change on the server.
- A web request is now billed on the tokens claude reports for it (BUG-159),
  so it costs what it used, not an estimate: expect the usage of keys with
  `web_access` to read higher than under 4.15.0, and their monthly budget to
  be reached sooner.
- After the switch, `GET /health` answers `{"ok": true, "version": "4.15.1"}`
  and a chat request without the web answers as before.
- Rolling back to 4.15.0 works: nothing written changes shape. agy then runs
  in the container's network again.
- Not checked by these steps: a real agy call through its egress, and a real
  web run billed on the tokens claude reports; both are checked on the first
  real calls after the switch.

## PID 1 of the container: tini

From 4.14.4 the image starts `tini` as PID 1 (BUG-113, BUG-123), whatever the
Compose file: it reaps every orphan (codex's `bwrap`, `git`, a shell left by a
CLI) and passes `SIGTERM` on to the gateway, which stops its CLIs itself.
Before, the gateway was PID 1 and kept each orphan as a zombie (68 in one hour
on the UAT with 4.14.2) until the container could fork no more. A Compose file
with `init: true` only puts Docker's own init in front of tini: harmless, but
PID 1 is then `docker-init` (Docker's build of tini, `/sbin/docker-init`) and
the image's tini its direct child; the orphans go to `docker-init`, which
reaps them, and tini logs once at start `Tini is not running as PID 1 and
isn't registered as a child subreaper`: expected in that case. Both are a
pass; anything else is a go-back. An `--entrypoint` given to `docker run` replaces
tini for that one-off container only.

Read `/proc` (the slim image may have no `ps`). After each switch, from a
checkout of the deployed commit:

```bash
c="$(docker compose -p plugboard-private -f /etc/plugboard/compose.v<version>.json ps -q gateway)"
docker exec "$c" sh -c 'tr "\0" " " < /proc/1/cmdline; echo'
# Expected: /usr/bin/tini -- python run.py
# (with init: true: /sbin/docker-init -- /usr/bin/tini -- python run.py)
docker exec -i "$c" python - < deploy/check-pid1.py; echo "exit $?"
# Expected: "PID 1: /usr/bin/tini", or "PID 1: /sbin/docker-init" then
# "tini under it: pid <n>"; exit 0. A line "STOP: ..." and exit 1: go back.
docker exec "$c" sh -c "grep -s '^State:[[:space:]]*Z' /proc/[0-9]*/status | wc -l"
# Expected: 0 zombies in both cases (read it again after some agy and codex calls)
```

[`deploy/check-pid1.py`](../../deploy/check-pid1.py) (BUG-132) checks the
**binary** of PID 1 (`readlink /proc/1/exe`), not its name: any program can
call itself `tini`. With `docker-init` as PID 1, it looks for a direct child
whose binary is `/usr/bin/tini`, reading each parent in `/proc/<pid>/stat`
after the last `)` (the name between the parentheses may itself hold `) S 1`).
The gateway (python, uvicorn) as PID 1 means an `entrypoint:` bypassed tini:
`STOP`. A binary it cannot read is a `STOP` too, never a pass; run it again
with `docker exec -u 0 -i`. Where the kernel has `CONFIG_PROC_CHILDREN`
(the VPS's 6.8 kernel does), `cat /proc/1/task/1/children` lists PID 1's
children directly; the check does not rely on it.

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
