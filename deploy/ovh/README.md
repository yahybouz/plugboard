# Private OVH deployment

> **End of life.** This OVH instance is being retired: it is no longer the
> production instance, which runs on another private host not described in
> this directory. Use these files only to operate the old instance until it is
> shut down; do not start a new deployment from them.
>
> `compose.yml` stays the reference model of the production instance, which
> departs from it on the points listed in
> [the production instance runbook](../../docs/runbooks/production-instance.md).

## Scope

One small Ubuntu VPS, Docker `full`, private Tailscale Serve, no public application ports. The Compose file is a separate production configuration; it does not change the default developer deployment. The CLI versions of the image are pinned in the repository `Dockerfile` (stage `full`), not in the Compose file, and their home directory persists separately from application data. No provider credentials or existing local data are automatically copied.

## Current verification

- Application source baseline: `735232a`; deployment and home-ownership hardening are tracked as separate local commits.
- Full image built and exercised on both local ARM64 and the delivered AMD64 Ubuntu 24.04 VPS.
- `shellcheck`, shell syntax, and Compose configuration validation pass.
- Authentication/MFA regression selection: 106 passed, 46 deselected.
- Production `/health` and `/login` return 200; unauthenticated admin/models and invalid bearer return 401. A separately issued limited client key returns 200 and survives restoration.
- Claude, Codex, Gemini and GitHub CLI version commands work as UID 1000 without home-permission warnings. Root-owned build-time CLI state was fixed in the Dockerfile and the existing volume with a one-off helper; the gateway remains capability-dropped and unprivileged.
- Data and CLI-home markers survive container recreation and a real VPS reboot onto the updated kernel. Docker and Tailscale restart automatically; the gateway is healthy afterwards.
- UFW defaults to deny incoming on IPv4/IPv6; SSH and HTTPS are permitted only on `tailscale0`. Public-IP TCP 22, 443 and 8765 were unreachable from the Mac. The gateway binds only host loopback. Serve is tailnet-only; no Funnel configuration is present.
- The saved tailnet grant allows only the owner identity to the VPS IPv4, not to the Mac. Actual private SSH and certificate-validated HTTPS work; a VPS-to-Mac desktop connection is blocked. A separate outside-tailnet identity was not enrolled solely for testing.
- A real cold backup includes application data, CLI home and runtime encryption/admin configuration. It was age-encrypted, copied to the Mac and SHA256-verified, then decrypted into isolated disposable volumes. Both populated production SQLite databases passed `PRAGMA integrity_check`; the restored app started with no network or published ports and accepted the original client key.
- Daily encrypted VPS backups are scheduled by `plugboard-backup.timer` (03:30 UTC plus up to 10 minutes jitter), keeping 7 archives. `sync-backups.sh` verifies copies and retains 30 on the Mac. The hourly offsite-sync script is scheduled by Hermes, not a LaunchAgent; copying requires an awake Mac with Tailscale and Hermes running. A manual sync passed. Store the Mac's age identity separately in a secure password manager for disaster recovery.
- Provisioning has not sent paid model probes; the later native Ollama test uses the currently free Beam cloud model (see below). The owner's dashboard changes expose three configured active providers in gateway health (`chatgpt-via-codex-cli`, `claude-via-claude-cli`, `anthropic-direct`); configuration alone does not establish usable authenticated inference.
- Admin TOTP enrollment and enforcement are verified: an otherwise-valid admin key alone receives `401` with `mfa_required`. A subsequent `auth.mfa_success` audit entry verifies the owner's completed MFA login. The provisioning QR and manual recovery secret stay in private files on the owner's Mac, not in Git, chat or external QR services; the original restoration test predates this enrollment.
- **Still pending:** actual provider inference through Plugboard after quota-test approval, verification of the owner's Google-account MFA, the CLI-home migration below, and a new restoration test covering the CLI logins made in the `cli-home` volume. Do not describe the entire setup as fully ready until these gates are completed.

