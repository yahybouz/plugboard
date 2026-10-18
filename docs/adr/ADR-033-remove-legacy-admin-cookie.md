# ADR-033: Remove the legacy admin-key cookie

## Status

Accepted (2026-04-26). Shipped in v3.0.0.

## Context

Early versions accepted the admin key in clear text in a cookie. Two places in `plugboard/app.py` read it: `_resolve_client`, which identifies the caller of `/v1/*`, and `_require_admin`, which guards the admin endpoints.

A cookie that carries the raw admin key exposes the most sensitive credential of the system (STRIDE: information disclosure):

- **XSS.** If the cookie is not `HttpOnly`, which early versions did not enforce consistently, any script injected into an admin page can read it.
- **Archives.** A backup, a proxy log or a network capture that contains the cookie reveals the admin key directly, with nothing to decrypt.

Since v2.5, `POST /auth/admin/login` issues the admin session cookie instead. It holds a time-limited token (issue time, nonce and an HMAC-SHA256 signature keyed by the admin key; valid 8 hours by default, set with `ADMIN_SESSION_MAX_AGE`). The cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` on non-local hosts. It covers every legitimate use of the legacy cookie.

## Decision

Stop reading the legacy cookie in `_resolve_client` and `_require_admin`. Operators who relied on it sign in through `POST /auth/admin/login` and use the admin session cookie.

The cookie is an internal operator authentication mechanism, not a public API, so the usual public deprecation period does not apply. Immediate removal in v3.0.0 is justified because the replacement already existed and keeping both doubled the attack surface on the admin credential.

## Consequences

Positive:

- Admin access through the API uses either a time-limited signed session or the admin key as a bearer token, never a cookie holding the raw key.
- Less code in the two authentication paths.

Negative:

- Breaking change: a caller that only sends the legacy cookie now gets HTTP 401 on admin endpoints.

Neutral:

- Rollback requires reverting the change and redeploying; there is no environment switch.
- Login and logout delete any leftover legacy cookie from the browser.

Not fully applied:

- The check that gates the dashboard HTML pages (`_check_dashboard_auth` in `plugboard/app.py`) still accepts the legacy cookie. The admin API calls those pages make go through `_require_admin`, which no longer reads it.

## Alternatives considered

- **Keep the cookie with a gradual deprecation.** Leaves a credential leak vector in production with no benefit, since no deployment used the cookie after v2.5.
- **Add `HttpOnly` and `Secure` to the legacy cookie.** Reduces the XSS risk but not the archive and proxy log risk, and adds code without removing the vector.
