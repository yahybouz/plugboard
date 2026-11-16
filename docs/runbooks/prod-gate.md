# Runbook: production gate

The ordered commands that move the production instance to a new release,
with a way back at every step: data check, backup and its check, image
checks, CLI versions, smoke, a rehearsed rollback, then 24 hours of
observation. The operator runs every command; nothing here is automated.

The instance in service is described in
[production-instance.md](production-instance.md): Compose project
`plugboard-private`, one Compose file per version under `/etc/plugboard`
(`compose.v<version>.json`), image `plugboard:private-full-v<version>`, no
published port, an existing Docker network. Its deployment files are not in
the repository. Where a command or a fact could not be checked against the
host, this runbook says so: **to be confirmed by the operator**. The former
private host (`deploy/ovh/`) has its own section at the end:
[former private host](#former-private-host-deployovh).

The gate passes when the new version serves for 24 hours with **less than
1 % of `5xx` answers** on the client routes, over at least 100 client
requests, after a memory measure under load below the stop threshold, a
green smoke and a rollback rehearsed both ways.

This is a planned procedure, not an incident runbook: its sections are the
steps of the gate.

`ci` is the local CI tool of the UAT host (`python3 /srv/dev/.ci/outils/ci.py`,
subcommands `etat`, `lancer`, `deployer`); `ci deployer` refuses a commit
without a green local CI run. There is no remote CI pipeline.

## When to use

Before every production upgrade. The UAT host must already run the same
commit (`ci deployer`), with a green smoke and a rehearsed rollback
([rollback-release.md](rollback-release.md#rehearse-the-rollback)). Write
down that commit: `ci deployer plugboard <sha> --etat` prints it as
`sha_en_service`.

Avoid 03:30 to 03:40 UTC: the nightly backup timer
(`plugboard-backup.timer`, 03:30 UTC plus up to 10 minutes) stops the
gateway for about two minutes then.

## Before you start

- A shell on the production host, kept open for the whole gate (the steps
  share shell functions and variables). The commands below run Docker with
  `sudo`; [production-instance.md](production-instance.md) writes them
  without it. Which account runs Docker there, and whether `/etc/plugboard`
  is readable by root only, is to be confirmed by the operator: drop `sudo`
  where it is not needed.
- `<checkout>`: a git checkout of the repository on the host, for the
  scripts the gate runs (data check, smoke, sandbox checks, backup). It does
  not drive the gateway: the Compose files are under `/etc/plugboard`. The
  nightly backup runs `deploy/ovh/backup.sh`, which works from
  `/opt/plugboard` (the script changes to that directory itself): which
  checkout to use is to be confirmed by the operator.
- The admin key and a fresh TOTP code for each admin call (admin MFA is
  enforced; a code is accepted once).
- A **smoke key**: a client key created in the dashboard for the gate only,
  with a small monthly limit, whose `allowed_models` holds one chat model
  of a CLI subscription provider (`claude-code:`, `Codex:`,
  `antigravity-via-agy-cli:`) that `GET /v1/models` lists for it. Ollama is
  off in production (`OLLAMA_ENABLED=false`): the key holds no `ollama:*`
  model, and the smoke's embeddings step is a `SKIP`. Each smoke run sends
  two short chat calls on the subscription's allowance (the structured one
  is refused before any call); the gate runs three smokes.
- The `age` identity of the encrypted backups, on the machine that keeps
  it (step 3).

Placeholders: `<running-version>` is the version in service before the gate,
`<running-image-name>` and `<running-image-id>` the name and the ID of its
image (step 1), `<previous-ref>` the checkout before the gate (step 1),
`<new-version>` the release being deployed (its `VERSION` file), `<model>`
the chat model of the smoke key, `<uat-sha>` the commit in service on the UAT
host and `<uat-image-id>` the ID of its image (step 0), `<gateway-url>` the
address used for admin calls (see
[production-instance.md](production-instance.md#add-claude-fable-52-to-the-claude-provider)).

**No port is published**: the gateway is reached through the Docker
network, not on the host's loopback. The gate's health checks and smokes
therefore run inside the gateway container (`docker exec`), against its own
port; the smoke key goes through standard input, never on a command line.
Running the smoke from a throwaway container on the instance's Docker
network also works; that network's name is to be confirmed by the operator.

Define the shell helpers once:

```bash
cd <checkout>
# The Compose file of the version in service, and the one of the release
gw_prev() { sudo docker compose -p plugboard-private -f /etc/plugboard/compose.v<running-version>.json "$@"; }
gw() { sudo docker compose -p plugboard-private -f /etc/plugboard/compose.v<new-version>.json "$@"; }
# Both name the same project: `ps`, `exec` and `logs` find the gateway in service.
wait_healthy() {
  for _ in $(seq 60); do
    state="$(sudo docker inspect --format '{{.Config.Image}} {{.State.Health.Status}}' "$(gw_prev ps -q gateway)")"
    case "$state" in *" healthy") echo "$state"; return 0 ;; esac
    sleep 5
  done
  echo "not healthy after 5 minutes: $state"; return 1
}
# /health, from inside the gateway container
health() {
  sudo docker exec "$(gw_prev ps -q gateway)" python -c \
    "import os, urllib.request; print(urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8765'), timeout=5).read().decode())"
}
# The smoke of the release, run by the container's Python: the key on the
# first line of standard input, the script after it. Embeddings are skipped:
# no embedding model is declared while Ollama is off.
smoke() {
  { printf '%s\n' "$PLUGBOARD_SMOKE_KEY"; cat ~/plugboard-tools/smoke_consumers.py; } \
    | sudo docker exec -i "$(gw_prev ps -q gateway)" sh -c \
      'IFS= read -r PLUGBOARD_SMOKE_KEY; export PLUGBOARD_SMOKE_KEY
       exec python - --url "http://127.0.0.1:${PORT:-8765}" "$@"' smoke \
      --model <model> --expect-version "$1"
}
# Memory of the gateway container (gateway and CLI processes), from its cgroup
mem() {
  gw_prev exec -T gateway sh -c 'echo "peak $(( $(cat /sys/fs/cgroup/memory.peak) / 1048576 )) MiB, \
now $(( $(cat /sys/fs/cgroup/memory.current) / 1048576 )) MiB, \
$(grep oom_kill /sys/fs/cgroup/memory.events)"'
}
```

The smoke's own Python runs in the gateway container and counts in its
memory while it runs.

## Memory stop threshold (D-43)

The production container is limited to 3 GiB (`3221225472` bytes, read at
the switch to 4.14.0; step 1 reads it again), CLI processes included, as
`mem_limit: 3g` in the repository's model. The gate **stops** when the
memory peak of the container reaches **2457 MiB (80 % of the limit)**, when
`oom_kill` is above 0, or when the container restarts during a measure: the
remaining 20 % absorb the background jobs (prices, model lists) and a spike
the bench did not produce. From 2150 MiB (70 %) up, go on but report the
figure with the gate results.

## Steps

### 0. Measure the memory under load, on the UAT host (D-43)

Before touching production, measure the memory of the same image under load
on the UAT host, which already runs the release (see
[When to use](#when-to-use)), with the CLI providers limited to one request
at a time (the release's default). The load comes from the bench of PLU-17,
`scripts/bench_concurrency.py`, shipped in the image; it runs in a throwaway
container of that image, so its own memory is not counted.

- `<bench-model>`: to measure what D-43 is about, a model of a CLI provider.
  Each chat uses that subscription's quota: one pass below sends
  (1 + 2 + 4 + 8) x 2 = 30 short chats, most of them refused with `503`
  (busy) at 4 and 8 clients since the CLI takes one request at a time.
- `PLUGBOARD_BENCH_KEY`: a client key of the UAT gateway allowed to use
  `<bench-model>`.
- Run as root on the UAT host (system Docker); the gateway container is
  `uat-plugboard-gateway-1`, published on `127.0.0.1:18765`.

```bash
c=uat-plugboard-gateway-1
img="$(docker inspect --format '{{.Config.Image}}' "$c")"
docker inspect --format '{{.Image}}' "$c"   # write it down: <uat-image-id>
docker inspect --format '{{.RestartCount}} {{.State.StartedAt}}' "$c"   # write both down
# The release's CLI limit (1) applies, and the bench ships in the image
docker exec "$c" sh -c 'echo "CLI limit: ${PLUGBOARD_CLI_PROVIDER_MAX_INFLIGHT:-1 (default)}"'
docker run --rm --entrypoint ls "$img" scripts/bench_concurrency.py
read -rs PLUGBOARD_BENCH_KEY && export PLUGBOARD_BENCH_KEY
for n in 1 2 4 8; do
  docker run --rm --network host -e PLUGBOARD_BENCH_KEY --entrypoint python "$img" \
    scripts/bench_concurrency.py --url http://127.0.0.1:18765 \
    --model <bench-model> --clients "$n" --rounds 2 --max-tokens 64
  docker exec "$c" sh -c 'echo "clients='"$n"': peak $(( $(cat /sys/fs/cgroup/memory.peak) / 1048576 )) MiB, \
now $(( $(cat /sys/fs/cgroup/memory.current) / 1048576 )) MiB, \
$(grep oom_kill /sys/fs/cgroup/memory.events)"'
done
docker inspect --format '{{.RestartCount}} {{.State.StartedAt}}' "$c"   # unchanged
unset PLUGBOARD_BENCH_KEY
```

`CLI limit` must be `1`, or `1 (default)`: with a higher limit the measure
does not describe the release as production runs it. `memory.peak` is the
highest memory of the container since it started, so
the line after each level holds the peak up to that level. Compare the last
line with the [stop threshold](#memory-stop-threshold-d-43): at 2457 MiB or
more, with `oom_kill` above 0, or with a changed restart count or start time,
**stop the gate** and report the four lines; the release must not reach
production. `docker stats --no-stream "$c"` gives the same figure as a
snapshot. A UAT gateway without a memory limit still gives the peak in MiB:
the threshold is the same.


### 1. Record what runs now and pause the nightly backup

```bash
cd <checkout>
health                                    # "version": <running-version>
sudo docker inspect --format '{{.Config.Image}} {{.Image}} {{.State.Health.Status}} {{.HostConfig.Memory}}' \
  "$(gw_prev ps -q gateway)"
# write down the first two words: <running-image-name> <running-image-id>
# the last one is the memory limit in bytes: 3221225472 (3 GiB)
gw_prev config --images                   # <running-image-name>: the file of the running version
git rev-parse HEAD && git rev-parse --abbrev-ref HEAD   # write both down: <previous-ref>
git status --short                        # must print nothing
sudo systemctl stop plugboard-backup.timer
```

`gw_prev config --images` must print `<running-image-name>`: the way back
(steps 8 and [if a step fails](#if-a-step-fails)) starts the image that file
names. Another name means the gateway was not started from that file: stop
and find which file it was. Local changes in `<checkout>` would be left
behind by the checkout of step 4: reconcile them with the release first.

### 2. Fetch the release, check it, copy its tools

```bash
cd <checkout>
git fetch --tags origin
git rev-parse "v<new-version>^{commit}"   # must equal <uat-sha>
git log --oneline "v<new-version>..HEAD"  # must print nothing
install -d -m 0700 ~/plugboard-tools
git show v<new-version>:scripts/precheck_data.py > ~/plugboard-tools/precheck_data.py
git show v<new-version>:scripts/smoke_consumers.py > ~/plugboard-tools/smoke_consumers.py
git show v<new-version>:CHANGELOG.md | less
```

`git log` lists the commits of this checkout that the release does not
contain: they must be in the release before the gate. In the `CHANGELOG`,
read every entry after `<running-version>`: a new or required setting goes
into the configuration under `/etc/plugboard` (the Compose file of step 4)
before step 6. The smoke and the data check come from the new release;
older releases do not ship them.

### 3. Check the data, then back it up

The data check reads the live data volume **read-only**, with the Python of
the running image (by ID) as the user `app`; it prints file names,
`key_id`s and rules, never a stored value.

```bash
cd <checkout>
cid="$(gw_prev ps -q gateway)"
sudo docker run --rm --network none --user 1000:1000 --volumes-from "$cid:ro" \
  -v "$HOME/plugboard-tools/precheck_data.py:/precheck_data.py:ro" \
  --entrypoint python "$(sudo docker inspect --format '{{.Image}}' "$cid")" \
  -I /precheck_data.py --data /app/data
echo "exit $?"   # 0: READY
```

Exit `1` (`NOT READY`): fix every `ERROR` line in the dashboard before going
on. A key is fixed by revoking it and creating a new one with a correct
`client_id` and `allowed_models` (written `<provider id>:<model>`), then
handing the new key to its client. Run the check again until it prints
`READY`. Read the `WARN` lines too (`--strict` makes them fail).

Then take a cold backup (the gateway stops while the archive is written).
This backup is mandatory: from 4.12.0 the first daily retention pass (04:00
UTC) deletes old usage, audit, watchdog, monitoring and session rows for good
(see [monitoring](../guide/monitoring.md)), and this archive is then the only
copy of them. How it has been taken before each switch of this instance:

- 4.14.1 and 4.14.2: the encrypted backup script, run by hand as root:
  `plugboard-<stamp>.tar.gz.age` under `/var/backups/plugboard/`, the same
  script as the nightly timer.
- 4.14.0: a plain archive of the data and the CLI home, by hand, under
  `/var/backups/plugboard/` (`pre-4.14.0-<stamp>.tar.gz`): **not encrypted**,
  readable by root only, with the keys, the CLI logins and agy conversations
  in clear. Its command is not in the repository: to be confirmed by the
  operator.

The script stops the gateway, archives `app/data`, `home/app` and
`/etc/plugboard/runtime.env`, encrypts the archive with `age` for the
recipient in `/etc/plugboard/backup.recipient`, then starts the gateway
again (about two minutes). It finds the gateway through the project name of
`deploy/ovh/compose.yml`, `plugboard-private`, which is the instance's:

```bash
cd <checkout>
sudo bash deploy/ovh/backup.sh            # prints the archive path: write it down
sudo sha256sum -c <archive-path>.sha256   # <archive-path>: OK
wait_healthy && health                    # "version": <running-version>
```

Data written between this backup and the switch of step 6 is not in the
archive.

The gate asks for a **proven** backup: on the machine that keeps the `age`
identity, pull the archive, keep a copy out of the rotated directory (the
host keeps 7 archives), and prove it decrypts, without writing the plaintext
anywhere. For this instance it has never been done: no archive has been
decrypted nor restored (PLU-63). How the archives reach that machine
(`deploy/ovh/sync-backups.sh` below, or another way) is to be confirmed by the
operator.

```bash
PLUGBOARD_BACKUP_SSH_TARGET=<user@host> PLUGBOARD_HOST_KEY_ALIAS=<host-key-alias> \
  bash deploy/ovh/sync-backups.sh         # "Encrypted backups verified: N"
install -d -m 0700 ~/plugboard-gate
cp -p ~/.local/share/plugboard-backups/plugboard-<stamp>.tar.gz.age \
  ~/.local/share/plugboard-backups/plugboard-<stamp>.tar.gz.age.sha256 ~/plugboard-gate/
age --decrypt -i <identity-file> ~/plugboard-gate/plugboard-<stamp>.tar.gz.age \
  | tar -tzf - | grep -oE '^(app/data|home/app|backup-config)/' | sort | uniq -c
```

Three lines must come out (`app/data/`, `backup-config/`, `home/app/`): the
data, the runtime configuration and the CLI logins. A failure here stops the
gate: there is no way back without a backup.

### 4. Keep the running image, then get the new one

Give the running image a tag of its own, by its ID, so no later tag can take
it away:

```bash
cd <checkout>
sudo docker image tag <running-image-id> plugboard:rollback-<running-version>
sudo docker image ls plugboard
```

The release's image must be on the host as
`plugboard:private-full-v<new-version>`. For 4.14.1 and 4.14.2 the image in
service had the same ID as the UAT's; how the image reaches the host (a copy
of the UAT's, or a build) is to be confirmed by the operator. A build from
the checkout uses the `full` stage, as the repository's
`deploy/ovh/compose.yml` does; a failed build changes nothing:

```bash
cd <checkout>
git checkout v<new-version>
cat VERSION                               # <new-version>
sudo docker build --target full -t plugboard:private-full-v<new-version> .
```

The build fails, on purpose, when a CLI cannot be installed or does not
start (BUG-50): then go back to `<previous-ref>`
([if a step fails](#if-a-step-fails)).

Then write `/etc/plugboard/compose.v<new-version>.json`: the file of the
running version with the image `plugboard:private-full-v<new-version>` and
the settings the `CHANGELOG` asks for. How the operator writes it is to be
confirmed by the operator. Keep the file of the running version unchanged: it
is the way back.

```bash
cd <checkout>
gw config --quiet
gw config --images                        # plugboard:private-full-v<new-version>
gw config --format json | python3 -c 'import json, sys; s = json.load(sys.stdin)["services"]["gateway"]; print("ports:", s.get("ports") or "none"); print("networks:", sorted(s.get("networks") or {}))'
gw_prev config --format json | python3 -c 'import json, sys; print("before:", sorted(json.load(sys.stdin)["services"]["gateway"].get("networks") or {}))'
new_id="$(sudo docker image inspect --format '{{.Id}}' plugboard:private-full-v<new-version>)"
echo "$new_id"                            # write it down: <new-image-id>
[ "$new_id" = "<uat-image-id>" ] && echo "same image as the UAT" || echo "another image than the UAT's"
```

`config --images` must name `plugboard:private-full-v<new-version>`, so that
the switch can never start the previous version. The new file must publish
no port (`ports: none`) and keep the networks of the running version (the
two `networks` lines equal): the instance reuses an existing network, and its
client address chain (nginx, `FORWARDED_ALLOW_IPS`) is described in
[production-instance.md](production-instance.md#client-address-and-rate-limit-bug-119).

`<new-image-id>` (`sha256:...`) is the image the switch will start. A tag
can be moved by another build: the broken first image of 4.14.2 and the
corrected one carried the same tag (BUG-117). Steps 5 and 6 check and start
this ID, not the tag. If the shell was closed, set `new_id` again from what
you wrote down. `same image as the UAT` means the image the green CI and the
UAT checked; another ID is a build of its own, which only the checks of
step 5 vouch for: write it down with the results.

### 5. Check the CLI versions and the bwrap of the new image

In throwaway containers of the new image (by its ID, `<new-image-id>`), as
the user `app`, without network, before switching:

```bash
sudo docker run --rm --network none --entrypoint sh "$new_id" -ec \
  'id -u; gemini --version; claude --version; codex --version; gh --version; agy --version'
# INC-4, BUG-117: no bwrap on the image's PATH (codex would take it)
sudo docker run --rm --network none --entrypoint sh "$new_id" -c 'command -v bwrap'; echo "exit $?"
# The gateway's own bwrap, for agy's cage, at its fixed path off PATH
sudo docker run --rm --network none --entrypoint /usr/libexec/plugboard/bwrap "$new_id" --version
```

Expected: `1000`, then `0.65.0`, `2.1.325 (Claude Code)`, `codex-cli 0.163.0`,
the `gh version` lines and `1.4.4`: the versions pinned in the `Dockerfile`
(the Compose file passes none). Another version or an error stops the gate.

The `bwrap` check prints nothing, then `exit 1`: no `bwrap` on the image's
`PATH`. A path printed (`/usr/bin/bwrap`), or `exit 0`, stops the gate: codex
would use that `bwrap` instead of its own and every codex call would fail
(INC-4, the first 4.14.2 image; BUG-117: the gate saw it only after the
switch). The last command prints `bubblewrap` and its version; an error stops
the gate too (agy would answer `503`). Remove such an image
([below](#remove-an-image-that-failed-the-gate)) once you are back on
`<previous-ref>`.

### 6. Switch to the new version

```bash
cd <checkout>
# The tag still names the image checked at step 5 (BUG-117)
[ "$(sudo docker image inspect --format '{{.Id}}' plugboard:private-full-v<new-version>)" = "$new_id" ] \
  && echo "tag = checked image" || echo "STOP: the tag names another image"
gw up -d --no-build gateway
wait_healthy                              # plugboard:private-full-v<new-version> healthy
[ "$(sudo docker inspect --format '{{.Image}}' "$(gw ps -q gateway)")" = "$new_id" ] \
  && echo "running the checked image" || echo "STOP: not the checked image"
health                                    # "version": <new-version>
gw exec -T gateway sh -c 'tr "\0" " " < /proc/1/cmdline; echo'
# Expected from 4.14.4: /usr/bin/tini -- python run.py
# (or, with init: true in the Compose file: /sbin/docker-init -- /usr/bin/tini -- python run.py)
gw exec -T gateway sh -c 'p1=$(cat /proc/1/comm); echo "PID 1: $p1"
[ "$p1" = tini ] && exit 0
[ "$p1" = docker-init ] && for s in /proc/[0-9]*/stat; do
  read -r pid comm state ppid rest < "$s" || continue
  [ "$ppid" = 1 ] && [ "$comm" = "(tini)" ] && echo "tini under it: pid $pid" && exit 0
done
echo "STOP: neither tini nor docker-init with tini under it"; exit 1'
# Expected: "PID 1: tini", or "PID 1: docker-init" then "tini under it: pid <n>"; exit 0
gw exec -T gateway sh -c "grep -s '^State:[[:space:]]*Z' /proc/[0-9]*/status | wc -l"
# Expected: 0 zombies, in both cases
gw exec gateway sh -ec 'claude --version; codex --version; gemini --version; gh --version; agy --version'
gw exec -T gateway python - < deploy/ovh/verify-agy-sandbox.py; echo "exit $?"
# Expected: one JSON line with "result": "pass", then exit 0
gw exec -T gateway python - < deploy/ovh/verify-codex-sandbox.py; echo "exit $?"
# Expected: one JSON line with "result": "pass", then exit 0
```

The switch recreates the container of the same project on the same network;
nothing is published. PID 1 and zombies are described in
[production-instance.md](production-instance.md#pid-1-of-the-container-tini).
The CLI versions command shows that the CLIs start in the running container
with the CLI logins volume; it does not prove their sign-ins. Check those
with each CLI's own status command. The last two commands check agy's cage
and codex's sandbox without a model request. On exit `1` or `2` of the
first, follow [agy cage](../../deploy/ovh/README.md#agy-cage) before going
on. The second runs `codex sandbox` (never a prompt) with a chat call's
isolation: it must read its own folder only and write nothing, under the
AppArmor profile `plugboard-codex` in enforce mode (a host whose profile has
another name passes it as `python - --apparmor <name>`; the profile's name on
the production host is to be confirmed by the operator); on exit `1` its
cause is on the last line, and every codex call would fail or leak (INC-4):
go back.

### 7. Smoke the new version

```bash
read -rs PLUGBOARD_SMOKE_KEY && export PLUGBOARD_SMOKE_KEY
smoke <new-version>; echo "exit $?"       # 0, in less than 60 s
```

Every line must be `PASS`, except `SKIP embeddings` (no embedding model is
declared while Ollama is off) and `SKIP structured` (a model served by CLI
subscription routes only takes no `response_format`). A `FAIL`: go back now
(step 8, **Back**), then stop the gate.

```bash
mem    # below the stop threshold, oom_kill 0
```

### 8. Rehearse the rollback, then go forward again

Make no admin change (keys, providers, prices) during this step.

**Back** to the previous image, by its ID, with the Compose file of the
running version:

```bash
cd <checkout>
old_id=<running-image-id>
img="$(gw_prev config --images)"          # <running-image-name>
# That name must still be the image that ran before the gate
[ "$(sudo docker image inspect --format '{{.Id}}' "$img")" = "$old_id" ] \
  || sudo docker image tag "$old_id" "$img"
gw_prev up -d --no-build gateway
wait_healthy
[ "$(sudo docker inspect --format '{{.Image}}' "$(gw_prev ps -q gateway)")" = "$old_id" ] \
  && echo "running the previous image" || echo "STOP: not the previous image"
smoke <running-version>; echo "exit $?"
```

**Forward** to the checked image:

```bash
cd <checkout>
[ "$(sudo docker image inspect --format '{{.Id}}' plugboard:private-full-v<new-version>)" = "$new_id" ] \
  && echo "tag = checked image" || echo "STOP: the tag names another image"
gw up -d --no-build gateway
wait_healthy
[ "$(sudo docker inspect --format '{{.Image}}' "$(gw ps -q gateway)")" = "$new_id" ] \
  && echo "running the checked image" || echo "STOP: not the checked image"
smoke <new-version>; echo "exit $?"
```

Both smokes must exit `0`, and both checks print `running the ...`. If the
previous version fails on data the new one wrote, note it: a real rollback
would then need the restore of
[rollback-release.md, step 5](rollback-release.md#5-if-the-old-version-cannot-read-the-current-data).

Then restart the nightly backup. It stops and restarts the gateway every day
between 03:30 and 03:40 UTC: the `/metrics` counters restart then, the access
log goes on.

```bash
sudo systemctl start plugboard-backup.timer
systemctl list-timers plugboard-backup.timer
```

### 9. Observe for 24 hours

Right after the last switch, and again 24 hours later, read the gateway's
counters (a fresh TOTP code each time; the key is passed on standard input,
not on the command line):

```bash
read -rs ADMIN_KEY
printf 'Authorization: Bearer %s\nX-Plugboard-Admin-TOTP: %s\n' "$ADMIN_KEY" "<code>" \
  | curl -s -H @- <gateway-url>/metrics | python3 -m json.tool
```

`/metrics` counts provider calls and provider errors (`requests`,
`errors`), not HTTP answers. The gate criterion is counted on the HTTP
answers of the access log, 24 hours after the last switch. Client routes are
`/v1/...`, `/v2/...` and `/usage/...`:

```bash
gw logs --timestamps --no-log-prefix --since 24h gateway \
  | awk -F'"' '
    NR == 1 { print "first log line: " substr($0, 1, 30) }
    $2 ~ /^[A-Z]+ \/[^ ]* HTTP\/[0-9.]+$/ && $3 ~ /^ [0-9][0-9][0-9]( |$)/ {
      split($2, req, " "); code = substr($3, 2, 3); all++; if (code ~ /^5/) all5++
      if (req[2] ~ /^\/(v1|v2|usage)\//) { api++; if (code ~ /^5/) api5++ }
    }
    END {
      printf "all routes:    %d requests, %d 5xx (%.2f %%)\n", all, all5, all ? 100 * all5 / all : 0
      printf "client routes: %d requests, %d 5xx (%.2f %%)\n", api, api5, api ? 100 * api5 / api : 0
    }'
```

The gate passes when the `client routes` line shows less than `1.00 %` over
at least 100 requests; with fewer, extend the observation. The first log line
must be about 24 hours old: if it is younger, the log was rotated or the
container recreated, and the window is shorter than it looks (the log
rotation of the production Compose file is to be confirmed by the operator;
the repository's model keeps 3 files of 10 MB). Not counted here: an error
inside a stream that already answered `200`, and an error served by nginx or
Tailscale in front of the gateway.

At the end of the 24 hours, the memory of the container must still be below
the [stop threshold](#memory-stop-threshold-d-43), with `oom_kill 0` (the
peak counts from the last start of the container, the nightly backup
included):

```bash
mem
```

### 10. Close the gate

```bash
unset PLUGBOARD_SMOKE_KEY ADMIN_KEY
rm -f ~/plugboard-tools/precheck_data.py ~/plugboard-tools/smoke_consumers.py
```

- Write down the results: versions, image IDs (`<running-image-id>`,
  `<new-image-id>`, `<uat-image-id>`), `<previous-ref>`, archive name, the
  memory lines of step 0 and of `mem`, the three smoke outputs, the
  `/metrics` snapshots and the 5xx rate.
- Keep `plugboard:rollback-<running-version>`,
  `/etc/plugboard/compose.v<running-version>.json` and the archive until the
  next release passes its own gate.
- Revoke the smoke key in the dashboard if it will not be used again.

## If a step fails

Going back to the running version, once step 6 has switched: the previous
image, by its ID, with the Compose file of the running version.

```bash
cd <checkout>
old_id=<running-image-id>
img="$(gw_prev config --images)"
[ "$(sudo docker image inspect --format '{{.Id}}' "$img")" = "$old_id" ] \
  || sudo docker image tag "$old_id" "$img"
gw_prev up -d --no-build gateway
wait_healthy && smoke <running-version>
[ "$(sudo docker inspect --format '{{.Image}}' "$(gw_prev ps -q gateway)")" = "$old_id" ] \
  && echo "running the previous image" || echo "STOP: not the previous image"
git checkout <previous-ref>
sudo systemctl start plugboard-backup.timer
```

This changes the image, not the data: what the newer version wrote stays in
place. Before step 6 nothing was switched: `git checkout <previous-ref>` and
`sudo systemctl start plugboard-backup.timer` are enough.

| Step | Failure | Do |
|---|---|---|
| 0 | memory at or above the stop threshold, `oom_kill` above 0, or a restart on the UAT host | stop before touching production; report the figures |
| 1 | `gw_prev config --images` does not name the running image | stop: find the Compose file the gateway was started from; restart the backup timer |
| 2 | the release is not the UAT commit, or the checkout has commits the release lacks | stop; restart the backup timer |
| 3 | the data check is not `READY` | fix the keys, run it again; do not go on |
| 3 | the backup, its checksum or its decryption fails | stop: no upgrade without a proven backup; restart the backup timer |
| 4 | the image cannot be obtained or built, `config --images` names another image, or the new file publishes a port or changes the networks | `git checkout <previous-ref>`, restart the backup timer; report it |
| 5 | a CLI version differs or does not start | `git checkout <previous-ref>`, restart the backup timer; the image is not the one the CI checked |
| 5 | a `bwrap` is on the image's `PATH`, or the gateway's `bwrap` does not start | `git checkout <previous-ref>`, restart the backup timer; [remove the image](#remove-an-image-that-failed-the-gate) and report it (INC-4, BUG-117) |
| 6 | `STOP`: the tag or the container is not on the image ID checked at step 5 | before `up`: do not switch, get and check the image again (steps 4 and 5); after `up`: go back (block above) |
| 6, 7 | not `healthy`, the PID 1 check says `STOP` (neither tini nor `docker-init` with tini under it), a zombie, a sandbox check fails, or the smoke fails | go back (block above); keep the outputs |
| 8 | the previous version fails its smoke, or does not run `<running-image-id>` | go forward again (**Forward**); a real way back would need the restore of rollback-release.md step 5: escalate before relying on it |
| 9 | 1 % or more `5xx` on the client routes | go back (block above), then investigate with [rollback-release.md](rollback-release.md) |
| 7, 9 | `mem` at or above the stop threshold, or `oom_kill` above 0 | go back (block above); report the figures |

## Remove an image that failed the gate

An image that failed step 5 (a `bwrap` on its `PATH`, as the first 4.14.2
image) must not stay under a tag a later `up -d` could start. Removing it is
the operator's gesture, by its ID, once the gateway runs another image. List
the images and find the ones with a `bwrap` on the `PATH`:

```bash
sudo docker inspect --format '{{.Image}}' "$(gw ps -q gateway)"   # the running image: keep it
sudo docker image ls --no-trunc plugboard
for id in $(sudo docker image ls -q --no-trunc plugboard | sort -u); do
  printf '%s ' "$id"; sudo docker run --rm --network none --entrypoint sh "$id" -c 'command -v bwrap || echo none'
done
sudo docker image rm <broken-image-id>
```

`<broken-image-id>` is an ID the loop prints with a path after it. Never
remove the running image nor `plugboard:rollback-<running-version>`. When
`image rm` answers that the image is referenced in several repositories,
remove each of its tags by name (`sudo docker image rm <tag>`), then the ID.

## Escalation

Hand over the step number, the command, its output (keys redacted) and the
notes of step 10 to the deployment owner. A failure at step 8 or 9 is also
reported as a bug of the release.

## Former private host (deploy/ovh)

The former private host is end of life: its gateway was stopped on
14 November 2026 (see [deploy/ovh/README.md](../../deploy/ovh/README.md)).
Up to 4.14.3 this runbook gave its commands: a checkout in `/opt/plugboard`
driving `deploy/ovh/compose.yml`, a port published on `127.0.0.1:8765`, a
network with a fixed subnet (`172.30.115.0/29`, D-50), the image chosen with
`PLUGBOARD_IMAGE`, and the decryption check of its synchronised backups.
Should that host serve again, take those commands from the history of this
file (`git log -p -- docs/runbooks/prod-gate.md`, the version before PLU-66):
the order and the criteria of the gate are the same, and so are the image
checks of steps 4 to 6 (BUG-117).

See also: [production-instance.md](production-instance.md),
[rollback-release.md](rollback-release.md),
[deploy/ovh/README.md](../../deploy/ovh/README.md).
