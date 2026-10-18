# ADR-034: Remove the synthetic client key built from API_KEY

## Status

Accepted (2026-04-26). Shipped in v3.0.0.

## Context

Early versions had no key management. To make the service usable without `api_keys.json`, `plugboard/apikeys.py` contained a fallback: when the file was missing or empty, `_legacy_record()` built a synthetic key record (`key_id="legacy"`) from the `API_KEY` environment variable, so the value of `API_KEY` worked as a client bearer token.

Since v1.x the canonical flow has been different: keys are created with `POST /admin/keys`, stored hashed in `api_keys.json`, and rotated or revoked through the admin endpoints.

The code itself showed that the fallback was obsolete. Key creation and four admin endpoints each filtered out `key_id == "legacy"`: defensive checks against data the code produced itself. No existing deployment used the fallback, and `API_KEY` had not been documented as a client credential since v2.0.

## Decision

Remove `_legacy_record()`, its call in the key cache refresh, and every filter on `key_id == "legacy"`. A missing or empty `api_keys.json` now means zero client keys.

## Consequences

Positive:

- Dead code and five defensive filters are gone, and the key cache refresh has a single path.
- No implicit behaviour remains: a key record exists only if someone created it.
- Admin key listings no longer need to hide a synthetic entry.

Negative:

- Breaking change: an operator who used the value of `API_KEY` as a bearer token without `api_keys.json` sees client requests rejected with HTTP 401. Migration: create a client key with `POST /admin/keys` before upgrading.

Neutral:

- Rollback requires reverting the change and redeploying.

Superseded in part (two statements of the original text do not match the code):

- It said a missing key file would produce a clear error at startup. There is no startup check; requests are rejected with 401 when they arrive.
- It said `API_KEY` would simply be ignored. It is not: a non-empty `API_KEY` is still the switch that turns client authentication on (see ADR-036). Only its value stopped being accepted as a credential.

## Alternatives considered

- **Keep `_legacy_record()` for compatibility.** No deployment used it, and it cost more in defensive code than it delivered. Dead code also widens the room for bugs in future refactors.
- **Log a deprecation warning at startup and remove it in v4.0.0.** Keeps dead code for a full major cycle for a path with no known user.