The verification list above is a historical record of provisioning; it is not updated by later configuration changes.

## Model and pricing jobs

Provisioning disabled both background jobs. They are now enabled in `compose.yml` (`MODEL_AVAILABILITY_SCHEDULER_DISABLED` and `PRICING_SCHEDULER_DISABLED` set to `"0"`) with the default cadence: prices and model lists every 6 hours, CLI (`subprocess:`) providers at most every 24 hours. The account sync below needs Plugboard 4.3.3 or later: build the `plugboard:private-full-v4.4.0` image, then `docker compose -f deploy/ovh/compose.yml up -d`. The pinned CLI versions are unchanged.

- For the Antigravity provider (`subprocess:agy`), the signed-in account's `agy models` list is the authoritative discovery. It is read without any inference: listing models does not consume the subscription's quota. Every model the account advertises is added to the saved provider list as reported (new IDs included, none hard-coded), the default model is kept, and a model leaves the list only after two consecutive valid passes without it. The unchanged exposure policy (`PLUGBOARD_MODEL_EXPOSE`, default `latest`) can still hide unused older versions of a model family in scheduled passes.
- A listed model is not a tested one. When the subscription's quota is exhausted, agy refuses the request (`Individual quota reached ... Resets in ...`) until the quota resets, even for a model that appears in the list.
- A model already quarantined (refused for the account, unavailable, or after repeated failures) stays hidden: the account list never clears a quarantine. If `agy models` fails, returns nothing or Plugboard falls back to its preset list, the saved models and the default stay as they are.
- `PLUGBOARD_MODEL_REFRESH_PROBE_KINDS` is empty: no CLI model is probed with a completion, neither during the CLI refresh nor at container start, so these jobs use no subscription quota. Claude and Codex keep their saved lists instead of being expanded from pricing data.
- An admin can refresh at once with `POST /admin/auto-update/run` (all catalogs, CLI interval ignored) or `POST /providers/<id>/discover` (one provider; manual removals apply immediately).
- Nothing new is signed in: if `agy` already works inside the gateway, no new authentication is needed. There is no API fallback and no Gemini API key is used. If `agy` is signed out, check the scheduler status (`GET /admin/models/scheduler/status`) and sign in again inside the container rather than adding an API key.
- Prices shown are public API list prices, used for cost estimates. They are not the subscription's invoice.

## Native SSH tools

The Ubuntu host additionally has native Codex 0.163.0, Claude Code 2.1.306, Antigravity CLI (`agy`) 1.4.4 and Ollama 0.40.8. This is separate from the CLIs embedded in the gateway image, but the Codex, Claude Code and `agy` releases match the image's: a test keeps `install-host-tools.sh` aligned with the `Dockerfile`, so a CLI bump updates both files (with the new release checksum) in one change. All four version commands work from an ordinary Ubuntu SSH shell; help commands for the three agents pass without inference.

`install-host-tools.sh` installs one selected tool at a time as `ubuntu`. Stage that script, `ollama-private.service`, and the reviewed official `https://claude.ai/install.sh` as `~/claude-native-install.sh` before invoking it. Codex and Ollama archives are SHA256-verified against their official GitHub release digests; AGY is SHA512-verified against the official installer's release manifest. Claude's native installer verifies its vendor manifest. User-local native binaries have global symlinks so already-open SSH shells can find them without PATH edits.

Ollama runs as the dedicated `ollama` user with systemd, bound only to `127.0.0.1:11434`, limited to 1 GiB RAM and one CPU. Version API and initially empty model inventory were verified; port 11434 is unreachable from the Mac through both public and Tailscale addresses. The gateway remained healthy after installation.

### Free Cloud references

