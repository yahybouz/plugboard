# Private OVH deployment

## Scope

One small Ubuntu VPS, Docker `full`, private Tailscale Serve, no public application ports. The Compose file is a separate production configuration; it does not change the default developer deployment. CLI versions are pinned and their home directory persists separately from application data. No provider credentials or existing local data are automatically copied.

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
- **Still pending:** actual provider inference through Plugboard after quota-test approval, verification of the owner's Google-account MFA, and a new restoration test covering the subsequently shared native CLI homes. Do not describe the entire setup as fully ready until these gates are completed.

## Native SSH tools

The Ubuntu host additionally has native Codex 0.163.0, Claude Code 2.1.306, Antigravity CLI (`agy`) 1.4.4 and Ollama 0.40.8. This is separate from the CLIs embedded in the gateway image. All four version commands work from an ordinary Ubuntu SSH shell; help commands for the three agents pass without inference.

`install-host-tools.sh` installs one selected tool at a time as `ubuntu`. Stage that script, `ollama-private.service`, and the reviewed official `https://claude.ai/install.sh` as `~/claude-native-install.sh` before invoking it. Codex and Ollama archives are SHA256-verified against their official GitHub release digests; AGY is SHA512-verified against the official installer's release manifest. Claude's native installer verifies its vendor manifest. User-local native binaries have global symlinks so already-open SSH shells can find them without PATH edits.

Ollama runs as the dedicated `ollama` user with systemd, bound only to `127.0.0.1:11434`, limited to 1 GiB RAM and one CPU. Version API and initially empty model inventory were verified; port 11434 is unreachable from the Mac through both public and Tailscale addresses. The gateway remained healthy after installation.

### Free Cloud references

The daemon is signed into a Free Ollama account (checked with local `POST /api/me`, without exposing account identity). Added `beam:cloud` (official catalogue currently labels it free) and `gemma4:31b-cloud` (local model recommendations explicitly report `required_plan: free`). The owner's existing `gpt-oss:20b-cloud` reference was preserved, not classified or tested as zero-priced. Native `ollama list` now exposes all three references; no model weights were downloaded. Beam was tested through the actual daemon/cloud path and returned `OK` with `done_reason: stop`; Gemma's starter eligibility and registration are verified, but its inference was not tested against potentially billable credit balances. `ollama ps` has no local runner.

The [current pricing](https://ollama.com/pricing) distinguishes a finite monthly starter allowance from purchased credits: Cloud does not mean unlimited/free. No credits were bought and no subscription was activated. These are native host Ollama references, not an assertion that the Docker gateway is connected to the host daemon.

Native Codex and Claude authentication is now deliberately shared with the gateway through directory bind mounts from the Ubuntu account's `.codex` and `.claude` directories. These sources must exist, remain private and have the same UID1000 owner as the gateway. Do not bind individual token files: native CLIs replace them atomically on refresh. The app's Codex backend ignores user config/rules, uses ephemeral sessions and a restricted permission profile; the Claude backend keeps tools, MCP and settings disabled. Host AGY/Ollama authentication remains separate. The pre-change backup predates these shared credentials; strict cold backups must account for any host CLI writers too.

Both CLI auth-status commands succeed inside the gateway under the exact `build_child_env` used by the adapters. Codex's obsolete `gpt-5.5` default was replaced with `gpt-6.1-sol`; the provider catalogue was rebuilt from the signed-in account's model cache and the new default is visible through `/v1/models`. App health, auth denial checks and persisted data markers pass after recreation. Actual Claude/Codex generation through Plugboard remains unverified: consent for two subscription-quota micro-tests was requested but not received. Do not label the inference path fully verified.

## Codex Linux sandbox

Codex 0.163 requires bubblewrap for filesystem-restricted sessions, including the initial `AGENTS.md` read. Docker's default seccomp and AppArmor policies block its namespace setup even when `kernel.unprivileged_userns_clone=1`; restarting alone does not fix this. The deprecated Landlock fallback explicitly refuses this restricted policy, so do not switch to full-access or disable the sandbox.

Before starting the gateway, run `sudo bash deploy/ovh/install-codex-sandbox.sh`. It installs the persistent, named `plugboard-codex` AppArmor profile. Compose uses that profile and `codex-seccomp.json`: the Moby default-deny policy plus seven namespace/mount setup syscalls. All outer capabilities remain dropped, `no-new-privileges` remains enabled, and no host sysctl is changed. Upstream revision/license are in `codex-sandbox-source.json` and `LICENSE.moby-profiles`.

The Codex child profile keeps `:root=deny`, adds Codex's Linux `:minimal=read` runtime paths so its own helper can execute, masks `/etc`, and allows only its private spawn directory for application data. A real offline test must first read a public file in the allowed directory successfully, then refuse reads outside it and refuse writes. A sandbox that fails to start is not a successful denial test. Local macOS unit coverage skips this Linux integration; it must also be exercised in the real Linux container. No model call is needed for these checks.

Claude CLI catalogues are exposed with the `claude-code:` model prefix, while their owner remains `claude-via-claude-cli`. Discovering preset model names is not an account entitlement or generation test; do not enable paid probes merely to refresh the list.

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
6. Enroll application TOTP using the existing `scripts/admin_mfa_setup.py` CLI as the gateway user, or the authenticated MFA API if an enrollment UI actually exists. The current static dashboard has no MFA-enrollment page: do not direct the owner to nonexistent settings. Keep provisioning output in a private local file, render its QR entirely on the owner's machine, and have the owner import it and complete the login. Set `PLUGBOARD_ADMIN_MFA_REQUIRED=1`, verify `enrolled` and `enforced` are both true, and test that the admin key alone is refused. The environment flag alone does not enroll MFA. Protect the admin bearer separately, because bearer admin authentication bypasses TOTP.
7. Only sign into the desired provider CLIs after the user validates their OAuth/device flows. Some subscription providers prohibit gateway usage; inspect current terms. Keep all logins in the persistent CLI-home volume.
8. Before scheduling backups, stop every data writer; archive data, CLI home and the runtime secret configuration using a root read-only helper. Encrypt and export off-host. Resume on errors with a trap. Prove restoration in isolated volumes with populated SQLite databases and `PRAGMA integrity_check` before claiming backups ready.

`verify.sh` intentionally does not create keys, enroll MFA, call models, alter providers or touch user data.
