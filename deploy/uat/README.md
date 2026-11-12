# UAT deployment

The configuration of the UAT gateway, as it runs on the UAT host. It is versioned here so that every change to it is reviewed; the host keeps the files it actually uses.

## Files

| File here | Path on the UAT host | What it is |
|---|---|---|
| `compose.yml` | `/srv/plugboard/compose.yml` | Docker Compose project `uat-plugboard`: one `gateway` service, published on `127.0.0.1:18765` only, all capabilities dropped, confined by the two profiles below. |
| `compose.env.example` | `/srv/plugboard/compose.env` | Compose variables: `PLUGBOARD_IMAGE`, the image tag the deployer sets on each deployment. |
| `runtime.env.example` | `/srv/plugboard/secrets/runtime.env` | Names of the gateway's runtime secrets (`ADMIN_KEY`, `API_KEY`, `ENCRYPTION_KEY`, `PLUGBOARD_OLLAMA_TOKEN`), loaded by `env_file`. |
| `seccomp-plugboard-userns.json` | `/srv/plugboard/seccomp/plugboard-userns.json` | Seccomp profile: the Docker default plus the user-namespace and mount calls the codex sandbox (`bwrap`) needs. |
| `apparmor-plugboard-userns` | `/etc/apparmor.d/plugboard-userns` | AppArmor profile `plugboard-userns`: `docker-default` plus `mount` inside the user namespace that `bwrap` creates. |
| `deployer-entry.json` | entry `plugboard` of `/etc/uat-deployer/apps.json` | How the host's deployer builds, starts and health-checks the gateway. |

## How the UAT is deployed

Only by the host's deployer, which builds the `full` image from a commit with a green CI, writes `PLUGBOARD_IMAGE` into `compose.env`, recreates the `gateway` service and rolls back when `/health` does not answer. Never deploy the UAT with a manual `docker compose up`.

A change to a file here takes effect only once a root operator installs it on the host. Editing the repository does not change the running UAT.

## Faithful copies

`compose.yml`, `seccomp-plugboard-userns.json` and `apparmor-plugboard-userns` are byte-for-byte copies of the host files, taken on 2026-11-12. Their comments are kept as they are on the host. To check the host for drift, a root operator compares `sha256sum <host path>` with the digest below; `tests/test_uat_deploy_config.py` checks that this table matches the files here.

| File here | SHA-256 | Copied | Drift check on the host |
|---|---|---|---|
| `compose.yml` | `9ba0e1328932840bc3aca3ca185eed173151ccf6e95a68a326c4f76103123d2f` | 2026-11-12 | `sha256sum /srv/plugboard/compose.yml` |
| `seccomp-plugboard-userns.json` | `35afc90b6fbd72bcd6d6af1df24bc1250cf6c196d1e01f384cf2688930b74ed5` | 2026-11-12 | `sha256sum /srv/plugboard/seccomp/plugboard-userns.json` |
| `apparmor-plugboard-userns` | `74b76daa402cbc29fc70caa75b162cecf15fe525e450bdf361318a30964a99e1` | 2026-11-12 | `sha256sum /etc/apparmor.d/plugboard-userns` |

When the host file changes, copy it here again in the same change and update its digest and date.

## What is not in the repository

Only the values of `secrets/runtime.env`: they are not in the repository and are never committed. `runtime.env.example` lists the names with nothing after `=`. The value of `PLUGBOARD_IMAGE` is not a secret; it changes on every deployment, so `compose.env.example` leaves it empty too.

## CLI versions

The provider CLI versions of the `full` image come from the `Dockerfile` only (the `ARG` lines of its `full` stage); see [Deployment, CLI versions](../../docs/guide/deployment.md#cli-versions). `deployer-entry.json` is the target state of the deployer's entry: its image carries no `build_args`. The live entry on the host must carry no `build_args` either; a root operator removes them there, otherwise the deployer would pass a second copy of the versions that overrides the `Dockerfile`.
