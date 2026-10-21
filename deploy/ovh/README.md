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
- No completion requests or paid model probes were made; the production health response currently reports zero connected providers.
- Admin TOTP has been enrolled with the existing CLI bootstrap and enforcement is verified: an otherwise-valid admin key alone receives `401` with `mfa_required`. The provisioning QR and manual recovery secret stay in private files on the owner's Mac, not in Git, chat or external QR services. Owner import into an authenticator and the first complete browser MFA login remain pending; the backup restoration above predates this enrollment.
- **Still pending:** owner authenticator import and complete TOTP login, provider OAuth/login, a real inference test after provider approval, and verification of the owner's Google-account MFA. Do not describe the whole setup as fully ready until these gates are completed.

## Deployment after authorized SSH is available

1. Confirm the delivered hostname/IP, OS and architecture from OVH and the live host; establish a dedicated authorized SSH key. Never request passwords in chat.
2. Install Docker Engine/Compose and Tailscale from their official signed repositories.
3. Prepare `/etc/plugboard/runtime.env` with mode 0600 and random independent `ADMIN_KEY`, nonempty `API_KEY` and `ENCRYPTION_KEY`. Back up these secrets outside the VPS securely. `API_KEY` enables client authentication; issue actual `pb_` client keys through the authenticated application.
4. From this repository root:
   ```sh
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
