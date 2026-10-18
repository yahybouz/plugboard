# ADR-036: AUTH_MODE as a derived constant

## Status

Accepted (2026-05-04), with reduced scope: a derived constant now, a full enum later.

## Context

Since v3.0.0, client authentication has three implicit behaviours, driven by two settings: whether `API_KEY` is empty, and whether strict tenant mode is on (ADR-032). The three branches were mixed inside `_resolve_client` without names, which made operational debugging and reading the hot path harder. A review proposed an `AUTH_MODE` enum.

## Decision

Compute a module-level constant `AUTH_MODE` in `plugboard/app.py` at startup and log it at INFO level (`auth.mode=...`). It takes one of three values:

| `API_KEY` | Strict tenant mode | `AUTH_MODE` |
|---|---|---|
| empty | any | `DISABLED` |
| set | off (`PLUGBOARD_ALLOW_PERMISSIVE=1`) | `LEGACY` |
| set | on (default) | `ENFORCED` |

The routing logic in `_resolve_client` stays exactly as it was. Refactoring an authentication path while guaranteeing no security regression did not fit the iteration.

The full enum, with one `_resolve_*` function per mode and a regression test for each, is deferred. It becomes due as soon as a fourth behaviour depending on `API_KEY` is proposed, or operators ask for it.

## Consequences

Positive:

- Operators read the mode in the startup log instead of inferring it from the environment.
- The constant is the entry point for the future enum.
- No regression risk: the routing code did not change.

Negative:

- The debt is only partly paid: the three branches still live in one function.

What each mode means in the current code:

- `DISABLED`: no bearer token is required. A valid client key, the admin key or an admin session is still resolved when presented, and an invalid client key is rejected with 401. With strict tenant mode on (the default), `POST /v1/chat/completions` still refuses anonymous requests with 401, and startup logs the combination as a critical misconfiguration. `/health` reports `auth_disabled: true`, and unless `PLUGBOARD_DEV=1` is set a critical message is repeated every 5 minutes.
- `LEGACY`: a valid client key, the admin key or an admin session is still required on `/v1/*`. The only difference from `ENFORCED` is that the cache layers accept an empty `client_id` with a warning instead of refusing it.
- `ENFORCED`: a valid client key (`pb_...`), the admin key or an admin session is required, and tenant isolation is enforced in the cache layers.

Superseded in part:

- The original text said `LEGACY` accepts anonymous requests in a degraded mode. `_resolve_client` rejects them with 401 whenever `API_KEY` is set.
- It said startup is refused when `API_KEY` is empty in strict mode. Startup is refused only when strict mode is forced with `PLUGBOARD_STRICT_TENANT=1`; otherwise a critical message is logged.
- It planned to expose `AUTH_MODE` through an admin status endpoint. The mode is only logged at startup.

## Alternatives considered

- **Full enum with three separate functions.** Too large a refactor of a security-sensitive path for the iteration. Deferred until a fourth behaviour appears or operators ask for it.
- **Keep the code as it was, with no constant.** No observability, and the debt stays invisible.
