# UAT deployment

The configuration of the UAT gateway, as it runs on the UAT host. It is versioned here so that every change to it is reviewed; the host keeps the files it actually uses.

## Files

| File here | Path on the UAT host | What it is |
|---|---|---|
| `compose.yml` | `/srv/plugboard/compose.yml` | Docker Compose project `uat-plugboard`: one `gateway` service, published on `127.0.0.1:18765` only, all capabilities dropped, confined by the two profiles below. |
| `compose.env.example` | `/srv/plugboard/compose.env` | Compose variables: `PLUGBOARD_IMAGE`, the image tag the deployer sets on each deployment. |
| `runtime.env.example` | `/srv/plugboard/secrets/runtime.env` | Names of the gateway's runtime secrets (`ADMIN_KEY`, `API_KEY`, `ENCRYPTION_KEY`), loaded by `env_file`. |
| `seccomp-plugboard-userns.json` | `/srv/plugboard/seccomp/plugboard-userns.json` | Seccomp profile: the Docker default plus the user-namespace and mount calls the codex sandbox (`bwrap`) needs. |
| `apparmor-plugboard-userns` | `/etc/apparmor.d/plugboard-userns` | AppArmor profile `plugboard-userns`: `docker-default` plus `mount` inside the user namespace that `bwrap` creates. |
| `deployer-entry.json` | entry `plugboard` of `/etc/uat-deployer/apps.json` | How the host's deployer builds, starts and health-checks the gateway. |

## How the UAT is deployed

Only by the host's deployer, started with the local CI tool of the UAT host: `ci deployer plugboard <sha>` (that is `python3 /srv/dev/.ci/outils/ci.py deployer plugboard <sha>`; `--etat` prints the SHA in service without deploying). The deployer refuses a commit without a green local CI run, builds the `full` image `plugboard:uat-<sha7>` when it is missing, writes `PLUGBOARD_IMAGE` into `compose.env`, recreates the `gateway` service and rolls back by itself when `/health` does not answer `200` or the container is not healthy on the new image (`sante_delai_s`, 120 seconds, in `deployer-entry.json`). There is no remote CI pipeline. Never deploy the UAT with a manual `docker compose up`.

A change to a file here takes effect only once a root operator installs it on the host. Editing the repository does not change the running UAT.

## Faithful copies

`compose.yml`, `seccomp-plugboard-userns.json` and `apparmor-plugboard-userns` are byte-for-byte copies of the host files, taken on the dates of the table below. Their comments are kept as they are on the host. To check the host for drift, a root operator compares `sha256sum <host path>` with the digest below; `tests/test_uat_deploy_config.py` checks that this table matches the files here.

| File here | SHA-256 | Copied | Drift check on the host |
|---|---|---|---|
| `compose.yml` | `8f5a1e0a382ab5263f8f4ec035149697ffc5573a489713d3e512def2e41f2d40` | 2026-11-14 | `sha256sum /srv/plugboard/compose.yml` |
| `seccomp-plugboard-userns.json` | `35afc90b6fbd72bcd6d6af1df24bc1250cf6c196d1e01f384cf2688930b74ed5` | 2026-11-12 | `sha256sum /srv/plugboard/seccomp/plugboard-userns.json` |
| `apparmor-plugboard-userns` | `74b76daa402cbc29fc70caa75b162cecf15fe525e450bdf361318a30964a99e1` | 2026-11-12 | `sha256sum /etc/apparmor.d/plugboard-userns` |

Since 2026-11-14 `compose.yml` is the 4.14.0 version: Ollama off (`OLLAMA_ENABLED: "false"`, no relay, no embedding model) and the model catalogue and price schedulers on, without inference probes (INC-2). Until an operator copies it to the host, the host file still has the previous digest `b154b0ef238d1d84ea3c5702a2a336397f971dccc5f33e15060aecf00aba9f36`. To use Ollama again on the UAT: set `OLLAMA_ENABLED: "true"`, `OLLAMA_URL` (the relay), `PLUGBOARD_EMBEDDING_MODELS` and `PLUGBOARD_OLLAMA_TOKEN` in `secrets/runtime.env`.

When the host file changes, copy it here again in the same change and update its digest and date.

## What is not in the repository

Only the values of `secrets/runtime.env`: they are not in the repository and are never committed. `runtime.env.example` lists the names with nothing after `=`. The value of `PLUGBOARD_IMAGE` is not a secret; it changes on every deployment, so `compose.env.example` leaves it empty too.

## PID 1 of the gateway container: tini

From 4.14.4 the image starts `tini` as PID 1 (BUG-113, BUG-123); `compose.yml` needs no `init: true` (with it, Docker's own init would only sit in front of tini, harmlessly). tini reaps the orphans the gateway's CLIs leave (codex's `bwrap`, `git`, a shell) and passes `SIGTERM` on to the gateway. With 4.14.2, the gateway was PID 1 and kept 68 zombies in one hour. A root operator checks `/proc` after each deployment (the slim image may have no `ps`):

```bash
docker exec uat-plugboard-gateway-1 sh -c 'tr "\0" " " < /proc/1/cmdline; echo'
# Expected: /usr/bin/tini -- python run.py
docker exec uat-plugboard-gateway-1 sh -c "grep -s '^State:[[:space:]]*Z' /proc/[0-9]*/status | wc -l"
# Expected: 0 (zombies), again after some agy and codex calls
```

## Sandbox checks after each deployment

Two offline checks, run by a root operator from a checkout of the deployed commit; neither makes a model request. codex's sandbox (PLU-71), which would have shown INC-4 before any client did; the UAT confines the gateway with `plugboard-userns`, so the check is told that profile (without `--apparmor` it requires production's `plugboard-codex`, and its other checks are the same):

```bash
docker exec -i uat-plugboard-gateway-1 python - --apparmor plugboard-userns < deploy/ovh/verify-codex-sandbox.py; echo "exit $?"
# Expected: one JSON line with "result": "pass" and "apparmor": "plugboard-userns (enforce)", then exit 0
docker exec -i uat-plugboard-gateway-1 python - < deploy/ovh/verify-agy-sandbox.py; echo "exit $?"
# Expected: one JSON line with "result": "pass", then exit 0
```

`verify-codex-sandbox.py` runs `codex sandbox` (never `codex exec`, never a prompt) with the isolation of a chat call: the probe must read its own folder, fail to read three files outside it, and fail to write. On exit `1`, the cause is on the last line; agy's check is described in [deploy/ovh](../ovh/README.md#agy-cage).

## CLI versions

The provider CLI versions of the `full` image come from the `Dockerfile` only (the `ARG` lines of its `full` stage); see [Deployment, CLI versions](../../docs/guide/deployment.md#cli-versions). `deployer-entry.json` is the target state of the deployer's entry: its image carries no `build_args`. The live entry on the host must carry no `build_args` either; a root operator removes them there, otherwise the deployer would pass a second copy of the versions that overrides the `Dockerfile`.
