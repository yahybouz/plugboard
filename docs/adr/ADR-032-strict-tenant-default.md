# ADR-032: Strict tenant isolation by default

## Status

Accepted (2026-04-26). Shipped in v3.0.0.

## Context

A series of cache isolation fixes in the v2.8.x releases changed the role of `client_id`. It used to be an optional routing field; it became a security precondition. The response cache and the semantic cache are scoped by `client_id`, and without it they cannot guarantee that one tenant never receives an answer cached for another tenant.

The strict tenant flag decides what happens when a cache call has no `client_id`:

- off, the default until v3.0.0: log a warning and accept. The response may then fill a cache entry that has no tenant scope.
- on: refuse the call.

With warn-only as the default, an operator who deployed without reading the documentation got weaker isolation and no visible error. At the time, every existing deployment was run by the maintainers and could be audited before upgrading.

## Decision

Make strict tenant mode the default in v3.0.0.

The variable name is kept (`PLUGBOARD_STRICT_TENANT`). An operator who wants the permissive behaviour sets it to `0`: an explicit opt-out that is visible in the environment.

## Consequences

Positive:

- Every default installation isolates tenants without extra configuration. The secure behaviour is the path of least resistance.
- The recurring "empty client_id" warnings disappear from normal operation.

Negative:

- Breaking change: a caller that sends requests Plugboard cannot attribute to a client now gets an error instead of a 200 with a server-side warning. Tests that relied on anonymous requests had to authenticate.

Neutral:

- Rollback needs no rebuild: change the environment variable and restart.
- Strict mode with an empty `API_KEY` is contradictory, since anonymous requests carry no `client_id`. Plugboard logs this combination as critical at startup, and refuses to start if strict mode was forced with `PLUGBOARD_STRICT_TENANT=1`.

Superseded in part:

- This ADR kept the variable name and deferred a rename to an opt-out flag. The rename happened later. `strict_tenant_mode()` in `plugboard/cache_common.py`, used by every cache backend, now resolves the flag as follows: `PLUGBOARD_ALLOW_PERMISSIVE=1` turns strict mode off; otherwise `PLUGBOARD_STRICT_TENANT=0` still turns it off and logs a deprecation warning; otherwise strict mode is on.
- The original text specified HTTP 400 for refused requests. In strict mode a cache call with an empty `client_id` raises an error, and `POST /v1/chat/completions` refuses a request it cannot attribute to a client with HTTP 401, before reaching the cache.

## Alternatives considered

- **Rename the flag to an opt-out (`PLUGBOARD_ALLOW_PERMISSIVE`).** No security gain over flipping the default, and every deployment's environment would have had to change. Deferred, then done later (see above).
- **Keep warn-only as the default.** Every new installation would run without tenant isolation. Not acceptable for a major release.
- **A six-month deprecation period.** Not justified: every existing deployment could be audited before upgrading.