The daemon is signed into a Free Ollama account (checked with local `POST /api/me`, without exposing account identity). Added `beam:cloud` (official catalogue currently labels it free) and `gemma4:31b-cloud` (local model recommendations explicitly report `required_plan: free`). The owner's existing `gpt-oss:20b-cloud` reference was preserved, not classified or tested as zero-priced. Native `ollama list` now exposes all three references; no model weights were downloaded. Beam was tested through the actual daemon/cloud path and returned `OK` with `done_reason: stop`; Gemma's starter eligibility and registration are verified, but its inference was not tested against potentially billable credit balances. `ollama ps` has no local runner.

The [current pricing](https://ollama.com/pricing) distinguishes a finite monthly starter allowance from purchased credits: Cloud does not mean unlimited/free. No credits were bought and no subscription was activated. These are native host Ollama references, not an assertion that the Docker gateway is connected to the host daemon.

## CLI homes

The gateway keeps its CLI logins (`.codex`, `.claude`, `.gemini`) in the named `cli-home` volume mounted at `/home/app`, and nothing else. The host account's native CLI homes are never mounted: the host's own `codex` and `claude` load their configuration (`mcp_servers`, `notify`, hooks) and run it as `ubuntu`, outside the container's seccomp, AppArmor and dropped capabilities. A writable mount of them would let any write access gained inside the gateway run commands on the host, and would expose the native session transcripts and personal subscription tokens. The app's Codex backend ignores user config/rules, uses ephemeral sessions and a restricted permission profile; the Claude backend keeps tools, MCP and settings disabled. Host AGY/Ollama authentication remains separate.

**Migration from the earlier bind mounts** (deployments that mounted `~ubuntu/.codex` and `~ubuntu/.claude` into the gateway):

1. Update the checkout and recreate the gateway: `docker compose -f deploy/ovh/compose.yml up -d`. The volume list no longer has the two binds; `cli-home` and its existing content are kept.
2. Log in again inside the container with each CLI's own login flow, for example `docker compose -f deploy/ovh/compose.yml exec -it gateway codex login`, and `claude` then its `/login` command. Do not copy token or config files from the host account, and do not paste them into tickets, docs or chat.
3. Check the logins with each CLI's auth-status command inside the container, as in step 4 of the deployment below.
4. Before running native `codex` or `claude` on the host again, review `~/.codex/config.toml` and `~/.claude/settings.json` of the host account for `mcp_servers`, `notify` or hook entries you did not add: the container could write there until this migration. Sign the host sessions out and back in if in doubt.
5. Take a new cold backup and restoration test: `cli-home` now holds the gateway's only CLI credentials.

Both CLI auth-status commands succeeded inside the gateway under the exact `build_child_env` used by the adapters (checked with the earlier bind mounts; repeat after the migration). Codex's obsolete `gpt-5.5` default was replaced with `gpt-6.1-sol`; the provider catalogue was rebuilt from the signed-in account's model cache and the new default is visible through `/v1/models`. App health, auth denial checks and persisted data markers pass after recreation. Actual Claude/Codex generation through Plugboard remains unverified: consent for two subscription-quota micro-tests was requested but not received. Do not label the inference path fully verified.

## Codex Linux sandbox

Codex 0.163 requires bubblewrap for filesystem-restricted sessions, including the initial `AGENTS.md` read. Docker's default seccomp and AppArmor policies block its namespace setup even when `kernel.unprivileged_userns_clone=1`; restarting alone does not fix this. The deprecated Landlock fallback explicitly refuses this restricted policy, so do not switch to full-access or disable the sandbox.

Before starting the gateway, run `sudo bash deploy/ovh/install-codex-sandbox.sh`. It installs the persistent, named `plugboard-codex` AppArmor profile. Compose uses that profile and `codex-seccomp.json`: the Moby default-deny policy plus seven namespace/mount setup syscalls. All outer capabilities remain dropped, `no-new-privileges` remains enabled, and no host sysctl is changed. Upstream revision/license are in `codex-sandbox-source.json` and `LICENSE.moby-profiles`.

The Codex child profile keeps `:root=deny`, adds Codex's Linux `:minimal=read` runtime paths so its own helper can execute, masks `/etc`, and allows only its private spawn directory for application data. A real offline test must first read a public file in the allowed directory successfully, then refuse reads outside it and refuse writes. A sandbox that fails to start is not a successful denial test. Local macOS unit coverage skips this Linux integration; it must also be exercised in the real Linux container. No model call is needed for these checks.

The sandbox fix is deployed as `plugboard:private-full-sandbox-v1`. Local regression coverage reports 191 passed and one Linux-only integration skipped on macOS. The real Linux preflight also passes in both a disposable image container and the deployed gateway: allowed file read succeeds, three outside reads and writes are refused, UID1000/zero capabilities/no-new-privileges/seccomp/AppArmor enforcement remain verified. Application auth denial checks, MFA, preserved data marker and private HTTPS pass after recreation. The native Playground dropdown was visually inspected and contains both CLI groups, including the Claude models; no message or model probe was submitted.

Claude CLI catalogues are exposed with the `claude-code:` model prefix, while their owner/UI group remains `claude-via-claude-cli` (distinct from the paid `anthropic-direct` API provider). Discovering preset model names is not an account entitlement or generation test; do not enable paid probes merely to refresh the list.

## agy cage

From 4.14.2, every agy launch runs inside a bubblewrap cage (BUG-86): what agy sees and what happens when the cage cannot start are in [agy's cage](../../docs/guide/providers-and-subscriptions.md#agys-cage). The cage needs the same user and mount namespaces as the Codex sandbox above: the `plugboard-codex` AppArmor profile and `codex-seccomp.json`. The `full` image installs `bubblewrap`.

`verify-agy-sandbox.py` checks the cage inside the running gateway. It makes no model request and never runs agy: it builds the cage of an agy call with the gateway's own code (same environment, same store, a private `TMPDIR`) and runs a small Python probe in it instead of agy. Run it after each switch to a new image, once agy is signed in:

```sh
docker compose -f deploy/ovh/compose.yml exec -T gateway python - < deploy/ovh/verify-agy-sandbox.py; echo "exit $?"
```

It prints one JSON line. Expected: `"result": "pass"`, `"model_requests": 0`, `"agy_run": false`, `"sandbox_start": "pass"`, `"token": "readable, rewritable, unchanged"`, `"failures": []`, then `exit 0`. agy's token is never printed.

| Exit | Meaning | What to do |
|---|---|---|
| `0` | Every check passed: the gateway's data and logs, `~/.claude`, `~/.codex` and `/usr/local/bin/agy` are hidden, writes outside the `TMPDIR` fail, the cage has its own pid namespace, its environment has no server secret and has `AGY_CLI_DISABLE_AUTO_UPDATE=1`, and agy's token is readable and rewritable. | Nothing. |
| `1` | A check failed; `failures` names it. | `agy is not signed in: no token to check`: agy is not signed in; sign in inside the container (`docker compose -f deploy/ovh/compose.yml exec -it gateway agy`), then run the script again. `token changed during the check`: agy refreshed its token meanwhile; run the script again. Any other failure: the cage does not hold; turn the agy provider off and report the JSON line (BUG-86). |
| `2` | The cage cannot start; the cause is on the standard error (`bwrap not found on PATH`, `bwrap`'s first error line...). Every agy call answers `503 provider_sandbox_unavailable`. | Check that the image is the `full` one (`docker compose -f deploy/ovh/compose.yml exec gateway bwrap --version`) and that the container runs with the `plugboard-codex` AppArmor profile and `codex-seccomp.json` ([Codex Linux sandbox](#codex-linux-sandbox)). |

## Ollama bridge

Optional: `compose.yml` ships with `OLLAMA_ENABLED=false` and without this network (decision of 14/11). To use Ollama again, run the installer, set `OLLAMA_ENABLED: "true"` and `OLLAMA_URL`, add the external network `plugboard-ollama` to the gateway and `PLUGBOARD_EMBEDDING_MODELS`.

Native Ollama stays bound to host loopback `127.0.0.1:11434`. `install-ollama-bridge.sh` creates the internal Docker network `plugboard-ollama` (`172.30.114.0/29`, bridge `br-pb-ollama`) and a socket-activated `systemd-socket-proxyd` listening only on `172.30.114.1:11434`, forwarding to loopback. UFW allows that port only on that bridge, from that subnet. Nothing is published on the public or Tailscale interfaces. The gateway joins this network with `OLLAMA_URL=http://172.30.114.1:11434`; cloud-catalog autosync is disabled so only the installed references appear, not unverified paid models. Run the installer before `docker compose up`.

The gateway's own network, `plugboard-private_default`, has a fixed subnet, `172.30.115.0/29`: Tailscale Serve reaches the gateway from its gateway address, `172.30.115.1`, the only address listed in `FORWARDED_ALLOW_IPS`, so the rate limit and the admin-login lockout count each tailnet client (D-50). Tailscale Serve sets `X-Forwarded-For` itself; a client cannot choose its address.

## Deployment after authorized SSH is available

1. Confirm the delivered hostname/IP, OS and architecture from OVH and the live host; establish a dedicated authorized SSH key. Never request passwords in chat.
2. Install Docker Engine/Compose and Tailscale from their official signed repositories.
3. Prepare `/etc/plugboard/runtime.env` with mode 0600 and random independent `ADMIN_KEY`, nonempty `API_KEY` and `ENCRYPTION_KEY`. Back up these secrets outside the VPS securely. `API_KEY` enables client authentication; issue actual `pb_` client keys through the authenticated application.
4. From this repository root:
   ```sh
   sudo bash deploy/ovh/install-codex-sandbox.sh
   docker compose -f deploy/ovh/compose.yml config --quiet
   docker compose -f deploy/ovh/compose.yml up -d --build
   docker compose -f deploy/ovh/compose.yml exec gateway sh -ec 'id; claude --version; codex --version; gemini --version; gh --version'
   bash deploy/ovh/verify.sh
   ```
5. Authorize the server in the existing personal tailnet, enable HTTPS only as needed, then `tailscale serve --bg http://127.0.0.1:8765`. Never enable Funnel. Restrict HTTPS access to the owner by tailnet access policy. Confirm host/cloud ingress and access from both an authorized client and an unauthorized origin.
6. Enroll application TOTP using the existing `scripts/admin_mfa_setup.py` CLI as the gateway user, or the authenticated MFA API if an enrollment UI actually exists. The current static dashboard has no MFA-enrollment page: do not direct the owner to nonexistent settings. Keep provisioning output in a private local file, render its QR entirely on the owner's machine, and have the owner import it and complete the login. Set `PLUGBOARD_ADMIN_MFA_REQUIRED=1`, verify `enrolled` and `enforced` are both true, and test that the admin key alone is refused, at login and as a bearer on `/admin/*` and `/providers`. The environment flag alone does not enroll MFA. Admin scripts then open a session with `POST /auth/admin/login` (key and `totp_code`) or send a fresh code in `X-Plugboard-Admin-TOTP`; each code is accepted once.
7. Only sign into the desired provider CLIs after the user validates their OAuth/device flows. Some subscription providers prohibit gateway usage; inspect current terms. Keep all logins in the persistent CLI-home volume.
8. Before scheduling backups, stop every data writer; archive data, CLI home and the runtime secret configuration using a root read-only helper. Encrypt and export off-host. Resume on errors with a trap. Prove restoration in isolated volumes with populated SQLite databases and `PRAGMA integrity_check` before claiming backups ready.

`verify.sh` intentionally does not create keys, enroll MFA, call models, alter providers or touch user data.

## Upgrades

Move this host to a new release with the ordered commands of [the production gate](../../docs/runbooks/prod-gate.md): data check, backup and its check, build, CLI versions, smoke, rehearsed rollback, then 24 hours of observation. Going back is described in [the rollback runbook](../../docs/runbooks/rollback-release.md#4b-private-host-deployovh).
