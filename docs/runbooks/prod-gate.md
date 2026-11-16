# Runbook: production gate (private host upgrade)

The ordered commands that move the private host (`deploy/ovh/`) to a new
release, with a way back at every step: data check, backup and its check,
build, CLI versions, smoke, a rehearsed rollback, then 24 hours of
observation. The operator runs every command; nothing here is automated.

The gate passes when the new version serves for 24 hours with **less than
1 % of `5xx` answers** on the client routes, over at least 100 client
requests, after a memory measure under load below the stop threshold, a
green smoke and a rollback rehearsed both ways.

This is a planned procedure, not an incident runbook: its sections are the
steps of the gate.

The commands below are written for the former private host `deploy/ovh/`,
which is end of life (see [deploy/ovh/README.md](../../deploy/ovh/README.md)).
The production instance now runs on another private host whose deployment
files are not in `deploy/`: keep the order and the criteria of the gate (data
check, proven backup, CLI versions, smoke, rehearsed rollback, 24 hours of
observation), not these paths and names.

`ci` is the local CI tool of the UAT host (`python3 /srv/dev/.ci/outils/ci.py`,
subcommands `etat`, `lancer`, `deployer`); `ci deployer` refuses a commit
without a green local CI run. There is no remote CI pipeline.

## When to use

