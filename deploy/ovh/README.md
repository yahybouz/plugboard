# Private OVH deployment

## Scope

One small Ubuntu VPS, Docker `full`, private Tailscale Serve, no public application ports. The Compose file is a separate production configuration; it does not change the default developer deployment. CLI versions are pinned and their home directory persists separately from application data. No provider credentials or existing local data are automatically copied.

## Current verification

- Source baseline: `735232a`.
- Image successfully built locally (Linux ARM64 on the Mac); rebuild on the VPS for its actual architecture.
- `shellcheck`, shell syntax, and Compose configuration validation pass.
- Authentication/MFA regression selection: 106 passed, 46 deselected.
- Disposable runtime: `/health` and `/login` return 200; unauthenticated admin/models and invalid bearer return 401.
- Claude, Codex, Gemini and GitHub CLIs execute their version commands as UID 1000. This does NOT establish provider login or inference capability.
- Data and CLI-home markers survive container removal/recreation with named volumes.
- Cold archive extracted successfully using a root archive helper. A separate populated WAL-mode SQLite fixture was archived/restored into an isolated volume; `PRAGMA integrity_check=ok`, sentinel row and root-only file were verified. Real VPS data restoration remains untested.
- No completion requests or paid model probes were made.
- Server access, host firewall, Tailscale TLS/ACLs, MFA enrollment, provider logins, encrypted off-host backup and real VPS restoration are NOT yet verified. An accepted hosting order is not a deployed service.

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
6. Enroll application TOTP through the authenticated dashboard before setting `PLUGBOARD_ADMIN_MFA_REQUIRED=1`; the environment flag alone does not enroll MFA. Protect the admin bearer separately, because bearer admin authentication bypasses TOTP.
7. Only sign into the desired provider CLIs after the user validates their OAuth/device flows. Some subscription providers prohibit gateway usage; inspect current terms. Keep all logins in the persistent CLI-home volume.
8. Before scheduling backups, stop every data writer; archive data, CLI home and the runtime secret configuration using a root read-only helper. Encrypt and export off-host. Resume on errors with a trap. Prove restoration in isolated volumes with populated SQLite databases and `PRAGMA integrity_check` before claiming backups ready.

`verify.sh` intentionally does not create keys, enroll MFA, call models, alter providers or touch user data.
