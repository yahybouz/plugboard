# Sign out every dashboard session (PB-SEC-04) — design

Status: approved by the operator on 2026-11-10 (short design, small change).

## Intent

Admin dashboard sessions are already server-side (`plugboard/admin_sessions.py`):
logout revokes the browser's own session, and any MFA change revokes them all.
Two gaps remain from the TODOS.md item "Session revocation":

1. The operator cannot cut every open session at once (stolen cookie, lost
   phone, shared computer) without changing MFA or `ADMIN_KEY`.
2. Rotating `ENCRYPTION_KEY` leaves every session open, although an operator
   usually rotates a key because something may have leaked.

## Goals

- `POST /auth/admin/logout-all`: admin only (the same `_require_admin` rules as
  every admin route, MFA included). Revokes every session through
  `admin_sessions.revoke_all()`, records `auth.logout_all` in the audit log,
  clears this browser's cookies and answers 204.
- Dashboard: a "Log out everywhere" button under "Logout" in the sidebar
  footer. It asks for confirmation, calls the route, then goes to the login page
  the way an explicit logout does.
- `scripts/rotate_encryption_key.py`: after a rotation that was actually
  completed (the key changed), revoke every session and say so, even when no
  secret was stored yet. A dry run or a refused rotation revokes nothing.
- Docs: `docs/guide/monitoring.md` (sessions paragraph), the rotation section of
  `docs/guide/configuration.md`, `CHANGELOG.md`, and the TODOS.md entry.

## Non-goals

- Listing sessions or revoking one other device's session.
- A separate session epoch counter (option B below).
- Per-person dashboard keys (PB-AUTH-01).

## Approaches

| Option | What it does | Trade-off |
|---|---|---|
| **A — chosen** | Route + button + revocation in the rotation tool, all on the existing server-side store | Small; reuses `revoke_all()` |
| B | A session epoch recorded in each session, bumped to refuse them all | Survives a session store that cannot be rewritten; more code for a case the MFA generation already covers |
| C | Document that changing `ADMIN_KEY` already cuts every session | No code; heavy for the operator |

The operator picked A: the epoch the TODO asked for predates the server-side
session store, which now gives the same effect.

## Left to the builder

- Where exactly the rotation tool calls the revocation (script or
  `secrets_rekey`), as long as it uses the same data directory as the rotation.
- Button wording and styling, following the existing sidebar footer buttons.