Before every production upgrade of the private host. The UAT host must
already run the same commit (`ci deployer`), with a green smoke and a
rehearsed rollback ([rollback-release.md](rollback-release.md#rehearse-the-rollback)).
Write down that commit: `ci deployer plugboard <sha> --etat` prints it as
`sha_en_service`.

Avoid 03:30 to 03:40 UTC: the daily backup timer (03:30 UTC plus up to 10
minutes of random delay) stops and restarts the gateway then.

## Before you start

- A shell on the host as the administrator account, with `sudo`, in
  `/opt/plugboard`, kept open for the whole gate (the steps share shell
  functions and variables). `git -C /opt/plugboard status` must work without
  `sudo`; if the checkout belongs to root, prefix every `git` command with
  `sudo`.
- `/etc/plugboard/runtime.env` is readable by root only: every
  `docker compose` command runs with `sudo`, and an image is passed with
  `sudo env PLUGBOARD_IMAGE=...` (an `export` does not cross `sudo`).
- The admin key and a fresh TOTP code for each admin call (admin MFA is
  enforced; a code is accepted once).
- A **smoke key**: a client key created in the dashboard for the gate only,
  with a small monthly limit, whose `allowed_models` holds one chat model
  of a CLI subscription provider (`claude-code:`, `Codex:`,
  `antigravity-via-agy-cli:`)
  that `GET /v1/models` lists for it. Ollama is off in
  `deploy/ovh/compose.yml` (`OLLAMA_ENABLED=false`, no embedding identity):
  the key holds no `ollama:*` model, and the smoke's embeddings step is a
  `SKIP`. Each smoke run sends two short chat calls on the subscription's
  allowance (the structured one is refused before any call); the gate runs
  three smokes.
- The `age` identity of the backups, on the machine that keeps it.

Placeholders: `<running-version>` is the version in service before the gate
and `<running-image-name>` the name of its image (step 1), `<previous-ref>`
the checkout before the gate (step 1), `<new-version>` the release being
deployed (its `VERSION` file), `<model>` the chat model of the smoke key,
`<uat-sha>` the commit in service on the UAT host.

Define the shell helpers once:

```bash
cd /opt/plugboard
gw() { sudo docker compose -f deploy/ovh/compose.yml "$@"; }
wait_healthy() {
  for _ in $(seq 60); do
    state="$(sudo docker inspect --format '{{.Config.Image}} {{.State.Health.Status}}' "$(gw ps -q gateway)")"
    case "$state" in *" healthy") echo "$state"; return 0 ;; esac
    sleep 5
  done
  echo "not healthy after 5 minutes: $state"; return 1
}
# Embeddings are skipped: no embedding model is declared while Ollama is off.
smoke() {
  python3 ~/plugboard-tools/smoke_consumers.py --url http://127.0.0.1:8765 \
    --model <model> --expect-version "$1"
}
# Memory of the gateway container (gateway and CLI processes), from its cgroup
mem() {
  gw exec -T gateway sh -c 'echo "peak $(( $(cat /sys/fs/cgroup/memory.peak) / 1048576 )) MiB, \
now $(( $(cat /sys/fs/cgroup/memory.current) / 1048576 )) MiB, \
$(grep oom_kill /sys/fs/cgroup/memory.events)"'
}
```

## Memory stop threshold (D-43)

`deploy/ovh/compose.yml` limits the gateway container to `mem_limit: 3g`
(3072 MiB), CLI processes included. The gate **stops** when the memory peak
of the container reaches **2457 MiB (80 % of the limit)**, when
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


### 1. Record what runs now and pause the daily backup

```bash
cd /opt/plugboard
curl -s http://127.0.0.1:8765/health      # "version": <running-version>
sudo docker inspect --format '{{.Config.Image}} {{.Image}} {{.State.Health.Status}}' "$(gw ps -q gateway)"
# write down the first word: <running-image-name>
git rev-parse HEAD && git rev-parse --abbrev-ref HEAD   # write both down: <previous-ref>
git status --short                        # must print nothing
sudo systemctl stop plugboard-backup.timer
```

Local changes would be left behind by the checkout of step 4: reconcile them
with the release first.

### 2. Fetch the release, check it, copy its tools

```bash
cd /opt/plugboard
git fetch --tags origin
git rev-parse "v<new-version>^{commit}"   # must equal <uat-sha>
git log --oneline "v<new-version>..HEAD"  # must print nothing
install -d -m 0700 ~/plugboard-tools
git show v<new-version>:scripts/precheck_data.py > ~/plugboard-tools/precheck_data.py
git show v<new-version>:scripts/smoke_consumers.py > ~/plugboard-tools/smoke_consumers.py
git show v<new-version>:CHANGELOG.md | less
```

`git log` lists the commits of this checkout that the release does not
contain (deployment changes made on the host): they must be in the release
before the gate. In the `CHANGELOG`, read every entry after
`<running-version>`: a new or required setting goes into
`/etc/plugboard/runtime.env` before step 6. The smoke and the data check come
from the new release; older releases do not ship them.

### 3. Check the data, then back it up

The data check reads the live data volume **read-only**, with the Python of
the running image (by ID) as the user `app`; it prints file names,
`key_id`s and rules, never a stored value.

```bash
cd /opt/plugboard
cid="$(gw ps -q gateway)"
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

Then take a cold backup (the gateway stops while the archive is written) and
check it.
This backup is mandatory: from 4.12.0 the first daily retention pass (04:00
UTC) deletes old usage, audit, watchdog, monitoring and session rows for good
(see [monitoring](../guide/monitoring.md)), and this archive is then the only
copy of them.

```bash
cd /opt/plugboard
sudo bash deploy/ovh/backup.sh            # prints the archive path: write it down
sudo sha256sum -c <archive-path>.sha256   # <archive-path>: OK
wait_healthy && bash deploy/ovh/verify.sh # every line PASS
```

Data written between this backup and the switch of step 6 (usage of the
build time) is not in the archive.

On the machine that keeps the `age` identity, pull the archive (the same
variables as the scheduled copy; `PLUGBOARD_BACKUP_DIRECTORY` if it is not
the default `~/.local/share/plugboard-backups`), keep a copy out of the
rotated directory (the host keeps 7 archives, this copy 30), and prove it
decrypts, without writing the plaintext anywhere:

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

### 4. Keep the running image, then build the new one

Give the running image a tag of its own, so the build cannot take its name:

```bash
cd /opt/plugboard
sudo docker image tag "$(sudo docker inspect --format '{{.Image}}' "$(gw ps -q gateway)")" \
  plugboard:rollback-<running-version>
sudo docker image ls plugboard
```

Build the release without switching yet (a failed build changes nothing):

```bash
cd /opt/plugboard
git checkout v<new-version>
cat VERSION                               # <new-version>
gw config --quiet
gw config --images                        # plugboard:private-full-v<new-version>
gw build gateway
new_id="$(sudo docker image inspect --format '{{.Id}}' plugboard:private-full-v<new-version>)"
echo "$new_id"                            # write it down: <new-image-id>
```

`config --images` names the image this release's compose file starts by
default; it must be `plugboard:private-full-v<new-version>`, so that a later
plain `up -d` can never start the previous version. The build fails, on
purpose, when a CLI cannot be installed or does not start (BUG-50): then go
back to `<previous-ref>` ([if a step fails](#if-a-step-fails)).

`<new-image-id>` (`sha256:...`) is the image this build produced. A tag can
be moved by another build: the broken first image of 4.14.2 and the corrected
one carried the same tag (BUG-117). Steps 5 and 6 check and start this ID, not
the tag. If the shell was closed, set `new_id` again from what you wrote down.

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

From this release, `deploy/ovh/compose.yml` fixes the default network to
`172.30.115.0/29` and trusts only its gateway, `172.30.115.1`, in
`FORWARDED_ALLOW_IPS` (D-50): Tailscale Serve reaches the gateway from that
address, and each tailnet client then gets its own rate limit and login
lockout. A network created by an earlier release has another subnet: it is
removed once, with the container, by `gw down` (never `-v`: the volumes and
`plugboard-ollama` stay).

```bash
cd /opt/plugboard
# The fixed subnet must not overlap a host route (VPN, another bridge).
# Expected: "free", or only the bridge of plugboard-private_default once fixed.
python3 -c 'import ipaddress,json,subprocess;w=ipaddress.ip_network("172.30.115.0/29");r=json.loads(subprocess.check_output(["ip","-j","-4","route"]));print([(x["dst"],x.get("dev")) for x in r if x.get("dst") not in (None,"default","0.0.0.0/0") and w.overlaps(ipaddress.ip_network(x["dst"],strict=False))] or "free")'
net=$(sudo docker network inspect plugboard-private_default \
  --format '{{range .IPAM.Config}}{{.Subnet}} {{.Gateway}}{{end}}' 2>/dev/null || true)
# A network of an earlier release (another subnet) is removed once
[ -z "$net" ] || [ "$net" = "172.30.115.0/29 172.30.115.1" ] || gw down   # never -v
# The tag still names the image checked at step 5 (BUG-117)
[ "$(sudo docker image inspect --format '{{.Id}}' plugboard:private-full-v<new-version>)" = "$new_id" ] \
  && echo "tag = checked image" || echo "STOP: the tag names another image"
gw up -d --no-build gateway
wait_healthy                              # plugboard:private-full-v<new-version> healthy
[ "$(sudo docker inspect --format '{{.Image}}' "$(gw ps -q gateway)")" = "$new_id" ] \
  && echo "running the checked image" || echo "STOP: not the checked image"
sudo docker network inspect plugboard-private_default \
  --format '{{range .IPAM.Config}}{{.Subnet}} {{.Gateway}}{{end}}'
# Expected: 172.30.115.0/29 172.30.115.1
gw exec gateway sh -ec 'claude --version; codex --version; gemini --version; gh --version; agy --version'
gw exec -T gateway python - < deploy/ovh/verify-agy-sandbox.py; echo "exit $?"
# Expected: one JSON line with "result": "pass", then exit 0
gw exec -T gateway python - < deploy/ovh/verify-codex-sandbox.py; echo "exit $?"
# Expected: one JSON line with "result": "pass", then exit 0
```

The CLI versions command shows that the CLIs start in the running container
with the `cli-home` volume; it does not prove their sign-ins. Check those with
each CLI's own status command (see `deploy/ovh/README.md`, CLI homes). The
last two commands check agy's cage and codex's sandbox without a model
request. On exit `1` or `2` of the first, follow
[agy cage](../../deploy/ovh/README.md#agy-cage) before going on. The second
runs `codex sandbox` (never a prompt) with a chat call's isolation: it must
read its own folder only and write nothing, under the AppArmor profile
`plugboard-codex` in enforce mode (a host whose profile has another name
passes it as `python - --apparmor <name>`); on exit `1` its cause is on the
last line, and every codex call would fail or leak (INC-4): go back.

### 7. Smoke the new version

```bash
read -rs PLUGBOARD_SMOKE_KEY && export PLUGBOARD_SMOKE_KEY
smoke <new-version>; echo "exit $?"       # 0, in less than 60 s
bash deploy/ovh/verify.sh
```

Every line must be `PASS`, except `SKIP embeddings` (no embedding model is
declared while Ollama is off) and `SKIP structured` (a model served by CLI
subscription routes only takes no `response_format`). A `FAIL`: go back now (step 8, **Back**), then stop
the gate.

```bash
mem    # below the stop threshold, oom_kill 0
```

### 8. Rehearse the rollback, then go forward again

Make no admin change (keys, providers, prices) during this step.

**Back** to the kept image:

```bash
cd /opt/plugboard
sudo env PLUGBOARD_IMAGE=plugboard:rollback-<running-version> \
  docker compose -f deploy/ovh/compose.yml up -d --no-build gateway
wait_healthy && smoke <running-version>; echo "exit $?"
```

**Forward** to the new image (the default of this release's compose file):

```bash
cd /opt/plugboard
gw up -d --no-build gateway
wait_healthy && smoke <new-version>; echo "exit $?"
```

Both must exit `0`. If the previous version fails on data the new one wrote,
note it: a real rollback would then need the restore of
[rollback-release.md, step 5](rollback-release.md#5-if-the-old-version-cannot-read-the-current-data).

Then restart the daily backup. It stops and restarts the gateway every day
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
  | curl -s -H @- http://127.0.0.1:8765/metrics | python3 -m json.tool
```

`/metrics` counts provider calls and provider errors (`requests`,
`errors`), not HTTP answers. The gate criterion is counted on the HTTP
answers of the access log, 24 hours after the last switch. Client routes are
`/v1/...`, `/v2/...` and `/usage/...`:

```bash
cd /opt/plugboard
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
must be about 24 hours old: if it is younger, the log was rotated (3 files of
10 MB) or the container recreated, and the window is shorter than it looks.
Not counted here: an error inside a stream that already answered `200`, and
an error served by the HTTPS proxy in front of the gateway.

At the end of the 24 hours, the memory of the container must still be below
the [stop threshold](#memory-stop-threshold-d-43), with `oom_kill 0` (the
peak counts from the last start of the container, the daily backup included):

```bash
mem
```

### 10. Close the gate

```bash
unset PLUGBOARD_SMOKE_KEY ADMIN_KEY
rm -f ~/plugboard-tools/precheck_data.py ~/plugboard-tools/smoke_consumers.py
```

- Write down the results: versions, image IDs, `<previous-ref>`, archive
  name, the memory lines of step 0 and of `mem`, the three smoke outputs, the
  `/metrics` snapshots and the 5xx rate.
- Keep `plugboard:rollback-<running-version>` on the host and the archive in
  `~/plugboard-gate/` until the next release passes its own gate.
- Revoke the smoke key in the dashboard if it will not be used again.

## If a step fails

Going back to the running version, once step 6 has switched:

```bash
cd /opt/plugboard
# The new compose file honours PLUGBOARD_IMAGE: start the kept image with it
sudo env PLUGBOARD_IMAGE=plugboard:rollback-<running-version> \
  docker compose -f deploy/ovh/compose.yml up -d --no-build gateway
wait_healthy && smoke <running-version>
# Then the previous checkout, whose compose file may name a fixed image:
# that name must point to the kept image, for any later plain `up -d`
git checkout <previous-ref>
sudo docker image tag plugboard:rollback-<running-version> <running-image-name>
sudo systemctl start plugboard-backup.timer
```

The fixed network (`172.30.115.0/29`) stays: the earlier version runs on it
as is, without `FORWARDED_ALLOW_IPS`. If a later `up -d` from the previous
checkout refuses that network, run `docker compose -f deploy/ovh/compose.yml
down` (never `-v`) once, then `up -d`.

Before step 6 nothing was switched: `git checkout <previous-ref>` and
`sudo systemctl start plugboard-backup.timer` are enough.

| Step | Failure | Do |
|---|---|---|
| 0 | memory at or above the stop threshold, `oom_kill` above 0, or a restart on the UAT host | stop before touching production; report the figures |
| 2 | the release is not the UAT commit, or the checkout has commits the release lacks | stop; restart the backup timer |
| 3 | the data check is not `READY` | fix the keys, run it again; do not go on |
| 3 | the backup, its checksum or its decryption fails | stop: no upgrade without a proven backup; restart the backup timer |
| 4 | `config --images` names another image, or the build fails | `git checkout <previous-ref>`, restart the backup timer; report it |
| 5 | a CLI version differs or does not start | `git checkout <previous-ref>`, restart the backup timer; the image is not the one the CI checked |
| 5 | a `bwrap` is on the image's `PATH`, or the gateway's `bwrap` does not start | `git checkout <previous-ref>`, restart the backup timer; [remove the image](#remove-an-image-that-failed-the-gate) and report it (INC-4, BUG-117) |
| 6 | `STOP`: the tag or the container is not on the image ID checked at step 5 | before `up`: do not switch, build and check again (steps 4 and 5); after `up`: go back (block above) |
| 6, 7 | not `healthy`, or the smoke fails | go back (block above); keep the outputs |
| 8 | the previous version fails its smoke | go forward again (**Forward**); a real way back would need the restore of rollback-release.md step 5: escalate before relying on it |
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

See also: [rollback-release.md](rollback-release.md),
[deploy/ovh/README.md](../../deploy/ovh/README.md).
