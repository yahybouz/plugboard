# Per-person dashboard keys (PB-AUTH-01) — design

Date: 2026-11-18. Ticket: PB-AUTH-01 (sprint V5, P1). Owner decision: D-63.

## Intent

Every admin of a deployed instance shares one `ADMIN_KEY` and one TOTP
secret today. The audit log cannot tell who did what, and removing one
person means changing the key for everyone. The owner wants each person to
hold their own dashboard key and their own second factor, named, revocable
on its own and named in the audit log. `ADMIN_KEY` stays as the break-glass
key.

## Decided by the owner (D-63)

- One dashboard key **and one TOTP secret per person**.
- Every person is a **full admin**: no roles, no read-only access.
- A key is **named**, **revocable** on its own, and every admin action it
  makes is **audited under that name**.
- `ADMIN_KEY` keeps working as today, with its own TOTP: the break-glass key.
- No real personal key is created on the UAT or in production by the
  build: tests use throwaway keys. Creating the real keys is the owner's
  gesture.

## Goals

- Create, list and revoke personal dashboard keys, through the admin API and
  the dashboard.
- A personal key opens the dashboard (login → session) and calls the admin
  API, always with a code from its own TOTP secret.
- Revoking a key cuts its open sessions at once.
- Every audit entry written by an admin request names its actor.

## Non-goals

- Roles or read-only users.
- Self-service: a person never creates their own key.
- Resetting a person's TOTP: revoke the key and create a new one.
- Changing how `ADMIN_KEY`, its TOTP and its sessions behave.
- Client API keys (`lle_*`): unchanged.

## Anti-goals (a failure even if it works)

- A personal key that works without its own TOTP code, on any instance.
- A revoked key, or a session it opened, still accepted anywhere.
- A raw key or a TOTP secret readable after creation (store, logs, audit,
  API answers).
- One person's wrong codes locking another person or `ADMIN_KEY`.

## Approaches

| Option | What it does | Trade-off |
|---|---|---|
| **A — chosen** | A new store of people (`data/admin_people.json`) beside `admin_mfa` and `admin_sessions`; the admin auth checks `ADMIN_KEY` first, then a personal key; sessions record the person | Reuses the session store, the TOTP code and the lockout; one more store to back up (it lives in `data/`, inside the backup) |
| B | Reuse client keys (`lle_*`) with an `admin` flag | One key store, but a client key leak would become an admin leak, and client-key code paths (budgets, allowed models) would have to skip admins everywhere |
| C | Accounts with passwords | Password storage and reset flows to build; the owner asked for keys |

A keeps admin and client keys apart, which is the reason the owner wants
per-person keys at all.

## How it works (decided in this design; builder's calls marked)

### Store — `plugboard/admin_people.py`

- `data/admin_people.json`, written with `plugboard.storage.atomic_write_json`,
  mode `0600`.
- One record per person:
  - `id` (`ap_` + 12 hex);
  - `name` (1 to 64 printable characters, unique among active keys);
  - `key_hash` (SHA-256 of the raw key, compared in constant time) and
    `key_prefix` (its first 10 characters, for display);
  - `totp_secret_enc` (encrypted as `admin_mfa` encrypts its secret);
  - `totp_confirmed`, and the last TOTP step used (a code is accepted once);
  - `generation` (random; a session opened under another value is refused);
  - `created_at`, `created_by`, `last_used_at`, `revoked_at`, `revoked_by`.
- An unreadable file refuses every personal key (never a `500`, never an
  open door), and logs the error type only.
- Raw key format: `pba_` + `secrets.token_urlsafe(32)`.

### Creation, listing, revocation — admin API

- `POST /admin/people` `{"name": …}`: answers **once** with the raw key, the
  TOTP secret and its `otpauth://` URI (issuer `Plugboard`, account = name).
  The key starts **pending**: its first accepted code (login or header)
  confirms it. Builder's call: whether a pending key expires.
- `GET /admin/people`: id, name, key prefix, status (`pending`, `active`,
  `revoked`), created/last used/revoked dates and actors. Never a hash or a
  secret.
- `DELETE /admin/people/{id}`: revokes (kept in the list as `revoked`), and
  revokes that person's sessions at once. Idempotent.
- Any admin may call them (D-63: everyone is a full admin), `ADMIN_KEY` or a
  person, a person revoking themselves included.
- These routes are public API: the contract version goes up (minor) and
  `openapi.json` / `mcp.json` are regenerated (`docs/api/README.md`).

### Authentication

- `Authorization: Bearer pba_…` plus `X-Plugboard-Admin-TOTP` with a code of
  **that person's** secret, **always** (even where global admin MFA is not
  required).
- `POST /auth/admin/login` accepts a personal key and its code and opens a
  session. The session record names the person and their `generation`.
  `_is_admin_session` refuses it once the person is revoked or the
  generation differs.
- `ADMIN_KEY` paths are unchanged, including when no personal key exists.
- Lockout: wrong codes count against that person only (scope
  `key:admin:<id>`). A wrong or unknown `pba_` key counts as a wrong admin
  key does today (per address, BUG-119).
- With auth fully off (no `API_KEY` and no `ADMIN_KEY`, local install),
  nothing changes. Without `ADMIN_KEY`, the people routes stay refused like
  every admin route (`503`).

### Audit

- Every audit entry written while serving an admin request names its actor:
  `admin-key` for `ADMIN_KEY`, `person:<id>` with the person's name for a
  personal key, on the key and on the sessions it opened.
- New entries: `admin.person.created`, `admin.person.confirmed`,
  `admin.person.revoked`, `auth.person.mfa_failed`.
- Builder's call: where the actor travels (request state is the expected
  place), and how existing entries without an actor read.

### Dashboard

- A **People** section on the admin page lists the keys and their status,
  creates a key (shows the key, the secret and a QR of the URI **once**, with
  a "copied" confirmation), and revokes one after a confirmation.
- The login screen accepts a personal key the same way as `ADMIN_KEY`
  (key + code).
- Builder's call: QR rendering (no new CDN; a dependency already in the
  page, or the URI as text).

### Docs

- `docs/guide/monitoring.md` (admin API), the admin guide, `CHANGELOG.md`
  `[Unreleased]`, `TODOS.md` entry removed.
- How to hand a key to a person, and how to revoke it.

## Constraints

- Tests: targeted only while building (files touched and their neighbours);
  one full validation in parallel blocks, then one CI, at the milestone
  (rule of 18/11). No test is relaxed.
- No real key on the UAT or in production; throwaway keys in tests only.
- One commit per task; no amend, rebase or reset.
- No personal data, real keys or client names in code, tests or commits.

## Left to the builder

The marked calls above, file layout inside `plugboard/`, test layout,
the exact JSON shapes, and the dashboard markup within the existing style.
